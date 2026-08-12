#!/usr/bin/env python3
"""Region-wide listing scan for gear carrying the **+Speed tertiary stat**
(bonus-list id 42), added 2026-08-12 as an experimental, *additive*
operation.

**This does not touch the snipe pipeline.** `snipe_check.py`'s pricing and
matching are unchanged and this module is never called from them. It reads
the same `data/listings/*.parquet` the region scanner already writes, and
nothing else -- notably it needs **no sell realm and no snapshots**, so it
works without `analyze.connect()`/`check_data_ready()` and can't be broken
by, or break, anything on the snipe path.

Why it exists: the snipe pricing model (2026-07-26) matches purely on
`item_id`, pooling every bonus/ilvl variant of an item into one market
priced at the sell realm's overall cheapest listing. That is a deliberate
product decision (see CLAUDE.md) -- but it means a tertiary stat is
*invisible* to pricing. A +Speed piece is priced as if it were the plain
version, so the snipe path can never flag one as valuable, and typically
does the opposite: the +Speed listing looks *overpriced* against a cheap
plain baseline and is filtered out.

The size of that blind spot, measured over a real live sweep (2026-08-12,
2,471,830 listings, 8,751 of them +Speed across 1,153 items): the cheapest
+Speed listing of an item sat at a **median 11.4x** its cheapest plain
listing. Read that number with care -- plain listings are far more
numerous, so their minimum is naturally lower and part of the multiple is
sample-size asymmetry rather than a pure speed premium. It is directional
evidence that the premium is real and large, not a measured premium.

**Deliberately un-calibrated (human decision, 2026-08-12).** This module
flags nothing and filters nothing by default: pricing thresholds are
human-specified in this project, and the "is this actually cheap in gold"
validation was explicitly deferred to a later pass. It *lists* +Speed
listings and computes a reference (`speed_region_median`) and a `gap_x`
purely so the output can be sorted and hand-filtered. No cutoff is baked
in; `--min-gap` exists but has no default. Do not add one without the
human -- see CLAUDE.md's "Heuristics flag, never silently filter" and the
pricing-calibration guardrail.

**The product thesis, stated by the human the same day**: "the idea is to
snipe these +speed items, that people list without knowing the +speed adds
tons of value. So the price of the listing really doesn't matter." That is
why no reference price *validates* a row here -- the seller is assumed to
have mispriced by ignoring the tertiary entirely, so every +Speed listing
is a candidate and `gap_x` is context, not a gate. What the buyer actually
filters on is **what they'll pay** and **what they can use**: hence
`--max-gold`, `--armor` and `--quality`, and why the default sort became
`price` (cheapest first) rather than `gap`.

Prices are copper end to end (10,000 = 1 gold), same as everywhere else;
`--min-gold`/`--max-gold` are the only gold-denominated inputs and are
converted at the boundary.

No 5% AH cut is applied anywhere here -- unlike a snipe, this row makes no
buy-here/sell-there claim to take a cut out of. Adding one would imply a
validated sell price this operation deliberately doesn't have yet.

Usage:
  python speed_check.py                          # every +Speed listing, cheapest first
  python speed_check.py --tarnished --names      # Midnight "Tarnished Dawnlit" set only
  python speed_check.py --tarnished --armor leather --max-gold 500
  python speed_check.py --armor cloth,mail --quality green,blue
  python speed_check.py --name-contains "Dawnlit Corsair"
  python speed_check.py --top 100 --names        # resolve item names (Blizzard API)
  python speed_check.py --items 204414,204422
  python speed_check.py --min-gap 3              # opt-in filter, no default

Requires: scan_region.py already run (data/listings/*.parquet).
"""
import argparse
from pathlib import Path

import duckdb

from fetch_snapshot import parse_bonus_key
from item_names import LIVE_RESOLVE_DEADLINE_SECONDS, NameCache

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Tertiary-stat bonus-list ids. **Verified 2026-08-12**, not assumed: each
# id was rendered against a real item currently listed in our own scan data
# (item 241035, Arathi Soldier's Morningstar) and read back off the
# resulting tooltip -- 40 produced "+8 Avoidance", 41 "+8 Leech", 42 "+8
# Speed", 43 "Indestructible". Corroborated independently by the shape of
# our own live data: across 7,932 distinct bonus_keys carrying any of the
# four, **no bonus_key ever carried two of them** -- exactly the mutual
# exclusivity a tertiary stat must have, and the four ids stand ~20x above
# their neighbours in frequency (4,000-8,700 listings each vs ~250 for
# adjacent ids like 39/45).
#
# Only SPEED is used by this module (human scope decision, 2026-08-12); the
# other three are recorded here because they were verified in the same pass
# and the mapping is the expensive part to re-derive, not because anything
# reads them yet.
TERTIARY_BONUS_IDS = {40: "Avoidance", 41: "Leech", 42: "Speed", 43: "Indestructible"}

SPEED_BONUS_ID = 42

# Naming footgun, worth stating once: **42 here is a `b:` bonus-list id, a
# completely different namespace from modifier type 42** (`m:42=...`, the
# per-craft continuous stat roll that market_key() strips as noise -- see
# fetch_snapshot.MARKET_IGNORE_MODIFIER_TYPES). Same number, unrelated
# meanings. Everything in this module reads the `b:` segment only.


def has_speed(bonus_key: str | None) -> bool:
    """True if this listing's bonus_key carries the +Speed tertiary.

    Pure and exact-match: the id is compared against the parsed `b:` list
    element-wise, never as a substring of the raw key -- a substring test
    would match 142/420/1042 and quietly poison the whole feature.

    Kept in lockstep with SPEED_FILTER_SQL below by
    tests/test_speed_check.py's parity check, the same two-implementations-
    one-test convention market_key()/MARKET_KEY_MACRO_SQL already follow.
    """
    if not bonus_key:
        return False
    return str(SPEED_BONUS_ID) in parse_bonus_key(bonus_key)["bonus_ids"]


# DuckDB counterpart to has_speed(). Filtering ~2.5M listings in SQL rather
# than pulling them into Python is the difference between a sub-second
# query and a multi-second one, so the logic genuinely does exist twice --
# hence the parity test. Splits the `b:` segment on commas and casts each
# element, so matching is element-wise and exact, same as the Python side.
# A bonus_key with no `b:` segment (pets, plain items, `m:`-only keys)
# yields [NULL] here, which list_contains() reports as false.
SPEED_FILTER_SQL = (
    "list_contains("
    "  list_transform("
    "    str_split(regexp_extract(bonus_key, 'b:([0-9,]+)', 1), ','),"
    "    x -> try_cast(x AS INTEGER)"
    f"  ), {SPEED_BONUS_ID})"
)

# The Midnight gear set (2026-08-12, human request: "only tarnished items as
# they are newest from midnight").
#
# **Matched on "Tarnished Dawnlit", not bare "Tarnished"** -- checked against
# the real name cache before wiring this up, and "Tarnished" alone is not a
# clean signal for "new": it also matches 22 legacy items spanning vanilla to
# Legion (Tarnished Chain Vest 2379, Tarnished Plate Belt 25381, Tarnished
# Fanatic's Battlevest 94085, Tarnished Dreamkeeper's Gauntlets 141695,
# Tarnished Claymore, Tarnished Bastard Sword). None of those happened to
# carry a +Speed listing in the sweep this was built against -- so bare
# "Tarnished" would have looked correct today and started quietly mixing
# vanilla greys into a Midnight view the first time one got listed. The
# Midnight set is the 58 "Tarnished Dawnlit" items (ids 258908-258962 and
# 266207-266210), and that two-word phrase separates them cleanly.
TARNISHED_NAME_MATCH = "Tarnished Dawnlit"

# Armor/weapon buckets, as (item_class, item_subclass) from Blizzard's own
# item data. Confirmed against the real name cache for the Midnight set.
#
# **Cloaks are subclass 1 (Cloth)** -- all four Tarnished Dawnlit capes are,
# including "Commander's Cape", whose name themes it to plate. That's
# Blizzard's classification, not a mistake to correct here: filtering "cloth"
# genuinely does include every cloak, and the UI says so rather than silently
# re-bucketing them by name (which would be inventing data).
#
# Jewelry (rings/necks) is armor subclass 0 ("Misc"), which is why it gets its
# own bucket instead of being lumped under an armor type nobody wears it as.
ARMOR_TYPES = {
    "cloth": (4, 1),
    "leather": (4, 2),
    "mail": (4, 3),
    "plate": (4, 4),
    "jewelry": (4, 0),
    "shield": (4, 6),
    # Weapons are item_class 2 with ~15 subclasses (dagger/staff/warglaive/...);
    # the whole class is one bucket, since "which weapon type" isn't a
    # wearability constraint the way an armor class is.
    "weapon": (2, None),
}

# Quality names as Blizzard reports them, keyed by what a player calls them.
# The Midnight Tarnished set is entirely `green` (59/59 UNCOMMON, confirmed
# 2026-08-12) -- `blue` currently matches nothing in that set and no blue
# Midnight item carries a +Speed listing at all yet. Kept anyway so the view
# picks them up automatically if/when they appear, rather than needing a code
# change at exactly the moment they matter.
QUALITY_ALIASES = {
    "green": "UNCOMMON",
    "blue": "RARE",
    "epic": "EPIC",
    "purple": "EPIC",
    "white": "COMMON",
    "grey": "POOR",
    "gray": "POOR",
    "legendary": "LEGENDARY",
}

SORT_COLUMNS = {
    # Default (changed 2026-08-12, human clarification: "the price of the
    # listing really doesn't matter" -- i.e. no reference price needs to
    # validate a +Speed listing, because the seller mispriced it by not
    # accounting for the tertiary at all. What matters when buying is what
    # you pay, so the cheapest listings lead).
    "price": "unit_price ASC",
    # "How many times below the typical +Speed price for this item is this
    # listing" -- still computed and still sortable, but no longer the
    # organizing principle of the view.
    "gap": "gap_x DESC NULLS LAST",
    "price_desc": "unit_price DESC",
    "median": "speed_region_median DESC NULLS LAST",
    "item": "item_id ASC, unit_price ASC",
}

CAVEAT = ("NOTE: +Speed listings only, region-wide -- this is a listing "
          "census, not a validated snipe. No sell-realm price, no AH cut "
          "and no gold validation is applied: gap_x compares this listing "
          "to the median of the per-realm cheapest +Speed listings for the "
          "same item, which is thin for items listed on only a realm or "
          "two. An AH listing is always unsoulbound (BoP can't be listed), "
          "so it can ride the warband bank -- just don't equip it first.")


def check_data_ready() -> str | None:
    """Returns an error message if no region sweep has been collected yet,
    else None. Deliberately its own function rather than reusing
    snipe_check.check_data_ready(), which also requires a sell realm's
    snapshots -- this operation needs none."""
    if not any((DATA / "listings").glob("*.parquet")):
        return "data/listings/*.parquet not found -- run scan_region.py first"
    return None


def connect() -> duckdb.DuckDBPyConnection:
    """A connection with just the region-wide listing view. No snapshots,
    no events, no market_key macros -- none of that is on this path."""
    con = duckdb.connect()
    con.execute(f"""
        CREATE VIEW listings AS
        SELECT * FROM read_parquet('{(DATA / "listings" / "*.parquet").as_posix()}')
        WHERE buyout IS NOT NULL AND buyout > 0 AND quantity > 0
    """)
    return con


def speed_item_ids(con: duckdb.DuckDBPyConnection) -> list[int]:
    """The distinct item_ids that have at least one +Speed listing. Small by
    construction (1,153 on the sweep this was built against, out of ~180k
    listed items), which is what makes the name filter below affordable."""
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT item_id FROM listings WHERE {SPEED_FILTER_SQL}").fetchall()]


def resolve_item_filter(con: duckdb.DuckDBPyConnection, *,
                        name_contains: str | None = None,
                        qualities: list[str] | None = None,
                        armor_types: list[str] | None = None,
                        items: list[int] | None = None,
                        names: NameCache | None = None) -> list[int]:
    """Turn item *metadata* filters (name substring, quality, armor type)
    into the list of item_ids to hand find_speed_listings()'s `items` param.

    All three are catalog properties of the item, not of the listing, so
    they can't be expressed in the listing SQL at all -- they live in
    NameCache. Resolving them together in one pass means the metadata is
    fetched once regardless of how many filters are active.

    `qualities` accepts player-facing names ("green"/"blue"/"epic", see
    QUALITY_ALIASES) or raw Blizzard ones ("UNCOMMON"). `armor_types`
    accepts ARMOR_TYPES keys. Unknown values raise rather than silently
    matching nothing -- a typo'd filter that returns an empty page is much
    harder to diagnose than one that errors.

    Resolving the filter to ids up front -- rather than fetching rows and
    dropping the non-matching ones afterwards, the way snipe_check.py's
    `_filter_by_appearance`/`_filter_by_sale_rate` post-filters work -- is
    deliberate: a post-filter has to over-fetch (snipe_check widens its SQL
    limit 20x to compensate) and can still return fewer rows than asked for.
    Here the whole +Speed universe is only ~1,150 items, so resolving names
    once turns the name filter into a plain item_id filter and `top` keeps
    meaning exactly what it says.

    **Blocking**: NameCache falls back to live Blizzard calls on a miss, so
    every caller must run this off the event loop. ensure_many() prewarms
    concurrently under the same deadline the live API paths use.

    Unresolved items can't match (NameCache.get() returns "item <id>" for
    them), so a cold cache that times out mid-prewarm under-reports rather
    than over-reports. Matching is case-insensitive.
    """
    wanted_qualities = None
    if qualities:
        wanted_qualities = {QUALITY_ALIASES.get(q.casefold(), q).upper() for q in qualities}
        unknown = wanted_qualities - set(QUALITY_ALIASES.values())
        if unknown:
            raise ValueError(f"unknown quality {sorted(unknown)}; "
                             f"try {sorted(QUALITY_ALIASES)}")
    wanted_armor = None
    if armor_types:
        wanted_armor = [a.casefold() for a in armor_types]
        unknown = set(wanted_armor) - set(ARMOR_TYPES)
        if unknown:
            raise ValueError(f"unknown armor type {sorted(unknown)}; "
                             f"try {sorted(ARMOR_TYPES)}")

    names = names or NameCache()
    candidates = speed_item_ids(con)
    if items:
        wanted = set(items)
        candidates = [i for i in candidates if i in wanted]
    names.ensure_many(candidates, max_workers=24,
                      deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)

    needle = name_contains.casefold() if name_contains else None

    def keep(item_id: int) -> bool:
        if needle is not None and needle not in names.get(item_id).casefold():
            return False
        if wanted_qualities is not None and names.quality(item_id) not in wanted_qualities:
            return False
        if wanted_armor is not None:
            cls, sub = names.item_class(item_id), names.item_subclass(item_id)
            # A bucket with subclass None (weapons) matches the whole class.
            if not any(cls == c and (s is None or sub == s)
                       for c, s in (ARMOR_TYPES[a] for a in wanted_armor)):
                return False
        return True

    matched = [i for i in candidates if keep(i)]
    names.save()
    return matched


def resolve_name_filter(con: duckdb.DuckDBPyConnection, name_contains: str,
                        items: list[int] | None = None,
                        names: NameCache | None = None) -> list[int]:
    """Name-only shorthand for resolve_item_filter(). Kept because a name
    substring is the one filter used on its own often enough to deserve a
    positional call."""
    return resolve_item_filter(con, name_contains=name_contains, items=items, names=names)


def find_speed_listings(con: duckdb.DuckDBPyConnection, *,
                        items: list[int] | None = None,
                        min_gold: float | None = None,
                        max_gold: float | None = None,
                        min_gap: float | None = None,
                        top: int = 50, sort: str = "price") -> list[dict]:
    """Every current listing carrying the +Speed tertiary, with pricing
    *context* attached but no judgement applied.

    Context columns, and why each is shaped the way it is:

    - `speed_region_median`: the median of the **per-realm cheapest**
      +Speed listing for that item, not a median over every individual
      +Speed auction. Deliberately the same shape as
      snipe_check.find_snipes()'s `region_median_cheapest` (2026-07-27):
      medianing raw auctions over-weights whichever realm happens to have
      the most sellers, while the per-realm floor asks the more honest
      question "what does this cost on a typical realm". Reused here rather
      than reinvented so the two numbers mean comparable things.
    - `speed_realm_count` / `speed_listing_count`: how thin that median is.
      A gap_x computed from two realms is nearly meaningless and the caller
      is expected to see that; this is the "flag, never silently filter"
      convention -- a thin reference is surfaced, not dropped.
    - `plain_cheapest`: the cheapest **non**-Speed listing of the same item
      region-wide. This is the number the snipe path would effectively
      price the item at, so `unit_price` vs `plain_cheapest` shows directly
      whether the seller priced the tertiary in at all.
    - `gap_x`: speed_region_median / unit_price. NULL when the item has no
      other realm to compare against (a single-realm item has no reference,
      and inventing one -- falling back to plain_cheapest, say -- would be
      exactly the un-mandated pricing decision this module is avoiding).

    Nothing here is filtered on any of those unless the caller explicitly
    passes min_gap/min_gold/max_gold. Default output is a census.
    """
    if sort not in SORT_COLUMNS:
        raise ValueError(f"sort must be one of {sorted(SORT_COLUMNS)}")

    item_filter = ""
    if items:
        item_filter = f"AND item_id IN ({','.join(str(int(i)) for i in items)})"

    # Gold -> copper at the boundary, per the project-wide rule that prices
    # are copper everywhere internally.
    price_filter = ""
    if min_gold is not None:
        price_filter += f" AND unit_price >= {float(min_gold) * 10_000}"
    if max_gold is not None:
        price_filter += f" AND unit_price <= {float(max_gold) * 10_000}"
    gap_filter = ""
    if min_gap is not None:
        gap_filter = f" AND gap_x >= {float(min_gap)}"

    sql = f"""
        WITH tagged AS (
            SELECT cr_id, item_id, auction_id, bonus_key, buyout, quantity,
                   buyout * 1.0 / quantity AS unit_price,
                   {SPEED_FILTER_SQL} AS is_speed
            FROM listings
        ),
        speed_rows AS (
            SELECT * FROM tagged WHERE is_speed
        ),
        speed_realm_floor AS (
            -- Cheapest +Speed listing per realm per item; the reference is
            -- built from these, not from raw auctions (see docstring).
            SELECT item_id, cr_id, min(unit_price) AS realm_cheapest
            FROM speed_rows GROUP BY item_id, cr_id
        ),
        speed_stats AS (
            SELECT item_id,
                   median(realm_cheapest) AS speed_region_median,
                   count(*) AS speed_realm_count
            FROM speed_realm_floor GROUP BY item_id
        ),
        speed_counts AS (
            SELECT item_id, count(*) AS speed_listing_count
            FROM speed_rows GROUP BY item_id
        ),
        plain AS (
            -- The same item WITHOUT the tertiary: what the snipe path's
            -- item_id-only pooling would effectively price it at.
            SELECT item_id, min(unit_price) AS plain_cheapest
            FROM tagged WHERE NOT is_speed GROUP BY item_id
        ),
        joined AS (
            SELECT s.cr_id, s.item_id, s.auction_id, s.bonus_key,
                   s.buyout, s.quantity, s.unit_price,
                   st.speed_region_median, st.speed_realm_count,
                   sc.speed_listing_count, p.plain_cheapest,
                   CASE WHEN st.speed_realm_count > 1 AND s.unit_price > 0
                        THEN st.speed_region_median / s.unit_price END AS gap_x
            FROM speed_rows s
            JOIN speed_stats st USING (item_id)
            JOIN speed_counts sc USING (item_id)
            LEFT JOIN plain p USING (item_id)
            WHERE 1=1 {item_filter} {price_filter}
        )
        SELECT * FROM joined
        WHERE 1=1 {gap_filter}
        ORDER BY {SORT_COLUMNS[sort]}
        LIMIT {int(top)}
    """
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def print_speed_listings(rows: list[dict], resolve_names: bool = False) -> None:
    names = None
    if resolve_names:
        names = NameCache()
        names.ensure_many([r["item_id"] for r in rows], max_workers=24,
                          deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
    if not rows:
        print("no +Speed listings found")
        return

    def gold(copper) -> str:
        """Cheap listings are the whole point of this report, so sub-gold
        amounts keep two decimals rather than rounding to a misleading
        "0" -- several real rows sit under 1g."""
        if copper is None:
            return "-"
        g = copper / 10_000
        return f"{g:,.0f}" if g >= 10 else f"{g:,.2f}"

    header = f"{'item':>8}  {'name':<34}  {'realm':>6}  {'price(g)':>11}  {'typical(g)':>11}  {'gap':>9}  {'plain(g)':>10}  {'n':>3}"
    print(header)
    print("-" * len(header))
    for r in rows:
        name = names.get(r["item_id"])[:34] if names else ""
        gap = f"{r['gap_x']:,.1f}x" if r["gap_x"] else "-"
        print(f"{r['item_id']:>8}  {name:<34}  {r['cr_id']:>6}  "
              f"{gold(r['unit_price']):>11}  {gold(r['speed_region_median']):>11}  "
              f"{gap:>9}  {gold(r['plain_cheapest']):>10}  {r['speed_realm_count']:>3}")
    if names:
        names.save()
    print()
    print(CAVEAT)


def main() -> None:
    ap = argparse.ArgumentParser(description="List region-wide auction listings carrying the "
                                             "+Speed tertiary stat (experimental)")
    ap.add_argument("--items", help="comma-separated item ids to restrict to")
    ap.add_argument("--name-contains", metavar="TEXT",
                    help="only items whose name contains TEXT (case-insensitive)")
    ap.add_argument("--tarnished", action="store_true",
                    help=f"only the Midnight '{TARNISHED_NAME_MATCH}' set "
                         "(shortcut for --name-contains)")
    ap.add_argument("--armor", metavar="TYPES",
                    help=f"comma-separated armor types: {','.join(sorted(ARMOR_TYPES))} "
                         "(note: cloaks are classed as cloth by Blizzard)")
    ap.add_argument("--quality", metavar="TIERS",
                    help="comma-separated quality tiers, e.g. green,blue")
    ap.add_argument("--min-gold", type=float, help="only listings at or above this unit price")
    ap.add_argument("--max-gold", type=float, help="only listings at or below this unit price")
    ap.add_argument("--min-gap", type=float,
                    help="only listings at least this many times below the item's typical "
                         "+Speed price (opt-in; deliberately no default -- see module docstring)")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--sort", default="price", choices=sorted(SORT_COLUMNS))
    ap.add_argument("--names", action="store_true",
                    help="resolve item names via the Blizzard API (slow on a cold cache)")
    args = ap.parse_args()

    not_ready = check_data_ready()
    if not_ready:
        raise SystemExit(not_ready)

    items = [int(x) for x in args.items.split(",") if x.strip()] if args.items else None
    name_contains = TARNISHED_NAME_MATCH if args.tarnished else args.name_contains
    armor = [a for a in (args.armor or "").split(",") if a.strip()] or None
    quality = [q for q in (args.quality or "").split(",") if q.strip()] or None
    con = connect()
    try:
        if name_contains or armor or quality:
            try:
                items = resolve_item_filter(con, name_contains=name_contains,
                                            qualities=quality, armor_types=armor,
                                            items=items)
            except ValueError as e:
                raise SystemExit(str(e))
            if not items:
                raise SystemExit("no +Speed listings match those item filters")
        rows = find_speed_listings(con, items=items, min_gold=args.min_gold,
                                   max_gold=args.max_gold, min_gap=args.min_gap,
                                   top=args.top, sort=args.sort)
    finally:
        # Explicit close, same reasoning as dashboard.api_snipes()'s
        # _run_query (2026-08-01): DuckDB's native buffers for a scan this
        # wide aren't guaranteed back to the OS on dereference alone.
        con.close()
    print_speed_listings(rows, resolve_names=args.names)


if __name__ == "__main__":
    main()
