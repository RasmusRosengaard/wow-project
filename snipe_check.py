#!/usr/bin/env python3
"""Join region-scanner listings against a sell realm's own current cheapest
live listings to flag validated snipes: a listing on some other EU realm
priced well below what the item is currently listed for on your sell realm,
net of the 5% AH cut.

Pricing model (changed 2026-07-25): sell price = the sell realm's own
current cheapest live listing for that item/variant -- not an inferred
sold-price percentile. See find_snipes()'s docstring for the full history
of why the sold-price-percentile approach was replaced (three separate
live production bugs from troll/camped listings corrupting the inferred
price, despite successive patches).

**Matching, changed again 2026-07-26 (human product decision)**: matching
is now purely `(item_id, pet_species_id, pet_quality_id)` -- bonus/modifier
variance (ilvl, sockets, crafted stat rolls, per-craft noise) no longer
gates whether two listings are "the same item" for pricing purposes at all.
See find_snipes()'s docstring for the full reasoning and the bug that
prompted it; `market_key()`/its per-item noise-detection machinery
(`fetch_snapshot.py`) still exist and are still real, but are no longer
called from this module -- they remain in use by `diff_snapshots.py`'s
relist detection and `analyze.py`'s manual debugging tool, both of which
still need finer-grained identity than "same item_id" for their own
purposes.

NOTE: since anything listed on the AH is by definition unsoulbound (BoP items
can't be listed), a flagged snipe can always ride the warband bank to your
sell realm as long as you don't equip/use it before moving it -- the only
remaining per-item risk `snipe_check.py` doesn't model is that kind of
equip-lock, not "is this item BoP."

Usage:
  python snipe_check.py --sell 1403
  python snipe_check.py --sell 1403 --items 190237,192786 --min-discount 0.3
  python snipe_check.py --sell 1403 --items-file watchlist.txt --top 50
  python snipe_check.py --sell 1403 -g              # sort by sell price (gold), highest first
  python snipe_check.py --sell 1403 --max-appearance-sources 1   # unique transmog looks only
                                                     # (needs: python appearance.py --refresh)

Requires: diff_snapshots.py already run for --sell (data/events/{sell}.parquet)
and scan_region.py already run (data/listings/*.parquet).
"""
import argparse
from pathlib import Path

import duckdb
import pyarrow as pa

import analyze
from appearance import AppearanceCache
from item_names import NameCache

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def check_data_ready(sell: int) -> str | None:
    """Returns an error message if `sell`'s data isn't ready to query yet
    (no snapshot collected, or no region-wide listings sweep has run), else
    None. Shared between the CLI (raises SystemExit) and dashboard.py's
    /api/snipes route (raises HTTPException(400)) so the two error messages
    -- previously duplicated near-verbatim in both places -- can't drift.

    Checks for a snapshot, not an events file, since 2026-07-25: pricing
    only ever reads the sell realm's latest snapshot (see
    collect_all.py's module docstring), so an events file -- which nothing
    generates automatically anymore -- was never the right precondition."""
    snap_dir = DATA / "snapshots" / str(sell)
    if not snap_dir.exists() or not any(snap_dir.glob("*.parquet")):
        return f"no snapshots collected for realm {sell} -- run fetch_snapshot.py --cr-id {sell} first"
    if not any((DATA / "listings").glob("*.parquet")):
        return "data/listings/*.parquet not found -- run scan_region.py first"
    return None


def parse_items(items: str | None, items_file: str | None) -> list[int] | None:
    ids: list[int] = []
    if items:
        ids += [int(x) for x in items.split(",") if x.strip()]
    if items_file:
        ids += [int(x) for x in Path(items_file).read_text().split() if x.strip()]
    return ids or None


# Secondary tiebreaker (buy_g ASC, cheapest first) added 2026-07-28, human
# decision: discount_pct is rounded to 1 decimal at the SQL level, so a
# camped/troll sell-realm listing (e.g. a joke 9,999,999g price) can make
# dozens of genuinely-different-priced buy listings all round to the exact
# same displayed discount% -- with no secondary key, their relative order
# was whatever the scan happened to return, not sorted by anything visible.
# Cheapest-first is the most actionable tiebreak among equally-good deals.
#
# Final `, auction_id` tiebreaker added 2026-07-31 (human report: "items
# switch even with no filters touched" + live repro -- re-running
# find_snipes() twice against completely unchanged local data returned 66
# different rows out of 200). Root cause: even with buy_g as a secondary
# key, plenty of rows still tie on *both* discount_pct and buy_g (round-
# number decoy prices are common in the near-100%-discount pool -- see
# CLAUDE.md's "Known gaps" note on it), and DuckDB doesn't guarantee a
# stable row order for ties in a parallel/vectorized query plan -- so which
# tied row won a LIMIT boundary (here) or a ROW_NUMBER() cutoff (the
# item_rank/class_rank windows below) could differ between two otherwise-
# identical query executions. auction_id is stable for a listing's
# lifetime and unique (see CLAUDE.md's Blizzard API facts), so appending it
# makes the full ordering deterministic -- the result only changes when the
# underlying listings/pricing genuinely change, never on its own.
SORT_COLUMNS = {
    "discount": "discount_pct DESC, buy_g ASC, auction_id",
    "gold": "sell_p_g DESC, buy_g ASC, auction_id",
}

CAVEAT = ("NOTE: an AH listing is guaranteed unsoulbound (BoP items can't be "
          "listed), so it can ride the warband bank to your sell realm -- just "
          "don't equip/use it before moving it, or it may bind to that character.")

# Blizzard's real inventory_type.type values for profession tool/accessory
# slots (confirmed live 2026-07-23 against real items -- Mining Pick,
# Blacksmith Hammer, Fishing Pole all return PROFESSION_TOOL; the newer
# profession-accessory slot returns PROFESSION_GEAR). Neither slot is part
# of the visible paperdoll model, so "unique transmog" never meaningfully
# applied to them -- excluded from max_appearance_sources results below.
NON_TRANSMOG_INVENTORY_TYPES = {"PROFESSION_TOOL", "PROFESSION_GEAR"}

# Legacy/vendor jewelry (added 2026-07-31, experimental, human-requested):
# neck/ring/trinket items from long-superseded expansions generate obvious
# bait "100% discount" snipes -- unlike weapons/armor, these slots have no
# transmog-appearance value at all (WoW's transmog system doesn't cover
# neck/finger/trinket), so there's no collector-value angle that could make
# an ancient one worth flagging the way a rare old weapon/armor look might
# be. Confirmed live via blizz.api_get() against real items (2026-07-31):
# Charm of Potent and Powerful Passions (27982, ilvl 26, COMMON, NECK),
# Polished Pendant of Edible Energy (27976, ilvl 22, COMMON, NECK -- sold by
# an NPC vendor for 25g, so any AH price above that is nonsensical by
# definition), Ornate Band (83793, ilvl 37, UNCOMMON, FINGER), Shadowfire
# Necklace (83794, ilvl 37, UNCOMMON, NECK). `inventory_type.type=TRINKET`
# for trinkets also confirmed live via /data/wow/search/item. item_subclass
# is useless as a discriminator here -- it's 0 ("Miscellaneous") for
# neck/finger/trinket/cloak/shirt/tabard alike; inventory_type.type is the
# real per-slot signal. Current-expansion jewelry sits at ilvl 600+; these
# ancient items are rescaled by Blizzard's level-squish down into the
# 20s-30s -- a wide, clean separation.
#
# Known blind spot (same "every heuristic eventually has one" house style
# as market_key()'s old noise-detection history): low ilvl does not mean no
# real market. Level-bracket "twink" builds specifically prize old, low-ilvl
# neck/ring/trinket items (no level requirement to out-level them, no
# transmog competition to crowd them out either) -- some twink BiS jewelry
# is genuinely valuable. This heuristic can't distinguish dead vendor
# jewelry from a twink-valuable item via Blizzard's API alone. Deliberately
# non-authoritative for exactly this reason: annotate-only, never filters
# server-side.
LEGACY_JEWELRY_INVENTORY_TYPES = {"NECK", "FINGER", "TRINKET"}
# Deliberately conservative starting cutoff -- the live-verified examples
# above sit at ilvl 22-37, current-tier jewelry at 600+, so this leaves
# generous headroom without having live-verified every intermediate
# expansion's post-squish ilvl. Tune down if false negatives show up in
# practice, but err toward under- rather than over-flagging given the
# twink-market blind spot above.
LEGACY_JEWELRY_ILVL_MAX = 150

# Class-starter armor (added 2026-07-31, human request, broadening the
# jewelry rule above to every slot/class): a curated item-id set, not a
# threshold rule -- ilvl alone can't discriminate here the way it can for
# jewelry, since hundreds of unrelated epic/rare transmog and PvP-replica
# items also sit at ilvl 1 (confirmed live: a plain `inventory_type=WAIST,
# level=1` search returned 900 items across 9 pages, the overwhelming
# majority unrelated to starter gear). Confirmed live via blizz.api_get()
# (2026-07-31): patch 9.1.5 (2021-11-02) introduced a unified "{Class}'s
# {Slot}" ilvl-1 Common starter armor set per class, replacing the older
# race-specific starting gear -- e.g. Paladin's Girdle (187726, Plate,
# WAIST). Found by probing the contiguous id range around it
# (187690-187778): 9 classes confirmed -- Hunter, Shaman, Paladin, Warrior,
# Warlock, Mage, Priest, Rogue, Druid. Seven get all 6 slots (CHEST/LEGS/
# HAND/WRIST/FEET/WAIST); Rogue and Druid only get 5 (no WRIST piece --
# confirmed real, not a missed id: checked every gap in the surrounding id
# range and searched by exact name for "Rogue's Bracers"/"Druid's Bracers"
# and similar, found nothing). 52 items total. Death Knight/Demon Hunter/
# Monk/Evoker were checked (both this id range and name-pattern search) and
# NOT found -- not an oversight, those hero classes have bespoke starting
# gear from their own class-launch expansion instead of this unified
# revamp.
CLASS_STARTER_ARMOR_ITEM_IDS = frozenset({
    # Hunter (Mail)
    187690, 187691, 187692, 187693, 187694, 187695,
    # Shaman (Mail)
    187716, 187717, 187718, 187719, 187720, 187721,
    # Paladin (Plate)
    187722, 187723, 187724, 187725, 187726, 187727,
    # Warrior (Plate)
    187743, 187744, 187745, 187746, 187747, 187748,
    # Warlock (Cloth)
    187751, 187752, 187753, 187754, 187755, 187756,
    # Mage (Cloth)
    187757, 187758, 187759, 187760, 187761, 187762,
    # Priest (Cloth)
    187763, 187764, 187765, 187766, 187767, 187768,
    # Rogue (Leather)
    187769, 187770, 187771, 187772, 187773,
    # Druid (Leather)
    187774, 187775, 187776, 187777, 187778,
})

# Slithershell set (added 2026-08-01, human request): a naga-themed leveling
# quest-reward armor set, not a class-starter set -- confirmed live via
# blizz.api_get() (`/data/wow/search/item?name.en_US=Slithershell`, single
# page, 10 total results): 8 Leather pieces (CHEST/FEET/HAND/HEAD/LEGS/
# SHOULDER/WAIST/WRIST -- e.g. Slithershell Armwraps, the WRIST piece the
# human named) + 1 Cloth CLOAK, all UNCOMMON quality, ilvl 58, required
# level 50. Same "sus" reasoning as CLASS_STARTER_ARMOR_ITEM_IDS -- an old,
# low-value leveling item, plausible decoy-priced reference for a fake
# "100% discount" snipe. The 10th search result, Slithershell Warglaive
# (170119, Weapon/Warglaives), is deliberately excluded -- the human asked
# for "armors" specifically, and CLASS_STARTER_ARMOR_ITEM_IDS's own
# precedent doesn't cover weapons either.
SLITHERSHELL_ARMOR_ITEM_IDS = frozenset({
    169405,  # Slithershell Vest (CHEST)
    169406,  # Slithershell Boots (FEET)
    169407,  # Slithershell Mitts (HAND)
    169408,  # Slithershell Tricorne (HEAD)
    169409,  # Slithershell Leggings (LEGS)
    169410,  # Slithershell Mantle (SHOULDER)
    169411,  # Slithershell Belt (WAIST)
    169412,  # Slithershell Armwraps (WRIST)
    169434,  # Slithershell Cloak (CLOAK)
})

# Every curated (non-threshold) sus item-id set, unioned once here so
# is_sus_item() only needs one membership check -- a new curated batch
# should add its own documented frozenset above and join it into this set,
# not grow one undifferentiated blob (each batch keeps its own provenance
# comment, e.g. why CLASS_STARTER_ARMOR_ITEM_IDS or SLITHERSHELL_ARMOR_ITEM_IDS
# exists, intact and separately searchable).
CURATED_SUS_ITEM_IDS = CLASS_STARTER_ARMOR_ITEM_IDS | SLITHERSHELL_ARMOR_ITEM_IDS


def is_sus_item(item_id: int, inventory_type: str | None, base_level: int | None) -> bool:
    """True for a "sus" (suspect) item worth a second look before trusting a
    "100% discount" snipe: an old neck/ring/trinket item (see
    LEGACY_JEWELRY_ILVL_MAX's comment for the live-verified examples and the
    twink-market caveat) or one of the curated known-junk item ids (see
    CURATED_SUS_ITEM_IDS's comment -- currently the class-starter armor
    pieces and the Slithershell leveling set). None-safe: a caged pet or
    any item NameCache couldn't resolve yields inventory_type=None/
    base_level=None here, correctly returning False rather than raising --
    same defensive pattern NON_TRANSMOG_INVENTORY_TYPES's callers use."""
    if item_id in CURATED_SUS_ITEM_IDS:
        return True
    return (inventory_type in LEGACY_JEWELRY_INVENTORY_TYPES
            and base_level is not None
            and base_level <= LEGACY_JEWELRY_ILVL_MAX)


# Mirrors dashboard.html's ITEM_CLASS_FILTERS exactly, same bucket keys and
# same item_class/item_subclass rules -- so a class_quotas key here means
# the same category a human sees when they check that box client-side.
# "mount" needs both fields since it's a subclass (5) under the generic
# Miscellaneous class (15), not its own item_class -- a class-15 item with
# a different subclass matches no bucket at all (falls through to None,
# see _class_bucket()), same as any item_class not listed here.
CLASS_BUCKET_RULES: dict[str, tuple[int, int | None]] = {
    "weapon": (2, None),
    "armor": (4, None),
    "container": (1, None),
    "profession": (19, None),
    "housing": (20, None),
    "battlepet": (17, None),
    "quest": (12, None),
    "mount": (15, 5),
    "recipe": (9, None),
}

# Per-call resolution cap for _register_class_quota_maps()'s
# NameCache.ensure_many() call -- bounds a single request's worst-case
# latency the same way collect_all.py's PREWARM_BASE_LEVEL_CAP and
# dashboard.py's earlier ensure_many() calls already do (see "Real
# production outage" in CLAUDE.md): resolving thousands of never-before-seen
# items one at a time is exactly the shape that froze the dashboard for
# minutes before. Set well above what a real query's distinct-item count
# tends to be (confirmed live 2026-07-27 on Draenor: 15,258 distinct items
# among 450,568 qualifying rows) -- an item left unresolved past this still-
# generous cap just doesn't get a chance to fill its bucket's quota yet;
# collect_all.py's background prewarm converges the cache over time
# regardless of user traffic, so this self-heals.
CLASS_QUOTA_RESOLVE_LIMIT = 20000

# Per-item cap *inside* a single class_quotas bucket (added 2026-07-31,
# human report "why do I only have 1 housing item" + live repro on the
# human's own production account). class_rank below ranks by row, not by
# distinct item, and the overall max_per_item cap (capped's item_rank) is
# optional and defaults to unset -- so one real item with an unusually
# large number of qualifying listings can consume most of a small bucket's
# quota before any other item gets a slot. Confirmed live: item 264709
# (Stranglekelp Sack) had 663 qualifying region-wide listings and took 29
# of the free tier's 40-row housing quota (72%), 131 of the subscribed
# tier's 285 (46%), 242 of the superuser tier's 1,565 (15%) -- leaving only
# 5/20/64 distinct housing items visible respectively, before any of the
# user's own client-side filters even ran. This is the same "one thing
# crowds out everything else" failure class_quotas itself exists to
# prevent at the whole-category level (see its own docstring below), just
# recurring one level down inside a single category. Human-specified value
# ("~3-5 rows per item") -- picked from a live example showing 3 preserves
# real variety (37 of 40 free-tier housing slots go to *other* items)
# while still letting a genuinely great, widely-listed item show more than
# a single row.
CLASS_QUOTA_PER_ITEM_CAP = 3


def _class_bucket(item_class: int | None, item_subclass: int | None) -> str | None:
    """Which class_quotas bucket (if any) this item_class/item_subclass pair
    falls into -- None means "not in any quota'd bucket," which the
    class_rank join in find_snipes() treats as excluded, same as an
    explicit 0 quota."""
    for bucket, (cls, subcls) in CLASS_BUCKET_RULES.items():
        if item_class == cls and (subcls is None or item_subclass == subcls):
            return bucket
    return None


def _register_class_quota_maps(con: duckdb.DuckDBPyConnection, distinct_item_ids: list[int],
                                class_quotas: dict[str, int]) -> None:
    """Resolves item_class/item_subclass for every distinct candidate item id
    (bounded by CLASS_QUOTA_RESOLVE_LIMIT) and registers two small relations
    -- `class_quota_item_map` (item_id -> bucket, only for items that
    resolved into one) and `class_quota_bucket_map` (bucket -> quota) -- so
    find_snipes() can rank and filter per bucket as a real SQL window
    function over the *entire* candidate set.

    This replaces an earlier design (2026-07-27, same day) that ran the
    query with a widened but still fixed SQL LIMIT, then bucketed/truncated
    in Python after the fact -- traced live to a real failure: on Draenor,
    450,568 rows qualified at min_discount=0 region-wide, and the first
    genuine Housing candidate didn't appear until rank 39,524 -- far past
    any reasonably-bounded pre-truncation (the widened limit was capped at
    20,000). A fixed search-depth cutoff can't actually *guarantee* a
    category isn't crowded out, only make it less likely -- doing the
    ranking in SQL after joining in class data, with no row-count
    truncation before that ranking runs, is what makes the guarantee real
    rather than probabilistic."""
    names = NameCache()
    names.ensure_many(distinct_item_ids, limit=CLASS_QUOTA_RESOLVE_LIMIT)
    ids: list[int] = []
    buckets: list[str] = []
    for item_id in distinct_item_ids:
        # is_cached() first (2026-07-31, real bug fix, found during a bug
        # audit): item_class()/item_subclass() transparently fall back to a
        # *blocking* one-at-a-time network fetch for anything not already
        # cached -- calling them unconditionally here defeated the whole
        # point of ensure_many()'s bounded, concurrent resolution above.
        # Any item past CLASS_QUOTA_RESOLVE_LIMIT, or whose concurrent fetch
        # transiently failed, would silently reintroduce exactly the
        # sequential-blocking-calls failure mode documented in CLAUDE.md's
        # "Real production outage" -- the mode this whole limit exists to
        # prevent. An uncached item just doesn't get a bucket this call (same
        # as CLASS_QUOTA_RESOLVE_LIMIT's docstring already claims -- this
        # makes that claim actually true); it converges over time via
        # collect_all.py's background prewarm regardless.
        if not names.has_class_info(item_id):
            continue
        bucket = _class_bucket(names.item_class(item_id), names.item_subclass(item_id))
        if bucket is not None:
            ids.append(item_id)
            buckets.append(bucket)
    names.save()
    con.register("class_quota_item_map", pa.table({"item_id": ids, "bucket": buckets}))
    con.register("class_quota_bucket_map",
                 pa.table({"bucket": list(class_quotas.keys()), "quota": list(class_quotas.values())}))


# Server-side junk/decoy pre-filter (added 2026-08-01, human request): a row
# is dropped from the SQL candidate pool -- before class_quotas/max_per_item/
# top even see it -- only when BOTH the sell realm's own cheapest listing
# AND the region median are under this, in gold. Deliberately an OR-to-keep
# (AND-to-drop), not a floor on either number alone: an item can be
# genuinely worth sniping on the strength of just one of the two numbers
# (e.g. a real EU-wide market at healthy prices even if this particular sell
# realm's own current listing happens to be a low outlier, or vice versa) --
# requiring *both* to be healthy would wrongly cut those. The point is
# freeing up a small top-N/class-quota budget from being spent on rows that
# are unambiguously not worth the trip by either measure, not narrowing what
# counts as a real snipe. Applied via find_snipes()'s min_value_floor_g
# param -- dashboard.py's live API always passes MIN_VALUE_FLOOR_G; the CLI
# and every existing test default to None (no floor), so debugging/
# inspecting junk rows directly is still possible and no prior behavior
# changed for callers that don't opt in.
MIN_VALUE_FLOOR_G = 500


def find_snipes(con: duckdb.DuckDBPyConnection, sell_cr: int, *,
                items: list[int] | None = None, min_discount: float = 0.3,
                min_gold: float | None = None, max_gold: float | None = None,
                min_sell_now: float | None = None,
                max_appearance_sources: int | None = None,
                max_per_item: int | None = None,
                class_quotas: dict[str, int] | None = None,
                min_value_floor_g: float | None = None,
                top: int = 50, sort: str = "discount") -> list[dict]:
    """**Pricing model, replaced 2026-07-25 (human product decision)**: the
    sell price for an item/variant is simply the sell realm's own current
    cheapest live listing (`sell_now`'s `cheapest_now`, straight from the
    latest snapshot) -- not an inferred sold-price percentile. The original
    design used a percentile over `sales` (diff_snapshots.py's
    inferred_sale classification), with a `min_sales`/`min_per_day`
    liquidity floor and a current-lowest-listing cap layered on top after
    each new failure mode -- but that classification was never validated
    against real seller behavior (Phase 0's gate was explicitly skipped),
    and kept producing wrong prices live in production despite the patches:
    a single troll/decoy "sale" becoming the entire percentile (item
    15138), a repeated troll sale dragging the median even past the
    min_sales=2 floor (item 206477), and finally item 13051 (Witchfury),
    where the *only two* recorded sales were the same camped-relist troll
    listing at two different joke prices (at the time, the relist-matching
    window required an exact price match, so a price-varying troll relist
    slipped through as two fake "sales" -- fixed 2026-07-25 by
    diff_snapshots.RELIST_PRICE_TOLERANCE, but that fix alone wouldn't have
    saved this case, see below) -- and the existing safety cap couldn't rescue it
    because an unrelated bug (the ilvl-plausibility check treating a
    plausible-looking but still-junk value as a real, distinct item level)
    split the troll listing and a genuinely cheap real listing into two
    different, unmatched market_keys, so the cap never even saw the real
    one. After the same underlying design produced three separate live
    production bugs, each individually patched, the call was to stop
    inferring a historical price at all: the sell realm's current cheapest
    listing is directly observable, needs no classification, and can't be
    fooled by a misclassified sale by construction -- there's nothing to
    misclassify.

    **The tradeoff, stated plainly**: this makes a "snipe" a comparison of
    listing price to listing price -- the same thing TSM Sniper and
    Auctionator already do, which is exactly what this project's own
    original framing (see CLAUDE.md) positioned itself against ("existing
    tools flag items cheap relative to listing prices -- which are
    fiction"). It no longer tries to answer "does this item actually
    sell," only "is this cheaper than what's currently listed on my
    realm." A validated-sales signal could be reintroduced later as a
    secondary liquidity/confidence indicator without being back on the
    pricing critical path -- not built now, a deliberate, discussed
    tradeoff, not an oversight.

    Listings come from every scanned realm except the sell realm itself.
    **Match key, changed 2026-07-26 (human product decision): purely
    `(item_id, pet_species_id, pet_quality_id)`** -- bonus_key/modifier
    variance (ilvl, sockets, crafted stat rolls, per-craft "instance" noise)
    no longer affects whether two listings are treated as the same item for
    pricing at all. `sell_now.cheapest_now` is the sell realm's overall
    cheapest current listing for that item_id, full stop, across every
    bonus_key it has; a buy-side listing matches it regardless of its own
    bonus_key. The raw bonus_key is still carried through on the buy-side
    row for display (dashboard.py's `_variant_label()` still shows "ilvl
    NNN" etc. per row) -- only the matching/grouping stopped looking at it.

    This replaces the previous `market_key(bonus_key)`-based matching (see
    `fetch_snapshot.py`'s `market_key()` and its per-item noise-detection
    machinery, still real code, just no longer called from here) after two
    things came together: (1) a human product decision that bonus/ilvl
    differences shouldn't gate a snipe match at all -- if it's the same
    item_id, it's the same tradeable thing, full pooling, lowest price
    wins; and (2) a confirmed live bug in the noise-detection heuristic
    that motivated re-examining the whole approach: it only ran per item
    above a 20-sample floor (`BONUS_NOISE_MIN_SAMPLES`), and since the
    2026-07-25 retention change stopped accumulating multi-day history,
    traced live on Draenor to ~2,011 of 12,219 items with bonus data
    sitting under that floor (~1,223 of them showing the exact per-craft
    noise shape the heuristic was built to catch, e.g. item 36322 at 19
    samples) -- meaning fragmentation kept recurring silently for exactly
    the lower-liquidity items this product cares most about. See
    `HISTORY.md`'s "Bonus/ilvl matching removed" entry for the full trace
    and discussion.

    bonus_key is empty for every caged pet (82800), so without the pet
    identity fields every pet species/quality would collapse into one
    bucket and get compared against whichever pet happens to be listed
    cheapest -- that's still wrong (a poor-quality pet is not "the same
    item" as a legendary one), so pet identity remains part of the match
    key even though bonus_key no longer is. The pet fields are NULL for
    non-pet items, so this is a no-op for ordinary gear -- joined with IS
    NOT DISTINCT FROM since NULL = NULL is false in SQL and a plain USING
    join would drop every non-pet match.

    max_appearance_sources (Phase 3, added 2026-07-23) filters to items whose
    transmog appearance is shared by at most N distinct item ids region-wide
    (see appearance.py -- backed by wago.tools' ItemModifiedAppearance DB2
    export, not Blizzard's API). 1 means "only appearances no other item
    grants." This is a rarity proxy, not a real obtainability check -- it
    can't tell you if the source is still farmable. Every row also carries
    `appearance_sources` (None if the item isn't in the cache, e.g. cache
    never built) regardless of whether this filter is set, so callers can
    display it either way. Applied in Python after the SQL query rather than
    joined in SQL, since the cache is a local JSON file, not a DB table --
    fine because candidate rows per query are always small. Also excludes
    NON_TRANSMOG_INVENTORY_TYPES (profession tool/accessory slots, confirmed
    live against Blizzard's item API -- e.g. Mining Pick, Fishing Pole) --
    those slots aren't part of the visible paperdoll model at all, so a low
    appearance_sources for one of them isn't a meaningful "unique look" the
    way it is for real transmog-visible gear. This lookup uses
    item_names.NameCache, so filtering by max_appearance_sources can incur
    one Blizzard API call per never-before-seen item (cached after).

    max_per_item (added 2026-07-23) caps how many listings of the *same*
    item/variant can appear in the results, keeping the best (highest
    discount%) N of them via a ROW_NUMBER() window before the outer
    ORDER BY/LIMIT -- without this, a single popular item with dozens of live
    auctions across scan realms could fill the entire `top` budget by itself,
    crowding out variety. `top` still caps the total row count; max_per_item
    caps rows per item on top of that -- e.g. top=500, max_per_item=1 means
    up to 500 *distinct* items.

    min_sell_now (added 2026-07-23; since 2026-07-25 this filters on the
    *same* number now used as the sell price, `sell_p_g`/`sell_copper` --
    there is no longer a separate percentile-vs-current-listing distinction)
    filters out low-value junk that happens to clear the discount
    threshold -- an item where the sell realm's own cheapest listing is
    only a few silver isn't worth the trip regardless of discount%. A row
    with no current listing on the sell realm (cheapest_now IS NULL) is
    excluded entirely regardless of whether this filter is set -- with no
    percentile fallback left, there's simply no price to compare against.

    Each returned row carries the raw `bonus_key` (display only, see
    above) plus `pet_species_id`/`pet_quality_id` -- dashboard.html groups
    rows by `(item_id, pet_species_id, pet_quality_id)` now (2026-07-26,
    replacing the old market_key-based grouping), since that's exactly
    what determines whether two rows are "the same match" for pricing.

    **region_median_g/region_median_copper** (added 2026-07-27, human
    request): the *median* of the same per-other-realm cheapest listings
    (`region_stats.region_median_cheapest`, built from the same `buy` rows
    already loaded here -- no new data source), shown to the user as "the
    EU median price" for the item -- primarily an informational display
    value (doesn't gate anything on its own), though see min_value_floor_g
    below for the one place it's also used to filter. Median rather than a
    mean specifically because this number is shown directly to a human as a
    reference point -- a single other realm having its own outlier listing
    skews a mean far more than a median, and the whole point of showing
    this figure is to be a trustworthy "what does this actually cost
    around the region" number. (`sell_price_suspect`, an earlier mean-based
    scam-price flag that used to live alongside this, was removed
    2026-07-31 -- see HISTORY.md.)

    **min_value_floor_g** (added 2026-08-01, human request, see
    MIN_VALUE_FLOOR_G's own comment above for the full rationale): drops a
    row from the SQL candidate pool when BOTH the sell realm's cheapest
    listing (sell_p_g) and the region median (region_median_g) are under
    this many gold -- an OR-to-keep, AND-to-drop threshold, not a floor on
    either number individually. None (the default, and every CLI call)
    disables it entirely, matching class_quotas' own None-preserves-prior-
    behavior convention. dashboard.py's live `/api/snipes` always passes
    MIN_VALUE_FLOOR_G so a class-quota bucket's limited budget isn't spent
    on rows that are unambiguously junk/decoy by both measures at once.

    **class_quotas** (added 2026-07-27, human request): a {bucket: max_rows}
    dict (bucket keys match dashboard.html's ITEM_CLASS_FILTERS -- weapon/
    armor/container/profession/housing/battlepet/quest/mount, see
    CLASS_BUCKET_RULES) that caps each item-class bucket independently
    instead of one flat top-N by discount% -- see
    _register_class_quota_maps()'s docstring for the real production case
    (a genuine 88.1%-discount Housing snipe crowded out entirely by decoy-
    priced listings elsewhere, and why a first attempt at this that widened
    the SQL LIMIT instead of doing this properly still wasn't enough: 450,568
    rows qualified region-wide on Draenor, and the first genuine Housing
    candidate sat at rank 39,524). None (the default) preserves the exact
    prior behavior -- CLI callers that don't pass this are unaffected. When
    set, the item-deduped candidate set (`capped`, see below) is materialized
    into a temp table with NO row-count truncation at all, so every
    candidate -- however deep a sparse bucket's real matches might sit --
    is actually considered; item_class/item_subclass are resolved for every
    *distinct* item id in it via NameCache (bounded by
    CLASS_QUOTA_RESOLVE_LIMIT) and joined back in as real DuckDB relations
    (`_register_class_quota_maps()`), so the per-bucket ranking
    (`class_rank`) and quota filter run as genuine SQL window functions over
    the complete set -- a guarantee, not a "wide enough in practice" hope.
    A bucket with no entry (or an explicit 0) is excluded entirely -- e.g.
    the free tier deliberately omits Containers/Profession/Quest, a human
    product decision. Within a bucket, `CLASS_QUOTA_PER_ITEM_CAP` (added
    2026-07-31, see its own docstring) always caps how many rows of the
    same item can occupy that bucket's ranking, independent of the
    separate/optional overall `max_per_item` -- otherwise one item with an
    unusually large number of qualifying listings can consume most of a
    small quota by itself, which is exactly the "one thing crowds out
    everything else" failure this whole mechanism exists to prevent, one
    level down."""
    item_filter = f"AND item_id IN ({','.join(map(str, items))})" if items else ""
    # Filters on the buy-side price -- what you'd actually spend on the
    # snipe -- since that's the number an "AH sniper" budget cap means.
    price_filter = ""
    if min_gold is not None:
        price_filter += f" AND b.buy_unit_price >= {float(min_gold) * 10000}"
    if max_gold is not None:
        price_filter += f" AND b.buy_unit_price <= {float(max_gold) * 10000}"
    if min_sell_now is not None:
        price_filter += f" AND n.cheapest_now >= {float(min_sell_now) * 10000}"
    if min_value_floor_g is not None:
        # AND-to-drop (OR-to-keep), see MIN_VALUE_FLOOR_G's comment above --
        # a row survives if *either* number clears the floor, only dropped
        # when both fall short.
        floor_copper = float(min_value_floor_g) * 10000
        price_filter += (f" AND (n.cheapest_now >= {floor_copper}"
                          f" OR rs.region_median_cheapest >= {floor_copper})")
    # When appearance-filtering, pull a wider SQL candidate pool since rows
    # get dropped *after* the query (the appearance cache is a local JSON
    # file, not something to join in SQL) -- otherwise a top-50 SQL LIMIT
    # could get filtered down to near-nothing before the appearance check
    # ever runs.
    sql_limit = int(top) if max_appearance_sources is None else max(int(top) * 20, 1000)
    max_per_item_where = f"WHERE item_rank <= {int(max_per_item)}" if max_per_item is not None else ""
    con.execute(f"""
        CREATE OR REPLACE VIEW listings AS
        SELECT * FROM read_parquet('{(DATA / "listings" / "*.parquet").as_posix()}')
        WHERE cr_id != {int(sell_cr)} AND buyout IS NOT NULL
    """)
    # Shared CTE chain: the sell-realm reference price, the region-wide
    # cross-check stats (region_median), the item-deduped
    # match set -- everything both branches below need, factored out once so
    # class_quotas doesn't pay for these (the expensive part -- region_stats'
    # aggregation over the whole region) twice.
    base_ctes = f"""
        WITH sell_now AS (
            -- Overall cheapest current listing per item_id (+ pet identity),
            -- across every bonus_key it has -- bonus/ilvl variance no longer
            -- splits this into sub-buckets (see docstring above).
            SELECT item_id, pet_species_id, pet_quality_id,
                   min(buyout * 1.0 / quantity) AS cheapest_now
            FROM snaps
            WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM snaps)
              AND buyout IS NOT NULL
            GROUP BY item_id, pet_species_id, pet_quality_id
        ),
        buy AS (
            SELECT cr_id, item_id, bonus_key, pet_species_id, pet_quality_id, auction_id,
                   buyout * 1.0 / quantity AS buy_unit_price
            FROM listings
            WHERE 1=1 {item_filter}
        ),
        region_realm_floor AS (
            -- Cheapest listing per *other* realm for this item -- the same
            -- rows already loaded as snipe candidates below, reused as an
            -- independent cross-check rather than a new data source.
            SELECT cr_id, item_id, pet_species_id, pet_quality_id,
                   min(buy_unit_price) AS realm_cheapest
            FROM buy
            GROUP BY cr_id, item_id, pet_species_id, pet_quality_id
        ),
        region_stats AS (
            -- Median of those per-realm cheapest listings -- "the median EU
            -- price for this item", not a statistic over every individual
            -- auction (which would over-weight realms with more sellers).
            -- Purely informational display value (added 2026-07-27, human
            -- request) -- less skewed by one other realm itself having an
            -- outlier listing, so it's the more honest "typical EU price"
            -- to show a user than a mean would be. (This CTE used to also
            -- compute a mean powering sell_price_suspect, removed
            -- 2026-07-31 -- see HISTORY.md.)
            SELECT item_id, pet_species_id, pet_quality_id,
                   median(realm_cheapest) AS region_median_cheapest
            FROM region_realm_floor
            GROUP BY item_id, pet_species_id, pet_quality_id
        ),
        matches AS (
            SELECT b.cr_id                                            AS buy_realm,
                   b.item_id, b.bonus_key, b.pet_species_id, b.pet_quality_id, b.auction_id,
                   round(b.buy_unit_price / 10000, 2)                  AS buy_g,
                   round(n.cheapest_now / 10000, 2)                    AS sell_p_g,
                   round(b.buy_unit_price)::BIGINT                     AS buy_copper,
                   round(n.cheapest_now)::BIGINT                       AS sell_copper,
                   round(100.0 * (n.cheapest_now * 0.95 - b.buy_unit_price)
                         / (n.cheapest_now * 0.95), 1)                 AS discount_pct,
                   round(rs.region_median_cheapest / 10000, 2)          AS region_median_g,
                   round(rs.region_median_cheapest)::BIGINT             AS region_median_copper
            FROM buy b
            JOIN sell_now n
              ON b.item_id = n.item_id
             AND b.pet_species_id IS NOT DISTINCT FROM n.pet_species_id
             AND b.pet_quality_id IS NOT DISTINCT FROM n.pet_quality_id
            JOIN region_stats rs
              ON b.item_id = rs.item_id
             AND b.pet_species_id IS NOT DISTINCT FROM rs.pet_species_id
             AND b.pet_quality_id IS NOT DISTINCT FROM rs.pet_quality_id
            WHERE (n.cheapest_now * 0.95 - b.buy_unit_price) / (n.cheapest_now * 0.95)
                  >= {float(min_discount)}
                  {price_filter}
        ),
        capped AS (
            SELECT *, ROW_NUMBER() OVER (
                -- item_id (+ pet identity), not bonus_key -- caps per real
                -- item now, so e.g. max_per_item=1 keeps a single best-
                -- discount listing across every bonus/ilvl variant of that
                -- item, not one per exact variant. buy_g/auction_id tiebreak
                -- (2026-07-31, see SORT_COLUMNS' comment above) -- without
                -- it, which listing "wins" a discount_pct tie here was
                -- nondeterministic per query execution, not just in the
                -- final display order.
                PARTITION BY item_id, pet_species_id, pet_quality_id
                ORDER BY discount_pct DESC, buy_g ASC, auction_id
            ) AS item_rank
            FROM matches
        )
    """
    if class_quotas is None:
        res = con.execute(base_ctes + f"""
            SELECT * EXCLUDE (item_rank)
            FROM capped
            {max_per_item_where}
            ORDER BY {SORT_COLUMNS[sort]}
            LIMIT {sql_limit}
        """)
        cols = [d[0] for d in res.description]
        rows = [dict(zip(cols, row)) for row in res.fetchall()]
        rows = _filter_by_appearance(rows, max_appearance_sources)
        return rows[: int(top)]

    # class_quotas path: materialize the item-deduped candidate set with NO
    # row-count truncation at all (see _register_class_quota_maps()'s
    # docstring for why a fixed cutoff can't be trusted), resolve item_class
    # for every distinct item id in it, then rank/filter per bucket as a
    # real SQL window function over the complete set.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE class_quota_candidates AS
        {base_ctes}
        SELECT * EXCLUDE (item_rank)
        FROM capped
        {max_per_item_where}
    """)
    distinct_item_ids = [r[0] for r in
                        con.execute("SELECT DISTINCT item_id FROM class_quota_candidates").fetchall()]
    _register_class_quota_maps(con, distinct_item_ids, class_quotas)
    res = con.execute(f"""
        WITH class_joined AS (
            SELECT c.*, m.bucket, q.quota
            FROM class_quota_candidates c
            JOIN class_quota_item_map m ON c.item_id = m.item_id
            JOIN class_quota_bucket_map q ON m.bucket = q.bucket
        ),
        -- CLASS_QUOTA_PER_ITEM_CAP (2026-07-31, see its own docstring): caps
        -- how many rows of the *same* item can occupy a single bucket
        -- before class_rank even runs, independent of the separate/optional
        -- overall max_per_item -- without this, one item with an unusually
        -- large number of qualifying listings could consume most of a
        -- small bucket's quota, leaving little to no room for other items.
        class_item_capped AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY bucket, item_id, pet_species_id, pet_quality_id
                ORDER BY discount_pct DESC, buy_g ASC, auction_id
            ) AS bucket_item_rank
            FROM class_joined
        ),
        class_ranked AS (
            -- Same buy_g/auction_id tiebreak as `capped` above, same reason:
            -- discount_pct DESC alone left which rows survive a bucket's
            -- quota nondeterministic across identical query executions.
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY bucket ORDER BY discount_pct DESC, buy_g ASC, auction_id
            ) AS class_rank
            FROM class_item_capped
            WHERE bucket_item_rank <= {CLASS_QUOTA_PER_ITEM_CAP}
        )
        SELECT * EXCLUDE (bucket, quota, class_rank, bucket_item_rank)
        FROM class_ranked
        WHERE class_rank <= quota
        ORDER BY {SORT_COLUMNS[sort]}
        LIMIT {sql_limit}
    """)
    cols = [d[0] for d in res.description]
    rows = [dict(zip(cols, row)) for row in res.fetchall()]
    rows = _filter_by_appearance(rows, max_appearance_sources)
    return rows[: int(top)]


def _filter_by_appearance(rows: list[dict], max_appearance_sources: int | None) -> list[dict]:
    """Annotates every row with `appearance_sources` (None if the item isn't
    in the cache, e.g. the cache was never built) regardless of whether
    filtering is active, so callers can always display it. When
    max_appearance_sources is set, filters to items whose transmog
    appearance is shared by at most N distinct items region-wide -- see
    find_snipes()'s docstring for the full rationale. Profession tool/
    accessory slots (NON_TRANSMOG_INVENTORY_TYPES) are always excluded from
    a filtered result even when their appearance_sources happens to look
    "unique" -- a small item pool trivially produces low source counts, but
    neither slot is part of the visible paperdoll model."""
    appearances = AppearanceCache()
    for r in rows:
        r["appearance_sources"] = appearances.source_count(r["item_id"])
    if max_appearance_sources is None:
        return rows
    names = NameCache()
    filtered = [r for r in rows
                if r["appearance_sources"] is not None
                and r["appearance_sources"] <= max_appearance_sources
                and names.inventory_type(r["item_id"]) not in NON_TRANSMOG_INVENTORY_TYPES]
    names.save()
    return filtered


def print_snipes(rows: list[dict], resolve_names: bool = False) -> None:
    if not rows:
        print("no snipes found at these thresholds")
        return
    names = NameCache() if resolve_names else None
    name_col = "name" if resolve_names else "item_id"
    hdr = (f"{'buy_realm':>9} {name_col:>28} {'variant':>14} {'buy_g':>10} {'sell_p_g':>10} "
           f"{'eu_med_g':>10} {'disc%':>7} {'appear':>7}")
    print(hdr)
    for r in rows:
        if r["pet_species_id"] is not None:
            variant = f"pet:{r['pet_species_id']}/{r['pet_quality_id']}"
        else:
            variant = r["bonus_key"] or "-"
        label = names.get(r["item_id"], r["pet_species_id"]) if names else str(r["item_id"])
        appear = r["appearance_sources"] if r["appearance_sources"] is not None else "-"
        print(f"{r['buy_realm']:>9} {label:>28.28} {variant:>14} "
              f"{r['buy_g']:>10.2f} {r['sell_p_g']:>10.2f} {r['region_median_g']:>10.2f} "
              f"{r['discount_pct']:>7.1f} {appear:>7}")
    if names:
        names.save()
    else:
        print("item names: https://www.wowhead.com/item=<item_id>")
    print(CAVEAT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sell", type=int, required=True, help="sell-realm connected-realm id")
    ap.add_argument("--items", help="comma-separated item ids to restrict to")
    ap.add_argument("--items-file", help="file of item ids (whitespace/newline separated)")
    ap.add_argument("--min-discount", type=float, default=0.3,
                    help="minimum discount vs the sell realm's current cheapest listing, "
                         "net of 5%% cut (default 0.3)")
    ap.add_argument("--min-gold", type=float, default=None,
                    help="minimum buy price in gold (budget floor, skips trivial junk)")
    ap.add_argument("--max-gold", type=float, default=None,
                    help="maximum buy price in gold (budget ceiling)")
    ap.add_argument("--min-sell-now", type=float, default=None,
                    help="minimum current sell-realm price in gold (excludes low-value junk "
                         "regardless of discount%%; rows with no current sell-realm listing "
                         "are always excluded, since there's no price to compare against)")
    ap.add_argument("--max-appearance-sources", type=int, default=None,
                    help="only show items whose transmog appearance is granted by at most N "
                         "distinct items region-wide (1 = unique look); requires "
                         "data/appearances.json -- see appearance.py --refresh. Items missing "
                         "from the appearance cache are excluded when this is set. Profession "
                         "tool/accessory items are always excluded (not real transmog slots)")
    ap.add_argument("--max-per-item", type=int, default=None,
                    help="cap how many listings of the same item/variant can appear in the "
                         "results (keeps the highest-discount ones); combine with --top for "
                         "e.g. --top 500 --max-per-item 1 = up to 500 distinct items")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--sort", choices=sorted(SORT_COLUMNS), default="discount",
                    help="sort order for results (default discount)")
    ap.add_argument("-g", "--gold", action="store_true",
                    help="shorthand for --sort gold (highest sell-price items first)")
    ap.add_argument("--names", action="store_true",
                    help="resolve item ids to display names via the static API "
                         "(cached in data/item_names.json; adds one API call per "
                         "never-before-seen item/pet)")
    args = ap.parse_args()

    not_ready = check_data_ready(args.sell)
    if not_ready:
        raise SystemExit(not_ready)

    items = parse_items(args.items, args.items_file)
    sort = "gold" if args.gold else args.sort
    con = analyze.connect(args.sell)
    rows = find_snipes(con, args.sell, items=items, min_discount=args.min_discount,
                       min_gold=args.min_gold, max_gold=args.max_gold,
                       min_sell_now=args.min_sell_now,
                       max_appearance_sources=args.max_appearance_sources,
                       max_per_item=args.max_per_item,
                       top=args.top, sort=sort)
    print_snipes(rows, resolve_names=args.names)


if __name__ == "__main__":
    main()
