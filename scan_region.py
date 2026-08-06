#!/usr/bin/env python3
"""Sweep every EU connected realm's *current* auction listings into
data/listings/{cr_id}.parquet — latest snapshot only, overwritten each sweep.
No history, no diffing: this is the buy side of the cross-realm engine. Sale
inference (sold prices, sales/day) stays on fetch_snapshot.py + diff_snapshots.py,
run only against your chosen sell realm(s).

Each sweep also records when Blizzard actually published each realm's dump,
into data/state/sweep_publish.json — see _publish_state_path() for why that
is worth keeping — and sends those back as If-Modified-Since, so a realm
whose dump hasn't changed costs a 304 rather than a full download.

Measured 2026-08-06: Blizzard publishes **the entire EU region at once**, not
per realm on staggered offsets. All 92 realms' dumps carried a Last-Modified
within one second of each other (88 at 05:41:26, 4 at 05:41:27). So the vast
majority of sweeps at the current 60s cadence re-download data that cannot
have changed, which is exactly what the conditional request now avoids.

Usage:
  python scan_region.py                 # one sweep of every EU connected realm
  python scan_region.py --loop          # sweep every hour, forever
  python scan_region.py --exclude 1403  # skip realms already deep-collected
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from email.utils import parsedate_to_datetime
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
    # sys.stdout explicitly -- see fetch_snapshot.py's setup_logging() for why
    # (Railway tags anything on stderr as "severity":"error" regardless of
    # the actual Python log level; StreamHandler() with no argument defaults
    # to stderr).
    console_h = logging.StreamHandler(sys.stdout)
    for h in (file_h, console_h):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


# Per-realm Blizzard publish times, recorded by every sweep (2026-08-06).
#
# The buy side had no record of *when* a realm's dump was actually published:
# scan_one() fetches unconditionally and threw the response's Last-Modified
# away, so a sniper-list alert timestamp could not be told apart from the
# other things that make an old listing newly qualify -- the 4h notification
# cooldown, the 6h TSM sale-average refresh, or a NameCache/appearance entry
# converging in the background. All four produce an alert; only one of them
# means "this listing just appeared".
#
# Costs nothing: the header rides on a response the sweep already makes, and
# the whole region is one small JSON file written once per sweep, not once
# per realm.
#
# These are also fed back as If-Modified-Since (see _conditional_for()), so
# an unchanged realm costs a 304 instead of a full dump download.
#
# A function, not a module-level constant, because the tests monkeypatch
# DATA -- same reason watchlist._rule_state_path() is one.
def _publish_state_path() -> Path:
    return DATA / "state" / "sweep_publish.json"


def load_publish_state() -> dict:
    """{cr_id (str): {last_modified, published_ts, first_seen_ts}} for every
    realm a sweep has seen. `published_ts` is Blizzard's own publish moment;
    `first_seen_ts` is when we first observed that value, so the difference
    is our detection lag for that realm. Empty dict if no sweep has recorded
    anything yet."""
    try:
        return json.loads(_publish_state_path().read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        # A truncated/corrupt state file is diagnostics only -- it must never
        # take a sweep down. Worst case it gets rebuilt on the next one.
        log.exception("unreadable publish state, starting fresh")
        return {}


def _save_publish_state(state: dict) -> None:
    """Temp-file-then-rename, the same atomicity discipline scan_one() below
    already applies to its parquet writes."""
    path = _publish_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def _conditional_for(cr: int, state: dict) -> str | None:
    """The If-Modified-Since value to send for this realm, or None to request
    the dump in full.

    Guarded on the parquet actually existing: a 304 means "you already have
    this", so sending the header while the file is missing would leave the
    realm with no listings at all until its *next* publish -- up to an hour
    of a realm silently absent from the sweep. The state file and the
    listings directory are separate files on the volume and can legitimately
    disagree (a file deleted by hand, a partial restore, or the malformed-body
    path above having written one without the other)."""
    entry = state.get(str(cr))
    if not entry or not entry.get("last_modified"):
        return None
    if not (DATA / "listings" / f"{cr}.parquet").exists():
        return None
    return entry["last_modified"]


def _parse_http_date(value: str | None) -> int | None:
    """HTTP date -> epoch seconds, or None if absent/unparseable. Never
    raises: a malformed header is worth losing one realm's timing record,
    not the sweep."""
    if not value:
        return None
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except Exception:
        log.warning("unparseable Last-Modified %r", value)
        return None


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


def scan_one(cr: int, ts: int, if_modified_since: str | None = None) -> tuple[int | None, str | None]:
    """Fetch one realm's current listings and overwrite its parquet. Returns
    (row count written, this dump's Last-Modified header).

    A row count of **None** means Blizzard answered 304 Not Modified: this
    realm's dump is unchanged, nothing was rewritten, and the parquet already
    on disk is still current. Same `None means nothing new` convention
    fetch_snapshot.fetch_once() already uses. 0, by contrast, means a real
    response we could not use.

    The Last-Modified is returned rather than recorded here so sweep() can
    write the whole region's timings in one file write instead of a
    read-modify-write per realm -- see _publish_state_path()."""
    headers = {"If-Modified-Since": if_modified_since} if if_modified_since else {}
    r = get_auctions_with_backoff(cr, headers=headers)
    if r.status_code == 304:
        # Checked before raise_for_status(), same order as fetch_once(). 304
        # is not in RETRYABLE, so the backoff wrapper returns it immediately.
        return None, if_modified_since
    r.raise_for_status()
    last_modified = r.headers.get("Last-Modified")
    try:
        payload = r.json()
    except ValueError:
        log.error("cr %s: malformed JSON body, skipping", cr)
        # (0, None), not (0, last_modified): nothing was written, so claiming
        # we hold this publish would overstate the freshness of the parquet
        # still sitting on disk from the previous sweep. Leaving it unrecorded
        # also means the next sweep sends no If-Modified-Since and re-requests
        # this dump in full rather than 304ing onto a file we never wrote.
        return 0, None

    table = pa.Table.from_pylist(rows(payload, cr, ts), schema=LISTING_SCHEMA)
    out_dir = DATA / "listings"
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"{cr}.parquet"
    # Write to a temp file in the same directory first, then atomically
    # rename over the final path -- writing directly to final_path left a
    # window where a concurrent reader (e.g. find_snipes() running in
    # another request/thread while collect_all()'s background loop sweeps)
    # could open a truncated/partially-written parquet file and crash with
    # a hard DuckDB read error. Hit live by accident 2026-07-25 while
    # verifying an unrelated fix. Same directory is required for
    # os.replace() to be atomic (a cross-filesystem rename isn't).
    tmp_path = out_dir / f"{cr}.parquet.tmp"
    pq.write_table(table, tmp_path, compression="zstd")
    os.replace(tmp_path, final_path)
    return table.num_rows, last_modified


def sweep(exclude: set[int]) -> None:
    realms = [cr for cr in list_connected_realms() if cr not in exclude]
    log.info("sweeping %s EU connected realms (%s excluded)", len(realms), len(exclude))
    ts = int(time.time())
    state = load_publish_state()
    republished = 0
    unchanged = 0
    for cr in realms:
        try:
            n, last_modified = scan_one(cr, ts, if_modified_since=_conditional_for(cr, state))
            if n is None:
                unchanged += 1
            else:
                log.info("cr %s: %s listings", cr, n)
        except Exception:                # one bad realm shouldn't kill the sweep
            log.exception("cr %s: sweep failed, continuing", cr)
            continue
        if not last_modified:
            continue
        prev = state.get(str(cr))
        if prev and prev.get("last_modified") == last_modified:
            continue                     # same dump as last sweep, nothing new
        published_ts = _parse_http_date(last_modified)
        # Wall clock *at this realm*, not the sweep's start `ts`.
        #
        # It was the sweep start originally, on the reasoning that a
        # sequential ~92-realm walk would otherwise fold its own position
        # into the number. Production disproved that within the hour: the
        # 06:41:46 publish landed mid-sweep on 2026-08-06 and every realm
        # visited after it recorded a *negative* lag (min -108s), because the
        # sweep had started before the dump existed. The walk's position is
        # not noise -- it is genuinely part of how long it took us to see the
        # dump, which is the whole quantity being measured.
        seen_ts = int(time.time())
        state[str(cr)] = {"last_modified": last_modified,
                          "published_ts": published_ts,
                          "first_seen_ts": seen_ts}
        republished += 1
        if published_ts is not None:
            log.info("cr %s: new dump, detected %ss after publish",
                     cr, seen_ts - published_ts)
    try:
        _save_publish_state(state)
    except Exception:                    # diagnostics must not fail a sweep
        log.exception("could not save publish state")
    log.info("sweep done: %s of %s realms republished, %s unchanged (304)",
             republished, len(realms), unchanged)


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
