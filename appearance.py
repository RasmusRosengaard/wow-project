#!/usr/bin/env python3
"""Phase 3 groundwork: appearance-rarity cache, backed by wago.tools' public
ItemModifiedAppearance DB2 export (not a Blizzard API -- there is no
per-item "what does this look like and how many other items share it"
endpoint, so the bulk DB2 dump is the only practical source; the roadmap's
"static API as fallback" isn't implemented since the bulk export has worked
reliably so far).

Rarity proxy: for each ItemAppearanceID (the visual "look"), how many
distinct ItemIDs grant it. Fewer source items -> a rarer/more unique look.
This is a deliberately simple v1 proxy, not a real obtainability model (drop
rate, whether the source is still farmable, BoE vs quest-locked, etc.) --
per CLAUDE.md's Phase 3 notes, manual curation on top of this is expected
later. It also doesn't dedupe modifier variants of the *same* gearing method
(e.g. warforged recolors) perfectly -- acceptable slop for a first cut.

Cache lives at data/appearances.json. Unlike item_names.NameCache (lazy,
per-item, built from a live low-latency Blizzard endpoint), this is a bulk
batch fetch (~160k rows in one shot) refreshed manually/periodically, not
per lookup -- there's no cheap per-item equivalent to fall back to. Never
wired into the automatic Railway collection loop: this hits a third-party
site outside the Blizzard rate-limit budget the rest of the project is
scoped around, so refreshing it is a deliberate, manual action for now.

Usage:
  python appearance.py --refresh          # download + rebuild the cache
  python appearance.py --status           # cache age / item count, no network
"""
import argparse
import csv
import io
import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE_PATH = DATA / "appearances.json"

WAGO_CSV_URL = "https://wago.tools/db2/ItemModifiedAppearance/csv"
CACHE_MAX_AGE = 7 * 86400  # DB2 dumps only move on content patches; weekly is plenty


def _download_rows() -> list[dict]:
    resp = requests.get(WAGO_CSV_URL, timeout=60)
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def build_cache() -> dict:
    """item_id (str) -> {"appearance_id": int, "source_count": int}.
    When an item has multiple rows (modifier variants), the row with
    ItemAppearanceModifierID == "0" (the base/default look) wins; otherwise
    the first row seen for that item is kept."""
    rows = _download_rows()
    sources_by_appearance: dict[str, set[str]] = {}
    chosen: dict[str, tuple[str, str]] = {}  # item_id -> (appearance_id, modifier_id)
    for row in rows:
        item_id = row["ItemID"]
        appearance_id = row["ItemAppearanceID"]
        modifier_id = row["ItemAppearanceModifierID"]
        sources_by_appearance.setdefault(appearance_id, set()).add(item_id)
        existing = chosen.get(item_id)
        if existing is None or (existing[1] != "0" and modifier_id == "0"):
            chosen[item_id] = (appearance_id, modifier_id)

    cache = {
        "fetched_at": int(time.time()),
        "items": {
            item_id: {
                "appearance_id": int(appearance_id),
                "source_count": len(sources_by_appearance[appearance_id]),
            }
            for item_id, (appearance_id, _modifier_id) in chosen.items()
        },
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))
    return cache


class AppearanceCache:
    """Read-only, fail-soft lookup over the cache file. Never raises --
    a missing/stale/corrupt cache just means every lookup returns None, same
    as item_names.NameCache's "unknown" fallback, so callers that skip
    filtering on None degrade to "no appearance filter" rather than crashing."""

    def __init__(self):
        self._data: dict = {}
        if CACHE_PATH.exists():
            try:
                self._data = json.loads(CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def is_stale(self) -> bool:
        fetched_at = self._data.get("fetched_at")
        return fetched_at is None or (time.time() - fetched_at) > CACHE_MAX_AGE

    def source_count(self, item_id: int) -> int | None:
        entry = self._data.get("items", {}).get(str(item_id))
        return entry["source_count"] if entry else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="download and rebuild the cache")
    ap.add_argument("--status", action="store_true", help="show cache age / item count")
    args = ap.parse_args()

    if args.refresh:
        cache = build_cache()
        print(f"cached {len(cache['items'])} items across "
              f"{len({v['appearance_id'] for v in cache['items'].values()})} appearances "
              f"-> {CACHE_PATH}")
        return

    if args.status:
        ac = AppearanceCache()
        if not ac._data:
            print("no cache -- run with --refresh")
            return
        age_days = (time.time() - ac._data["fetched_at"]) / 86400
        print(f"{len(ac._data.get('items', {}))} items cached, "
              f"{age_days:.1f} days old{' (stale)' if ac.is_stale() else ''}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
