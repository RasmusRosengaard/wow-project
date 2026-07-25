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
from fetch_snapshot import (
    BONUS_NOISE_BASE_TAG_FREQUENCY,
    BONUS_NOISE_COMPANION_THRESHOLD,
    BONUS_NOISE_LOW_FREQUENCY,
    BONUS_NOISE_MAX_EXCLUSIVE_GROUP,
    BONUS_NOISE_MIN_EXCLUSIVE_COVERAGE,
    BONUS_NOISE_MIN_SAMPLES,
)
from fetch_snapshot import market_key as _market_key
from item_names import NameCache

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def parse_items(items: str | None, items_file: str | None) -> list[int] | None:
    ids: list[int] = []
    if items:
        ids += [int(x) for x in items.split(",") if x.strip()]
    if items_file:
        ids += [int(x) for x in Path(items_file).read_text().split() if x.strip()]
    return ids or None


SORT_COLUMNS = {
    "discount": "discount_pct DESC",
    "gold": "sell_p_g DESC",
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

# Bounds the base_level half of a single _populate_market_keys() call's
# worst-case latency -- see its docstring. At max_workers=24 (comfortably
# under Blizzard's 100 req/s ceiling), 500 items resolves in single-digit
# seconds even fully cold, versus the 30-175s+ an unbounded, largely-
# uncached region-wide gather produced live 2026-07-25.
MAX_BASE_LEVEL_LOOKUPS_PER_CALL = 500


def _detect_noise_bonus_ids(con: duckdb.DuckDBPyConnection) -> dict[int, set[int]]:
    """Finds per-item `b:` bonus-list ids that are per-craft noise (a
    different "instance" id on every listing of what's really one liquid
    market, e.g. item 36507/Iron-Molded Fist) rather than a real distinguishing
    variant (e.g. item 109168's two-part gem bonus, or item 244752's
    five-value item-level-upgrade track). Unlike a modifier type, a raw
    bonus id isn't a typed field -- the same id can mean something
    completely different on a different item -- so this can't be a fixed
    global ignore-list; it's computed fresh per item from the data itself.

    Determined via a structural test, not a frequency threshold alone --
    **a frequency-only cutoff was tried first, shipped, and live-confirmed
    insufficient the same day (2026-07-24)**: item 36507's own noise
    reached 14% frequency, overlapping with other items' real dimensions
    also observed as low as 14-17%, so no single band separates the two.
    The real distinguishing signal is whether a value has a *partner*: a
    real dimension either reliably co-occurs with another specific value in
    the same bonus_key (a companion pair), or belongs to a small set of
    mutually-exclusive values that jointly cover most of the item's
    listings (a partition). Per-craft noise has neither shape: each value
    stands alone. See fetch_snapshot.py's BONUS_NOISE_* constants for the
    full methodology and the real-data validation (10 real items, both
    noise-heavy and multi-dimension-real, checked live).

    Materialized as real temp tables (occ_id assigned once), not CTEs
    referenced twice -- a CTE referencing row_number() OVER () and read
    from two places in the same query was live-confirmed to NOT give
    stable, matching row ids across both reads (DuckDB re-evaluated it
    independently each time), silently corrupting the co-occurrence
    computation below. A materialized table has one fixed occ_id per row,
    read consistently everywhere."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE all_bonus AS
        SELECT item_id, bonus_key FROM sales
        UNION ALL
        SELECT item_id, bonus_key FROM snaps
        WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM snaps) AND buyout IS NOT NULL
        UNION ALL
        SELECT item_id, bonus_key FROM listings
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE bonus_occurrences AS
        SELECT row_number() OVER () AS occ_id, item_id, bonus_key
        FROM all_bonus WHERE bonus_key LIKE 'b:%'
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE noise_bonus_values AS
        SELECT bo.occ_id, bo.item_id, CAST(t.v AS BIGINT) AS bonus_id
        FROM bonus_occurrences bo,
             UNNEST(string_split(regexp_extract(bo.bonus_key, '^b:([0-9,]+)', 1), ',')) AS t(v)
        WHERE length(t.v) > 0
    """)
    noise_rows = con.execute(f"""
        WITH item_totals AS (
            SELECT item_id, count(*) AS n FROM bonus_occurrences GROUP BY item_id
        ),
        value_freq AS (
            SELECT bv.item_id, bv.bonus_id, count(*) AS doc_count
            FROM noise_bonus_values bv GROUP BY bv.item_id, bv.bonus_id
        ),
        classified AS (
            SELECT vf.item_id, vf.bonus_id, vf.doc_count, it.n, vf.doc_count * 1.0 / it.n AS freq
            FROM value_freq vf JOIN item_totals it ON vf.item_id = it.item_id
            WHERE it.n >= {BONUS_NOISE_MIN_SAMPLES}
        ),
        -- The "ambiguous band": not rare enough to be automatic noise, not
        -- frequent enough to be an automatic real base tag. Every value
        -- here needs a partner (below) to be trusted as real.
        ambiguous AS (
            SELECT * FROM classified
            WHERE freq >= {BONUS_NOISE_LOW_FREQUENCY} AND freq < {BONUS_NOISE_BASE_TAG_FREQUENCY}
        ),
        -- Pairwise co-occurrence between ambiguous-band values only (a
        -- true base tag is deliberately excluded from ever being "the
        -- other side" of a pairing check -- see BASE_TAG_FREQ's comment).
        cooc AS (
            SELECT bv1.item_id, bv1.bonus_id AS id_a, bv2.bonus_id AS id_b, count(*) AS co_count
            FROM noise_bonus_values bv1
            JOIN noise_bonus_values bv2
              ON bv1.occ_id = bv2.occ_id AND bv1.bonus_id != bv2.bonus_id
            JOIN ambiguous a1 ON bv1.item_id = a1.item_id AND bv1.bonus_id = a1.bonus_id
            JOIN ambiguous a2 ON bv2.item_id = a2.item_id AND bv2.bonus_id = a2.bonus_id
            GROUP BY bv1.item_id, bv1.bonus_id, bv2.bonus_id
        ),
        -- (a) companion: value A reliably co-occurs together WITH some
        -- other ambiguous value B in the same bonus_key (e.g. a two-part
        -- gem/socket bonus).
        companions AS (
            SELECT DISTINCT c.item_id, c.id_a AS bonus_id
            FROM cooc c JOIN ambiguous ca ON c.item_id = ca.item_id AND c.id_a = ca.bonus_id
            WHERE c.co_count * 1.0 / ca.doc_count >= {BONUS_NOISE_COMPANION_THRESHOLD}
        ),
        -- (b) partition: value A's "exclusive partners" are the other
        -- ambiguous values that NEVER co-occur with it. If that set is
        -- small and A plus its exclusive partners jointly explain most of
        -- the item's listings, this is a real small enum/tier system
        -- (e.g. a 2-way quality flag, or a 5-way item-level-upgrade
        -- track) -- not a big per-craft pool that also happens to never
        -- repeat within one listing.
        exclusive_pairs AS (
            SELECT a1.item_id, a1.bonus_id AS id_a, a2.bonus_id AS id_b, a2.doc_count
            FROM ambiguous a1
            JOIN ambiguous a2 ON a1.item_id = a2.item_id AND a1.bonus_id != a2.bonus_id
            LEFT JOIN cooc c
              ON c.item_id = a1.item_id AND c.id_a = a1.bonus_id AND c.id_b = a2.bonus_id
            WHERE c.co_count IS NULL
        ),
        exclusive_groups AS (
            SELECT ep.item_id, ep.id_a AS bonus_id, count(*) AS group_size,
                   (a1.doc_count + sum(ep.doc_count)) * 1.0 / a1.n AS coverage
            FROM exclusive_pairs ep
            JOIN ambiguous a1 ON ep.item_id = a1.item_id AND ep.id_a = a1.bonus_id
            GROUP BY ep.item_id, ep.id_a, a1.doc_count, a1.n
        ),
        partitions AS (
            SELECT item_id, bonus_id FROM exclusive_groups
            WHERE group_size <= {BONUS_NOISE_MAX_EXCLUSIVE_GROUP}
              AND coverage >= {BONUS_NOISE_MIN_EXCLUSIVE_COVERAGE}
        )
        SELECT a.item_id, a.bonus_id FROM ambiguous a
        LEFT JOIN companions co ON a.item_id = co.item_id AND a.bonus_id = co.bonus_id
        LEFT JOIN partitions p ON a.item_id = p.item_id AND a.bonus_id = p.bonus_id
        WHERE co.bonus_id IS NULL AND p.bonus_id IS NULL
        UNION
        SELECT item_id, bonus_id FROM classified WHERE freq < {BONUS_NOISE_LOW_FREQUENCY}
    """).fetchall()
    noise_by_item: dict[int, set[int]] = {}
    for item_id, bonus_id in noise_rows:
        noise_by_item.setdefault(item_id, set()).add(bonus_id)
    return noise_by_item


def _resolve_base_levels(con: duckdb.DuckDBPyConnection) -> dict[int, int | None]:
    """Resolves each candidate item's catalog base level, needed by
    market_key()'s type-28 ilvl-plausibility pooling -- a Blizzard API call
    DuckDB can't make mid-query. `NameCache.ensure_many()` resolves these
    concurrently, capped at MAX_BASE_LEVEL_LOOKUPS_PER_CALL -- live-
    confirmed 2026-07-25 the type-28 prefilter's candidate set can be
    15,000+ items on a single sell realm (type 28 is ubiquitous on modern
    ilvl-scaling gear, not rare), so an unbounded gather made a single
    request take 30-175s+ even off the event loop (see CLAUDE.md's
    "Real production outage" section for the full incident). sell_ids
    (sales+snaps, this realm) is prioritized over listings_ids (region-
    wide) within the budget since it drives this query's own sold-price
    grouping. Items beyond the cap simply get no base_level this call --
    market_key() treats an unknown base_level as "don't strip, never
    assume junk," so this degrades gracefully, not incorrectly.
    collect_all.py additionally pre-warms this cache in the background so
    convergence doesn't depend on user traffic."""
    sell_ids = [item_id for (item_id,) in con.execute(r"""
        SELECT DISTINCT item_id FROM sales WHERE bonus_key LIKE '%28=%'
        UNION
        SELECT DISTINCT item_id FROM snaps
        WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM snaps)
          AND buyout IS NOT NULL AND bonus_key LIKE '%28=%'
    """).fetchall()]
    listings_ids = [item_id for (item_id,) in con.execute(
        "SELECT DISTINCT item_id FROM listings WHERE bonus_key LIKE '%28=%'"
    ).fetchall()]
    type28_ids = list(dict.fromkeys(sell_ids + listings_ids))  # dedup, sell_ids first
    names = NameCache()
    names.ensure_many(type28_ids, max_workers=24, limit=MAX_BASE_LEVEL_LOOKUPS_PER_CALL)
    return {item_id: names.base_level(item_id) for item_id in type28_ids}


def _load_market_key_table(con: duckdb.DuckDBPyConnection,
                           noise_by_item: dict[int, set[int]],
                           base_levels: dict[int, int | None]) -> None:
    """Computes market_key() in Python for every distinct (item_id,
    bonus_key) pair the query will touch, then bulk-loads the results into
    a `bonus_key_market_keys` temp table the main query JOINs against on
    (item_id, bonus_key) -- replacing inline SQL macro calls
    (`market_key(bonus_key, base_level)`) entirely for this query, since a
    per-item variable-length "strip these specific ids" set (noise_by_item)
    doesn't fit DuckDB's macro model as cleanly as a single scalar
    base_level does.

    **Why a bulk Arrow load, not executemany()**: live-timed 2026-07-25 --
    ~700k distinct (item_id, bonus_key) pairs is a realistic real-world
    count (Draenor). `executemany()` inserting them row-by-row did not
    return within 90s. Registering a pyarrow Table and `CREATE TABLE AS
    SELECT` from it ingests natively; the same 700k rows loaded in 0.23s."""
    pairs = con.execute("""
        SELECT DISTINCT item_id, bonus_key FROM sales
        UNION
        SELECT DISTINCT item_id, bonus_key FROM snaps
        WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM snaps) AND buyout IS NOT NULL
        UNION
        SELECT DISTINCT item_id, bonus_key FROM listings
    """).fetchall()
    item_ids = [p[0] for p in pairs]
    bonus_keys = [p[1] for p in pairs]
    market_keys = [
        _market_key(bk, base_levels.get(iid),
                    frozenset(noise_by_item[iid]) if iid in noise_by_item else None)
        for iid, bk in pairs
    ]

    tbl = pa.table({"bkm_item_id": item_ids, "bkm_bonus_key": bonus_keys, "bkm_market_key": market_keys})
    con.register("_bkm_source", tbl)
    con.execute("CREATE OR REPLACE TEMP TABLE bonus_key_market_keys AS SELECT * FROM _bkm_source")
    con.unregister("_bkm_source")


def _populate_market_keys(con: duckdb.DuckDBPyConnection) -> None:
    """Populates the `bonus_key_market_keys` temp table find_snipes() joins
    against for every distinct (item_id, bonus_key) pair. Three steps, each
    independently documented on its own helper: noise-bonus-id detection
    (per-craft `b:` bonus ids that fragment one market into many, see
    _detect_noise_bonus_ids), base_level resolution (needed for type-28
    ilvl-plausibility pooling, see _resolve_base_levels), then computing
    and bulk-loading the final market_key per pair (see
    _load_market_key_table). Renamed from `_populate_base_levels`
    (2026-07-25) once noise-bonus-id detection joined base_level as a
    second responsibility -- split back into named steps here for
    readability (2026-07-25, same day, no behavior change).

    Noise detection + base_level resolution + market_key computation + bulk
    load together: ~1.3s total on a realistic (~700k pair) realm, live-
    verified."""
    noise_by_item = _detect_noise_bonus_ids(con)
    base_levels = _resolve_base_levels(con)
    _load_market_key_table(con, noise_by_item, base_levels)


def find_snipes(con: duckdb.DuckDBPyConnection, sell_cr: int, *,
                items: list[int] | None = None, min_discount: float = 0.3,
                min_gold: float | None = None, max_gold: float | None = None,
                min_sell_now: float | None = None,
                max_appearance_sources: int | None = None,
                max_per_item: int | None = None,
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
    Match key is (item_id, market_key(bonus_key), pet_species_id,
    pet_quality_id) -- market_key() (see fetch_snapshot.py) is a coarser
    version of bonus_key that pools near-identical crafted-item rolls and
    per-craft bonus-list noise into one market instead of matching the
    exact roll, since Blizzard's undocumented per-craft modifiers otherwise
    fragment what's really one liquid market into dozens of near-unique
    buckets. The raw bonus_key is still carried through on the buy-side row
    for display -- only the join/group keys use market_key. bonus_key/
    market_key are both empty for every caged pet (82800), so without the
    pet identity fields every pet species/quality would collapse into one
    bucket and get compared against whichever pet happens to be listed
    cheapest. The pet fields are NULL for non-pet items, so this is a no-op
    for ordinary gear -- joined with IS NOT DISTINCT FROM since NULL = NULL
    is false in SQL and a plain USING join would drop every non-pet match.

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

    Each returned row also carries `market_key` (added 2026-07-24,
    previously computed for the join/matching but excluded from the output).
    dashboard.html groups rows by this instead of the exact `bonus_key` --
    real listings across different realms often share one market_key (same
    price, same tradeable item) but never share an exact bonus_key (per-
    instance modifiers), so grouping by the exact string was splitting one
    genuinely-collapsible market into several separate table rows even
    though the price shown for each was already identical."""
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
    # When appearance-filtering, pull a wider SQL candidate pool since rows
    # get dropped *after* the query (the appearance cache is a local JSON
    # file, not something to join in SQL) -- otherwise a top-50 SQL LIMIT
    # could get filtered down to near-nothing before the appearance check
    # ever runs.
    sql_limit = int(top) if max_appearance_sources is None else max(int(top) * 20, 1000)
    con.execute(f"""
        CREATE OR REPLACE VIEW listings AS
        SELECT * FROM read_parquet('{(DATA / "listings" / "*.parquet").as_posix()}')
        WHERE cr_id != {int(sell_cr)} AND buyout IS NOT NULL
    """)
    _populate_market_keys(con)
    res = con.execute(f"""
        WITH sell_now AS (
            SELECT s.item_id, bkm.bkm_market_key AS market_key,
                   s.pet_species_id, s.pet_quality_id,
                   min(s.buyout * 1.0 / s.quantity) AS cheapest_now
            FROM snaps s
            LEFT JOIN bonus_key_market_keys bkm
              ON s.item_id = bkm.bkm_item_id AND s.bonus_key = bkm.bkm_bonus_key
            WHERE s.snapshot_ts = (SELECT max(snapshot_ts) FROM snaps)
              AND s.buyout IS NOT NULL
            GROUP BY s.item_id, bkm.bkm_market_key, s.pet_species_id, s.pet_quality_id
        ),
        buy AS (
            SELECT l.cr_id, l.item_id, l.bonus_key,
                   bkm.bkm_market_key AS market_key,
                   l.pet_species_id, l.pet_quality_id, l.auction_id,
                   l.buyout * 1.0 / l.quantity AS buy_unit_price
            FROM listings l
            LEFT JOIN bonus_key_market_keys bkm
              ON l.item_id = bkm.bkm_item_id AND l.bonus_key = bkm.bkm_bonus_key
            WHERE 1=1 {item_filter}
        ),
        matches AS (
            SELECT b.cr_id                                            AS buy_realm,
                   b.item_id, b.bonus_key, b.market_key, b.pet_species_id, b.pet_quality_id, b.auction_id,
                   round(b.buy_unit_price / 10000, 2)                  AS buy_g,
                   round(n.cheapest_now / 10000, 2)                    AS sell_p_g,
                   round(b.buy_unit_price)::BIGINT                     AS buy_copper,
                   round(n.cheapest_now)::BIGINT                       AS sell_copper,
                   round(100.0 * (n.cheapest_now * 0.95 - b.buy_unit_price)
                         / (n.cheapest_now * 0.95), 1)                 AS discount_pct
            FROM buy b
            JOIN sell_now n
              ON b.item_id = n.item_id
             AND b.market_key IS NOT DISTINCT FROM n.market_key
             AND b.pet_species_id IS NOT DISTINCT FROM n.pet_species_id
             AND b.pet_quality_id IS NOT DISTINCT FROM n.pet_quality_id
            WHERE (n.cheapest_now * 0.95 - b.buy_unit_price) / (n.cheapest_now * 0.95)
                  >= {float(min_discount)}
                  {price_filter}
        ),
        capped AS (
            SELECT *, ROW_NUMBER() OVER (
                -- market_key, not the raw bonus_key -- caps per pooled
                -- market (see market_key() above), so e.g. max_per_item=1
                -- on a crafted item keeps its single best-discount listing
                -- across all its near-identical crafted rolls, not one per
                -- exact roll.
                PARTITION BY item_id, market_key, pet_species_id, pet_quality_id
                ORDER BY discount_pct DESC
            ) AS item_rank
            FROM matches
        )
        SELECT * EXCLUDE (item_rank)
        FROM capped
        {f"WHERE item_rank <= {int(max_per_item)}" if max_per_item is not None else ""}
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
           f"{'disc%':>7} {'appear':>7}")
    print(hdr)
    for r in rows:
        if r["pet_species_id"] is not None:
            variant = f"pet:{r['pet_species_id']}/{r['pet_quality_id']}"
        else:
            variant = r["bonus_key"] or "-"
        label = names.get(r["item_id"], r["pet_species_id"]) if names else str(r["item_id"])
        appear = r["appearance_sources"] if r["appearance_sources"] is not None else "-"
        print(f"{r['buy_realm']:>9} {label:>28.28} {variant:>14} "
              f"{r['buy_g']:>10.2f} {r['sell_p_g']:>10.2f} "
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

    events_path = DATA / "events" / f"{args.sell}.parquet"
    if not events_path.exists():
        raise SystemExit(f"{events_path} not found -- run diff_snapshots.py --cr-id {args.sell} first")
    if not any((DATA / "listings").glob("*.parquet")):
        raise SystemExit("data/listings/*.parquet not found -- run scan_region.py first")

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
