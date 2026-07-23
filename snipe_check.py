#!/usr/bin/env python3
"""Join region-scanner listings against a sell realm's inferred sold-price
percentiles to flag validated snipes: a listing on some other EU realm priced
well below what the item reliably sells for on your sell realm, net of the 5%
AH cut.

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
  python snipe_check.py --sell 1403 --sort per_day  # sort by liquidity

Requires: diff_snapshots.py already run for --sell (data/events/{sell}.parquet)
and scan_region.py already run (data/listings/*.parquet).
"""
import argparse
from pathlib import Path

import duckdb

import analyze
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
    "per_day": "per_day DESC",
}

CAVEAT = ("NOTE: an AH listing is guaranteed unsoulbound (BoP items can't be "
          "listed), so it can ride the warband bank to your sell realm -- just "
          "don't equip/use it before moving it, or it may bind to that character.")


def find_snipes(con: duckdb.DuckDBPyConnection, sell_cr: int, *,
                items: list[int] | None = None, min_discount: float = 0.3,
                min_per_day: float = 0.5, sell_percentile: float = 0.25,
                min_gold: float | None = None, max_gold: float | None = None,
                min_sales: int = 2,
                top: int = 50, sort: str = "discount") -> list[dict]:
    """Sold-price stats come from `sales` (a view over inferred_sale events,
    set up by analyze.connect). Listings come from every scanned realm except
    the sell realm itself. Match key is (item_id, bonus_key, pet_species_id,
    pet_quality_id) -- bonus_key alone is empty for every caged pet (82800),
    so without the pet identity fields every pet species/quality would
    collapse into one bucket and get compared against whichever pet actually
    sold. The pet fields are NULL for non-pet items, so this is a no-op for
    ordinary gear -- joined with IS NOT DISTINCT FROM since NULL = NULL is
    false in SQL and a plain USING join would drop every non-pet match.

    min_sales is a data-quality floor, distinct from min_per_day: a brand
    new sell realm with a short collection history can pass min_per_day on
    the strength of a single inferred_sale (count / a small `days` divisor
    rounds up fast), and inferred_sale can't tell a real sale apart from a
    cancel-without-relist (see CLAUDE.md's "known blind spot"). A lone
    troll/decoy listing -- posted at a joke price, then cancelled -- becomes
    the *entire* sold-price percentile with nothing to dilute it. Caught live
    2026-07-23: Onyxia Scale Cloak (item 15138) on Draenor had exactly one
    recorded inferred_sale, at 99,624g for an item that actually sells around
    444g, and that single bogus sample became its sell_price with no averaging
    to smooth it out. min_sales >= 2 doesn't eliminate the risk (two troll
    listings can still both be bogus) but it stops a single unverified sample
    from being trusted as a price signal outright."""
    item_filter = f"AND item_id IN ({','.join(map(str, items))})" if items else ""
    # Filters on the buy-side price -- what you'd actually spend on the
    # snipe -- since that's the number an "AH sniper" budget cap means.
    price_filter = ""
    if min_gold is not None:
        price_filter += f" AND b.buy_unit_price >= {float(min_gold) * 10000}"
    if max_gold is not None:
        price_filter += f" AND b.buy_unit_price <= {float(max_gold) * 10000}"
    con.execute(f"""
        CREATE OR REPLACE VIEW listings AS
        SELECT * FROM read_parquet('{(DATA / "listings" / "*.parquet").as_posix()}')
        WHERE cr_id != {int(sell_cr)} AND buyout IS NOT NULL
    """)
    res = con.execute(f"""
        WITH sell_stats AS (
            SELECT item_id, bonus_key, pet_species_id, pet_quality_id,
                   count(*)                                            AS sales,
                   round(count(*) / (SELECT days FROM span), 2)        AS per_day,
                   quantile_cont(unit_price, {float(sell_percentile)}) AS sell_price
            FROM sales
            WHERE 1=1 {item_filter}
            GROUP BY item_id, bonus_key, pet_species_id, pet_quality_id
            HAVING per_day >= {float(min_per_day)} AND sales >= {int(min_sales)}
        ),
        buy AS (
            SELECT cr_id, item_id, bonus_key, pet_species_id, pet_quality_id, auction_id,
                   buyout * 1.0 / quantity AS buy_unit_price
            FROM listings
            WHERE 1=1 {item_filter}
        )
        SELECT b.cr_id                                            AS buy_realm,
               b.item_id, b.bonus_key, b.pet_species_id, b.pet_quality_id, b.auction_id,
               round(b.buy_unit_price / 10000, 2)                  AS buy_g,
               round(s.sell_price / 10000, 2)                      AS sell_p_g,
               round(b.buy_unit_price)::BIGINT                     AS buy_copper,
               round(s.sell_price)::BIGINT                         AS sell_copper,
               s.per_day,
               round(100.0 * (s.sell_price * 0.95 - b.buy_unit_price)
                     / (s.sell_price * 0.95), 1)                   AS discount_pct
        FROM buy b
        JOIN sell_stats s
          ON b.item_id = s.item_id
         AND b.bonus_key IS NOT DISTINCT FROM s.bonus_key
         AND b.pet_species_id IS NOT DISTINCT FROM s.pet_species_id
         AND b.pet_quality_id IS NOT DISTINCT FROM s.pet_quality_id
        WHERE (s.sell_price * 0.95 - b.buy_unit_price) / (s.sell_price * 0.95)
              >= {float(min_discount)}
              {price_filter}
        ORDER BY {SORT_COLUMNS[sort]}
        LIMIT {int(top)}
    """)
    cols = [d[0] for d in res.description]
    return [dict(zip(cols, row)) for row in res.fetchall()]


def print_snipes(rows: list[dict], resolve_names: bool = False) -> None:
    if not rows:
        print("no snipes found at these thresholds")
        return
    names = NameCache() if resolve_names else None
    name_col = "name" if resolve_names else "item_id"
    hdr = f"{'buy_realm':>9} {name_col:>28} {'variant':>14} {'buy_g':>10} {'sell_p_g':>10} {'per_day':>8} {'disc%':>7}"
    print(hdr)
    for r in rows:
        if r["pet_species_id"] is not None:
            variant = f"pet:{r['pet_species_id']}/{r['pet_quality_id']}"
        else:
            variant = r["bonus_key"] or "-"
        label = names.get(r["item_id"], r["pet_species_id"]) if names else str(r["item_id"])
        print(f"{r['buy_realm']:>9} {label:>28.28} {variant:>14} "
              f"{r['buy_g']:>10.2f} {r['sell_p_g']:>10.2f} {r['per_day']:>8.2f} "
              f"{r['discount_pct']:>7.1f}")
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
                    help="minimum discount vs sell-realm price net of 5%% cut (default 0.3)")
    ap.add_argument("--min-per-day", type=float, default=0.5,
                    help="minimum inferred sales/day on the sell realm (liquidity floor)")
    ap.add_argument("--sell-percentile", type=float, default=0.25,
                    help="percentile of sold prices to require the discount against (default 0.25)")
    ap.add_argument("--min-gold", type=float, default=None,
                    help="minimum buy price in gold (budget floor, skips trivial junk)")
    ap.add_argument("--max-gold", type=float, default=None,
                    help="maximum buy price in gold (budget ceiling)")
    ap.add_argument("--min-sales", type=int, default=2,
                    help="minimum number of inferred sales required to trust the sold-price "
                         "percentile (default 2 -- a single sale can be an unverified "
                         "cancel-without-relist false positive, e.g. a troll-priced decoy)")
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
                       min_per_day=args.min_per_day, sell_percentile=args.sell_percentile,
                       min_gold=args.min_gold, max_gold=args.max_gold,
                       min_sales=args.min_sales,
                       top=args.top, sort=sort)
    print_snipes(rows, resolve_names=args.names)


if __name__ == "__main__":
    main()
