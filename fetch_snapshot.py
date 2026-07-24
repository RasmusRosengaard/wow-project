#!/usr/bin/env python3
"""Collect hourly auction-house snapshots for one connected realm into parquet files.

Usage:
  python fetch_snapshot.py --find silvermoon        # look up your connected-realm id
  python fetch_snapshot.py --cr-id 1096             # fetch once (only if new data)
  python fetch_snapshot.py --cr-id 1096 --loop      # poll every 10 min; leave running 48h+

Blizzard publishes a new AH dump roughly hourly; If-Modified-Since means the loop
only downloads when there's actually a new snapshot (~6 tiny requests/hour otherwise).
"""
import argparse
import json
import logging
import logging.handlers
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from blizz import api_get, find_connected_realm

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

log = logging.getLogger("collector")

# HTTP statuses worth retrying with backoff instead of failing the poll.
RETRYABLE = {429, 500, 502, 503, 504}


def setup_logging() -> None:
    log_dir = DATA / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_h = logging.handlers.RotatingFileHandler(
        log_dir / "collector.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console_h = logging.StreamHandler()
    for h in (file_h, console_h):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)

SCHEMA = pa.schema([
    ("snapshot_ts", pa.int64()),      # epoch seconds, taken from Last-Modified header
    ("auction_id", pa.int64()),       # stable per-auction id -> the diff key
    ("item_id", pa.int64()),
    ("bonus_key", pa.string()),       # canonical hash of bonus_lists + modifiers
    ("pet_species_id", pa.int64()),   # battle pets (item 82800 cages)
    ("pet_quality_id", pa.int64()),
    ("pet_level", pa.int64()),
    ("buyout", pa.int64()),           # copper; None = bid-only auction
    ("bid", pa.int64()),
    ("quantity", pa.int64()),
    ("time_left", pa.string()),       # SHORT <30m | MEDIUM 30m-2h | LONG 2-12h | VERY_LONG 12-48h
])


def bonus_key(item: dict) -> str:
    """Canonical string for an item's bonus lists + modifiers, so two listings of the
    'same' piece of gear (same ilvl, tertiary, crafted stats...) compare equal."""
    parts = []
    bl = sorted(item.get("bonus_lists") or [])
    if bl:
        parts.append("b:" + ",".join(map(str, bl)))
    mods = sorted((m.get("type", -1), m.get("value", -1))
                  for m in (item.get("modifiers") or []))
    if mods:
        parts.append("m:" + ",".join(f"{t}={v}" for t, v in mods))
    return "|".join(parts)


# Undocumented by Blizzard (same caveat as modifier 28's ilvl use in
# dashboard.py) -- this is inference from real production data, not a
# documented fact. Found 2026-07-23 investigating item 238014 (Sun-Blessed
# Sickle, a crafted item) reporting wildly wrong prices: type 44 values
# increment sequentially per craft (245822, 245823, 245824...) -- a
# per-instance serial number, never meaningful for price comparison; type 42
# varies continuously within an otherwise-identical bonus_lists+modifiers
# group -- the crafted stat roll, granular enough that near-identical items
# almost never share an exact bonus_key. Together they fragmented one liquid
# ~1000-1300g market into 25+ near-unique variants, each with 1-2 sales --
# thin enough that a single misclassified camped relist (a known separate
# bug, see diff_snapshots.py's blind-spot note) could dominate a variant's
# entire percentile.
#
# Type 9 added 2026-07-24, investigating item 7761 (Steelclaw Reaver, a
# non-crafted level-21 rare weapon): region-wide listings carried nine
# distinct m:9=NN values (25/30/60/61/79/80/81/84/90) for what a human
# confirmed is the same transmog appearance regardless of value -- i.e. not
# a real, value-changing variant, same category as 42/44 even though this
# item isn't crafted. Corroborating evidence from the same investigation:
# Draenor's own troll/camped listings priced identically (~398,605g)
# whether tagged 9=30 or 9=90, which wouldn't happen if the modifier were a
# stat that mattered. Consequence of NOT pooling it: a genuinely cheap
# region-wide listing (28,500g, tagged 9=30) never joined against Draenor's
# sell-side data for 9=90, so it was invisible to snipe_check entirely.
MARKET_IGNORE_MODIFIER_TYPES = {9, 42, 44}


def market_key(bk: str) -> str:
    """Coarser version of bonus_key for MATCHING/GROUPING only -- sold-price
    percentiles, the current-lowest-listing cap, relist detection, and the
    buy/sell join in snipe_check.py -- so near-identical crafted items pool
    into one market instead of fragmenting into dozens of 1-2-sale buckets
    (see MARKET_IGNORE_MODIFIER_TYPES above for why). The raw bonus_key is
    still stored and displayed as-is everywhere; only matching uses this.

    Mirrored as a SQL macro in analyze.connect() for DuckDB-side grouping
    (sold-price/current-lowest queries) rather than shared as a single
    Python UDF -- this duckdb version's Python UDF support requires numpy,
    which isn't otherwise a project dependency (see CLAUDE.md's "minimal
    deps" convention) and is disproportionate weight for one string
    transform. The two implementations are kept honest by
    tests/test_market_key.py, which runs the same vectors through both and
    asserts identical results."""
    if not bk:
        return bk
    parts = []
    for part in bk.split("|"):
        if not part.startswith("m:"):
            parts.append(part)
            continue
        mods = [pair for pair in part[2:].split(",")
                if int(pair.split("=", 1)[0]) not in MARKET_IGNORE_MODIFIER_TYPES]
        if mods:
            parts.append("m:" + ",".join(mods))
    return "|".join(parts)


def rows(payload: dict, ts: int) -> list[dict]:
    out = []
    for a in payload.get("auctions") or []:
        it = a.get("item") or {}
        out.append({
            "snapshot_ts": ts,
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


def state_path(cr: int) -> Path:
    return DATA / "state" / f"{cr}.json"


def get_auctions_with_backoff(cr: int, headers: dict, attempts: int = 4):
    """GET the auctions endpoint, backing off on 429/5xx (honors Retry-After)."""
    delay = 30
    for attempt in range(1, attempts + 1):
        r = api_get(f"/data/wow/connected-realm/{cr}/auctions", "dynamic", headers=headers)
        if r.status_code not in RETRYABLE or attempt == attempts:
            return r
        wait = min(int(r.headers.get("Retry-After") or delay), 600)
        log.warning("HTTP %s from auctions endpoint, retrying in %ss (attempt %s/%s)",
                    r.status_code, wait, attempt, attempts)
        time.sleep(wait)
        delay *= 2
    return r


def fetch_once(cr: int) -> Path | None:
    sp = state_path(cr)
    headers = {}
    if sp.exists():
        lm = json.loads(sp.read_text()).get("last_modified")
        if lm:
            headers["If-Modified-Since"] = lm

    r = get_auctions_with_backoff(cr, headers)
    if r.status_code == 304:
        return None                     # no new hourly dump yet
    r.raise_for_status()

    lm = r.headers.get("Last-Modified")
    ts = int(parsedate_to_datetime(lm).timestamp()) if lm else int(time.time())
    out_dir = DATA / "snapshots" / str(cr)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{ts}.parquet"
    if out.exists():                    # same dump fetched twice (e.g. after restart)
        return None

    try:
        payload = r.json()
    except ValueError:
        # Truncated/garbled body: skip this poll; state file is untouched, so the
        # next poll re-requests the same dump.
        log.error("malformed JSON body (%s bytes), skipping this poll", len(r.content))
        return None

    table = pa.Table.from_pylist(rows(payload, ts), schema=SCHEMA)
    pq.write_table(table, out, compression="zstd")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"last_modified": lm}))
    log.info("saved %s: %s auctions", out.name, table.num_rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", metavar="REALM_SLUG",
                    help="look up connected-realm id by realm slug and exit")
    ap.add_argument("--cr-id", type=int, help="connected realm id to collect")
    ap.add_argument("--loop", action="store_true", help="poll every 10 minutes forever")
    args = ap.parse_args()

    if args.find:
        hits = find_connected_realm(args.find)
        if not hits:
            sys.exit(f"No connected realm found for slug '{args.find}' "
                     f"(slug = lowercase, spaces -> hyphens, e.g. 'tarren-mill').")
        for cr_id, realms in hits:
            print(f"connected-realm id {cr_id}: {', '.join(realms)}")
        return

    if not args.cr_id:
        ap.error("--cr-id required (use --find <realm> first)")

    setup_logging()

    if not args.loop:
        p = fetch_once(args.cr_id)
        print("no new snapshot yet" if p is None else f"wrote {p}")
        return

    log.info("collecting connected realm %s; ctrl-c to stop", args.cr_id)
    while True:
        try:
            fetch_once(args.cr_id)
        except Exception:               # keep the multi-day run alive no matter what
            log.exception("poll failed; retrying in 10 min")
        time.sleep(600)


if __name__ == "__main__":
    main()
