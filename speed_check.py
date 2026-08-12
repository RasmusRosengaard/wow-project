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

Prices are copper end to end (10,000 = 1 gold), same as everywhere else;
`--min-gold`/`--max-gold` are the only gold-denominated inputs and are
converted at the boundary.

No 5% AH cut is applied anywhere here -- unlike a snipe, this row makes no
buy-here/sell-there claim to take a cut out of. Adding one would imply a
validated sell price this operation deliberately doesn't have yet.

Usage:
  python speed_check.py                          # every +Speed listing, biggest gap first
  python speed_check.py --top 100 --names        # resolve item names (Blizzard API)
  python speed_check.py --sort price --min-gold 5000
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

SORT_COLUMNS = {
    # Default. "How many times below the typical +Speed price for this item
    # is this listing" -- the sort the human asked for, with no cutoff
    # attached (see module docstring).
    "gap": "gap_x DESC NULLS LAST",
    "price": "unit_price ASC",
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


def find_speed_listings(con: duckdb.DuckDBPyConnection, *,
                        items: list[int] | None = None,
                        min_gold: float | None = None,
                        max_gold: float | None = None,
                        min_gap: float | None = None,
                        top: int = 50, sort: str = "gap") -> list[dict]:
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
    ap.add_argument("--min-gold", type=float, help="only listings at or above this unit price")
    ap.add_argument("--max-gold", type=float, help="only listings at or below this unit price")
    ap.add_argument("--min-gap", type=float,
                    help="only listings at least this many times below the item's typical "
                         "+Speed price (opt-in; deliberately no default -- see module docstring)")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--sort", default="gap", choices=sorted(SORT_COLUMNS))
    ap.add_argument("--names", action="store_true",
                    help="resolve item names via the Blizzard API (slow on a cold cache)")
    args = ap.parse_args()

    not_ready = check_data_ready()
    if not_ready:
        raise SystemExit(not_ready)

    items = [int(x) for x in args.items.split(",") if x.strip()] if args.items else None
    con = connect()
    try:
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
