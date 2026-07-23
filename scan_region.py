#!/usr/bin/env python3
"""Sweep every EU connected realm's *current* auction listings into
data/listings/{cr_id}.parquet — latest snapshot only, overwritten each sweep.
No history, no diffing: this is the buy side of the cross-realm engine. Sale
inference (sold prices, sales/day) stays on fetch_snapshot.py + diff_snapshots.py,
run only against your chosen sell realm(s).

Usage:
  python scan_region.py                 # one sweep of every EU connected realm
  python scan_region.py --loop          # sweep every hour, forever
  python scan_region.py --exclude 1403  # skip realms already deep-collected
"""
import argparse
import logging
import logging.handlers
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from blizz import list_connected_realms
from fetch_snapshot import bonus_key, get_auctions_with_backoff

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

log = logging.getLogger("scanner")


def setup_logging() -> None:
    log_dir = DATA / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_h = logging.handlers.RotatingFileHandler(
        log_dir / "scanner.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console_h = logging.StreamHandler()
    for h in (file_h, console_h):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


LISTING_SCHEMA = pa.schema([
    ("cr_id", pa.int64()),
    ("fetched_ts", pa.int64()),       # epoch seconds this sweep ran
    ("auction_id", pa.int64()),
    ("item_id", pa.int64()),
    ("bonus_key", pa.string()),
    ("pet_species_id", pa.int64()),
    ("pet_quality_id", pa.int64()),
    ("pet_level", pa.int64()),
    ("buyout", pa.int64()),           # copper; None = bid-only (not insta-buyable)
    ("bid", pa.int64()),
    ("quantity", pa.int64()),
    ("time_left", pa.string()),
])


def rows(payload: dict, cr: int, ts: int) -> list[dict]:
    out = []
    for a in payload.get("auctions") or []:
        it = a.get("item") or {}
        out.append({
            "cr_id": cr,
            "fetched_ts": ts,
            "auction_id": a.get("id"),
            "item_id": it.get("id"),
            "bonus_key": bonus_key(it),
            "pet_species_id": it.get("pet_species_id"),
            "pet_quality_id": it.get("pet_quality_id"),
            "pet_level": it.get("pet_level"),
            "buyout": a.get("buyout"),
            "bid": a.get("bid"),
            "quantity": a.get("quantity", 1),
            "time_left": a.get("time_left"),
        })
    return out


def scan_one(cr: int, ts: int) -> int:
    """Fetch one realm's current listings and overwrite its parquet. Returns
    the row count written (0 if the body was malformed)."""
    r = get_auctions_with_backoff(cr, headers={})
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError:
        log.error("cr %s: malformed JSON body, skipping", cr)
        return 0

    table = pa.Table.from_pylist(rows(payload, cr, ts), schema=LISTING_SCHEMA)
    out_dir = DATA / "listings"
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_dir / f"{cr}.parquet", compression="zstd")
    return table.num_rows


def sweep(exclude: set[int]) -> None:
    realms = [cr for cr in list_connected_realms() if cr not in exclude]
    log.info("sweeping %s EU connected realms (%s excluded)", len(realms), len(exclude))
    ts = int(time.time())
    for cr in realms:
        try:
            n = scan_one(cr, ts)
            log.info("cr %s: %s listings", cr, n)
        except Exception:                # one bad realm shouldn't kill the sweep
            log.exception("cr %s: sweep failed, continuing", cr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exclude", default="",
                    help="comma-separated connected-realm ids to skip "
                         "(e.g. sell realms already covered by fetch_snapshot.py)")
    ap.add_argument("--loop", action="store_true", help="sweep every hour, forever")
    args = ap.parse_args()
    exclude = {int(x) for x in args.exclude.split(",") if x.strip()}

    setup_logging()

    if not args.loop:
        sweep(exclude)
        return

    log.info("region scanner running hourly; ctrl-c to stop")
    while True:
        try:
            sweep(exclude)
        except Exception:
            log.exception("sweep failed; retrying in 1h")
        time.sleep(3600)


if __name__ == "__main__":
    main()
