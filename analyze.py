#!/usr/bin/env python3
"""DuckDB summaries over snapshots + inferred-sale events.

  python analyze.py --cr-id 1096 summary [--top 30]
      Most-sold items: sales/day, sold-price percentiles, avg concurrent
      listings, turnover, and the current cheapest listing.

  python analyze.py --cr-id 1096 item 152510 [--price 2500000]
      Sold-price distribution for one item; --price (in COPPER) tells you what
      percentile a candidate snipe sits at among *actual* inferred sales.

  python analyze.py --cr-id 1096 trace 152510
      Every disappearance event for one item, in time order — how each vanished
      auction was classified. Built for the verification protocol: post/cancel/
      buy your test items, then trace them here to check the classifications.

Prices print in gold (1g = 10,000 copper). Item names: wowhead.com/item=<id>
"""
import argparse
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# SQL mirror of fetch_snapshot.market_key() -- see that function's docstring
# for why this is a duplicated implementation rather than a shared Python
# UDF (numpy dependency), and tests/test_market_key.py for the parity check
# (run against this exact constant, not a copy) that keeps the two in sync.
# Strips MARKET_IGNORE_MODIFIER_TYPES = {9, 42, 44} from the "m:..." segment
# of a bonus_key, in 4 ordered passes: (1) any occurrence with a leading
# comma, (2) a leading occurrence followed by more modifiers, (3) a lone
# occurrence that's the entire "m:" segment, (4) a bonus_key that's nothing
# but that lone occurrence (no "b:" part at all).
MARKET_KEY_MACRO_SQL = r"""
    CREATE OR REPLACE MACRO market_key(bk) AS
      regexp_replace(
        regexp_replace(
          regexp_replace(
            regexp_replace(bk, ',(9|42|44)=[0-9]+', '', 'g'),
          'm:(9|42|44)=[0-9]+,', 'm:', 'g'),
        '\|m:(9|42|44)=[0-9]+$', '', 'g'),
      '^m:(9|42|44)=[0-9]+$', '', 'g')
"""


def connect(cr: int) -> duckdb.DuckDBPyConnection:
    snaps = str(DATA / "snapshots" / str(cr) / "*.parquet")
    events = str(DATA / "events" / f"{cr}.parquet")
    con = duckdb.connect()
    con.execute(f"CREATE VIEW snaps AS SELECT * FROM '{snaps}'")
    con.execute(f"CREATE VIEW ev AS SELECT * FROM '{events}'")
    con.execute("CREATE VIEW sales AS SELECT * FROM ev WHERE classification = 'inferred_sale'")
    con.execute("""CREATE VIEW span AS
                   SELECT (max(snapshot_ts) - min(snapshot_ts)) / 86400.0 AS days,
                          count(DISTINCT snapshot_ts) AS n_snaps
                   FROM snaps""")
    con.execute(MARKET_KEY_MACRO_SQL)
    return con


def cmd_summary(con: duckdb.DuckDBPyConnection, top: int) -> None:
    con.sql(f"""
        WITH listed AS (
            SELECT item_id,
                   count(*) * 1.0 / (SELECT n_snaps FROM span) AS avg_listed
            FROM snaps WHERE buyout IS NOT NULL
            GROUP BY item_id
        ),
        now AS (
            SELECT item_id, min(buyout * 1.0 / quantity) AS cheapest_now
            FROM snaps
            WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM snaps)
              AND buyout IS NOT NULL
            GROUP BY item_id
        )
        SELECT s.item_id,
               count(*)                                          AS sales,
               round(count(*) / (SELECT days FROM span), 1)      AS per_day,
               round(median(s.unit_price) / 10000)               AS med_sold_g,
               round(quantile_cont(s.unit_price, 0.25) / 10000)  AS p25_g,
               round(l.avg_listed, 1)                            AS avg_listed,
               round(count(*) / (SELECT days FROM span)
                     / nullif(l.avg_listed, 0), 2)               AS turns_per_day,
               round(n.cheapest_now / 10000)                     AS cheapest_now_g
        FROM sales s
        LEFT JOIN listed l USING (item_id)
        LEFT JOIN now n USING (item_id)
        GROUP BY s.item_id, l.avg_listed, n.cheapest_now
        ORDER BY sales DESC
        LIMIT {int(top)}
    """).show()
    print("item names: https://www.wowhead.com/item=<item_id>")


def cmd_item(con: duckdb.DuckDBPyConnection, item_id: int, price: float | None) -> None:
    con.sql(f"""
        SELECT count(*)                                        AS sales,
               round(min(unit_price) / 10000)                  AS min_g,
               round(quantile_cont(unit_price, 0.25) / 10000)  AS p25_g,
               round(median(unit_price) / 10000)               AS med_g,
               round(quantile_cont(unit_price, 0.75) / 10000)  AS p75_g,
               round(max(unit_price) / 10000)                  AS max_g
        FROM sales WHERE item_id = {int(item_id)}
    """).show()
    if price is not None:
        pct = con.execute(f"""
            SELECT round(100.0 * count(*) FILTER (WHERE unit_price <= {float(price)})
                          / nullif(count(*), 0), 1)
            FROM sales WHERE item_id = {int(item_id)}
        """).fetchone()[0]
        if pct is None:
            print("no inferred sales for this item yet")
        else:
            print(f"{price / 10000:g}g sits at the {pct}th percentile of inferred "
                  f"SOLD prices (lower = better snipe)")


def cmd_trace(con: duckdb.DuckDBPyConnection, item_id: int) -> None:
    con.sql(f"""
        SELECT strftime(to_timestamp(ts), '%Y-%m-%d %H:%M') AS vanished_at_utc,
               auction_id,
               classification,
               round(unit_price / 10000.0, 2)               AS unit_g,
               quantity,
               time_left                                    AS last_seen_bucket,
               gap_seconds
        FROM ev WHERE item_id = {int(item_id)}
        ORDER BY ts, auction_id
    """).show()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cr-id", type=int, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("--top", type=int, default=30)
    i = sub.add_parser("item")
    i.add_argument("item_id", type=int)
    i.add_argument("--price", type=float, help="candidate price in copper")
    t = sub.add_parser("trace")
    t.add_argument("item_id", type=int)
    args = ap.parse_args()

    con = connect(args.cr_id)
    if args.cmd == "summary":
        cmd_summary(con, args.top)
    elif args.cmd == "item":
        cmd_item(con, args.item_id, args.price)
    else:
        cmd_trace(con, args.item_id)


if __name__ == "__main__":
    main()
