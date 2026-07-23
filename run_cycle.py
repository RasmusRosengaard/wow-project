#!/usr/bin/env python3
"""One full pipeline cycle: poll the sell realm, sweep the region, then (once
there's enough sell-realm history) rebuild events and flag snipes. Does
exactly one pass and exits -- meant to be re-run on a recurring schedule
(Blizzard republishes roughly hourly), not looped internally itself.

Usage:
  python run_cycle.py --sell 1403
  python run_cycle.py --sell 1403 --items-file watchlist.txt --min-discount 0.3
"""
import argparse
import sys
from pathlib import Path

import analyze
import diff_snapshots
import fetch_snapshot
import scan_region
import snipe_check

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sell", type=int, required=True, help="sell-realm connected-realm id")
    ap.add_argument("--items", help="comma-separated item ids to restrict snipe-checking to")
    ap.add_argument("--items-file", help="file of item ids (whitespace/newline separated)")
    ap.add_argument("--min-discount", type=float, default=0.3)
    ap.add_argument("--min-per-day", type=float, default=0.5)
    ap.add_argument("--sell-percentile", type=float, default=0.25)
    ap.add_argument("--names", action="store_true",
                    help="resolve item ids to display names (see snipe_check.py --names)")
    args = ap.parse_args()

    fetch_snapshot.setup_logging()
    scan_region.setup_logging()

    print("== polling sell realm ==")
    p = fetch_snapshot.fetch_once(args.sell)
    print("no new snapshot yet" if p is None else f"wrote {p}")

    print("== scanning region ==")
    scan_region.sweep(exclude={args.sell})

    n_snaps = len(list((DATA / "snapshots" / str(args.sell)).glob("*.parquet")))
    if n_snaps < 2:
        print(f"only {n_snaps} sell-realm snapshot(s) so far -- need 2+ before diffing/snipe-checking")
        return

    print("== rebuilding events ==")
    orig_argv = sys.argv
    sys.argv = ["diff_snapshots.py", "--cr-id", str(args.sell)]
    try:
        diff_snapshots.main()
    finally:
        sys.argv = orig_argv

    print("== checking snipes ==")
    items = snipe_check.parse_items(args.items, args.items_file)
    con = analyze.connect(args.sell)
    rows = snipe_check.find_snipes(con, args.sell, items=items,
                                   min_discount=args.min_discount, min_per_day=args.min_per_day,
                                   sell_percentile=args.sell_percentile)
    snipe_check.print_snipes(rows, resolve_names=args.names)


if __name__ == "__main__":
    main()
