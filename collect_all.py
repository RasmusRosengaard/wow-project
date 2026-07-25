#!/usr/bin/env python3
"""Server-side collection cycle (Stage 4): deep-collects every **FULL/HIGH
population** EU connected realm's sale history (not literally all ~100 --
scoped down 2026-07-23 once low-pop realms turned out to add collection
overhead without much sniping-relevant liquidity), so any subscriber can
pick a sell realm from a set that's actually worth trading on. Replaced the
human's local run_cycle.py + Task Scheduler (both deleted) now that the app
runs on Railway -- dashboard.py's startup event calls collect_all() on a
background loop instead.

That loop runs every ~10 minutes, not hourly, deliberately mirroring how the
old local collector worked (see fetch_snapshot.py): Blizzard republishes AH
data roughly hourly but at no fixed clock time, so polling only once an hour
from whenever the container happened to boot could sit out of phase with the
real publish time by up to an hour. Polling every ~10 min keeps the lag
after a real update small; fetch_once()'s If-Modified-Since check makes the
no-op polls cheap (a 304, not a real download).

Reuses fetch_snapshot.fetch_once()/diff_snapshots.main() the same way
run_cycle.py used to (same sys.argv-patching pattern for diff_snapshots,
which is only importable as a CLI module today). scan_region.sweep() stays
**unscoped** -- every EU realm's current listings, not just FULL/HIGH pop
ones, because the whole cross-realm thesis is cheap listings sitting on
low-pop realms waiting to be validated against a high-pop sell realm's sold
prices; scoping the buy side down to FULL/HIGH would defeat that.

Retention: once this runs indefinitely instead of one human's bounded local
run, unbounded snapshot growth is a real storage cost, not just a
someday-TODO -- prune_old_snapshots() keeps the two most recent snapshots
per realm (diff_snapshots needs at least 2) and anything newer than
RETENTION_DAYS, dropping the rest.
"""
import logging
import sys
import time
from pathlib import Path

import duckdb

import blizz
import diff_snapshots
import fetch_snapshot
import item_names
import scan_region

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

log = logging.getLogger("collect_all")

RETENTION_DAYS = 14
DEEP_COLLECT_POPULATION_TIERS = {"FULL", "HIGH"}

# How many not-yet-cached items to resolve per cycle -- see
# _prewarm_item_base_levels() below. This runs in the background (no user
# waiting on it), so it can afford more per-call work than
# snipe_check.MAX_BASE_LEVEL_LOOKUPS_PER_CALL, but still bounded so one
# cycle can't run long enough to meaningfully delay the next poll.
PREWARM_BASE_LEVEL_CAP = 1000

_deep_collect_realm_ids: list[int] | None = None


def deep_collect_realm_ids(force_refresh: bool = False) -> list[int]:
    """FULL/HIGH-population realm ids, worth deep sale-inference collection.
    Computed once and cached in-process -- population tiers change rarely,
    and every process restart/redeploy recomputes it anyway; no need to
    re-check ~100 realms' population every hourly cycle."""
    global _deep_collect_realm_ids
    if _deep_collect_realm_ids is None or force_refresh:
        all_realms = blizz.list_connected_realms()
        ids = []
        for cr in all_realms:
            try:
                if blizz.connected_realm_population(cr) in DEEP_COLLECT_POPULATION_TIERS:
                    ids.append(cr)
            except Exception:
                log.exception("collect_all: population lookup failed for realm %s", cr)
        _deep_collect_realm_ids = ids
        log.info("collect_all: %s of %s EU realms are FULL/HIGH pop -- deep-collecting those",
                 len(ids), len(all_realms))
    return _deep_collect_realm_ids


def _diff(cr: int) -> None:
    orig_argv = sys.argv
    sys.argv = ["diff_snapshots.py", "--cr-id", str(cr)]
    try:
        diff_snapshots.main()
    finally:
        sys.argv = orig_argv


def prune_old_snapshots(cr: int, retention_days: int = RETENTION_DAYS) -> int:
    """Delete snapshots older than retention_days, always keeping at least
    the 2 most recent (diff_snapshots needs 2+ to produce anything). Returns
    the number of files removed."""
    snap_dir = DATA / "snapshots" / str(cr)
    paths = sorted(snap_dir.glob("*.parquet"), key=lambda p: int(p.stem))
    if len(paths) <= 2:
        return 0
    cutoff = int(time.time()) - retention_days * 86400
    removed = 0
    for p in paths[:-2]:
        if int(p.stem) < cutoff:
            p.unlink()
            removed += 1
    return removed


def _prewarm_item_base_levels(cap: int = PREWARM_BASE_LEVEL_CAP) -> int:
    """Resolves item catalog levels (item_names.NameCache.ensure_many())
    for a batch of items carrying a type-28 modifier, in the background,
    so snipe_check._populate_market_keys() finds them already cached by
    the time a real /api/snipes request needs them. Added 2026-07-25 after
    a live incident: that per-request lookup turned out to have a
    candidate set of 15,000+ distinct items on just one sell realm (type
    28 is common on modern ilvl-scaling gear, not rare), which is capped
    per-request now (MAX_BASE_LEVEL_LOOKUPS_PER_CALL in snipe_check.py) to
    keep any one request fast -- but a cap only helps if something else
    keeps making progress on the rest. This is that something else: it
    runs every ~10-min cycle regardless of user traffic, so the cache
    converges within a few hours instead of depending on how many
    dashboard loads happen to hit still-uncached items.

    Reads straight from data/listings/*.parquet (the region-wide sweep
    collect_all() just ran) via a throwaway DuckDB connection -- doesn't
    need analyze.connect()'s sell-realm-scoped setup, since base level is
    realm-independent. Returns the number of candidate ids considered (not
    necessarily all fetched -- ensure_many() itself skips anything already
    cached and stops at `cap` new lookups)."""
    listings_dir = DATA / "listings"
    if not any(listings_dir.glob("*.parquet")):
        return 0
    con = duckdb.connect()
    ids = [item_id for (item_id,) in con.execute(
        f"SELECT DISTINCT item_id FROM read_parquet('{(listings_dir / '*.parquet').as_posix()}') "
        "WHERE bonus_key LIKE '%28=%'"
    ).fetchall()]
    con.close()
    if not ids:
        return 0
    item_names.NameCache().ensure_many(ids, max_workers=24, limit=cap)
    return len(ids)


def collect_all() -> dict:
    """One full pass: poll+diff+prune every FULL/HIGH-pop realm, then one
    unscoped region-wide listings sweep (every EU realm, see module
    docstring). A single realm failing never aborts the rest -- same "don't
    let one bad realm kill the cycle" principle scan_region.sweep() follows.

    Only re-diffs a realm when fetch_once() actually wrote a *new* snapshot
    this cycle -- diff_snapshots recomputes from every snapshot on disk each
    time (by design, always safe to rebuild), so re-running it on a cycle
    where nothing changed would just recompute the identical output at real
    CPU cost. This matters more now that collect_all() runs every ~10
    minutes (matching Blizzard's own publish cadence, not a fixed hourly
    clock that could drift out of phase with it) instead of hourly --
    fetch_once() itself stays cheap on a no-op cycle (one If-Modified-Since
    request, 304 response), diff_snapshots does not."""
    realm_ids = deep_collect_realm_ids()
    polled = diffed = pruned = 0
    failed: list[int] = []

    for cr in realm_ids:
        try:
            got_new_snapshot = fetch_snapshot.fetch_once(cr) is not None
            if got_new_snapshot:
                polled += 1
                n_snaps = len(list((DATA / "snapshots" / str(cr)).glob("*.parquet")))
                if n_snaps >= 2:
                    _diff(cr)
                    diffed += 1
                    pruned += prune_old_snapshots(cr)
        except Exception:
            log.exception("collect_all: realm %s failed", cr)
            failed.append(cr)

    try:
        scan_region.sweep(exclude=set())
    except Exception:
        log.exception("collect_all: region sweep failed")

    prewarmed = 0
    try:
        prewarmed = _prewarm_item_base_levels()
    except Exception:
        log.exception("collect_all: item base-level prewarm failed")

    summary = {"realms": len(realm_ids), "polled": polled, "diffed": diffed,
              "pruned_snapshots": pruned, "failed": failed,
              "base_level_candidates": prewarmed}
    log.info("collect_all: %s", summary)
    return summary


if __name__ == "__main__":
    fetch_snapshot.setup_logging()
    collect_all()
