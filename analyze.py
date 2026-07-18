#!/usr/bin/env python3
"""DuckDB summaries over snapshots + inferred-sale events.

  python analyze.py --cr-id 1096 summary [--top 30]
      Most-sold items: sales/day, sold-price percentiles, avg concurrent
      listings, turnover, and the current cheapest listing.

  python analyze.py --cr-id 1096 item 152510 [--price 2500000]
      Sold-price distribution for one item; --price (in COPPER) tells you what
      percentile a candidate snipe sits at among *actual* inferred sales.

Prices print in gold (1g = 10,000 copper). Item names: wowhead.com/item=<id>
"""
import argparse
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


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
    args = ap.parse_args()

    con = connect(args.cr_id)
    if args.cmd == "summary":
        cmd_summary(con, args.top)
    else:
        cmd_item(con, args.item_id, args.price)


if __name__ == "__main__":
    main()
