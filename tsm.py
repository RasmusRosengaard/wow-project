#!/usr/bin/env python3
"""EU region-wide sale-rate/liquidity data from TradeSkillMaster's public
CSV feed (https://tradeskillmaster.com/public-data). Added 2026-08-01,
human request -- this project's pricing model (see CLAUDE.md's "What this
project is") deliberately reads only the sell realm's current cheapest
listing, which means it carries zero liquidity/confidence signal on its
own: a listing that's sat unsold for weeks looks identical to a fast-moving
one. TSM's `saleRate`/`soldPerDay` fill that gap, purely as an
enrichment/filter on top of `snipe_check.find_snipes()`'s output -- not
part of the actual pricing computation, same non-authoritative role
`appearance.py`'s `AppearanceCache` already has.

Data source confirmed live via TSM's own docs (2026-08-01,
https://tradeskillmaster.com/public-data and
https://support.tradeskillmaster.com/en_US/api-documentation/tsm-public-web-api):
the public-data files are free, unauthenticated, no API key/rate limit,
explicitly intended for external tools ("pull them into ... any tool that
can read a URL"). Only the REGION-WIDE file carries sale-rate fields at
all -- the per-realm files (`realm/{slug}/items.csv`) have
marketValue/minBuyout/recent/historical but no saleRate/soldPerDay, so this
is deliberately EU-region-wide data, not specific to any one sell realm
(same scope as `snipe_check.py`'s own `region_median_g`). Region files
update ~daily per TSM's docs, versus ~every 3 hours for realm files --
`REFRESH_INTERVAL_SECONDS` is set well above that to avoid re-downloading
the same numbers.

Scope note: only non-pet items (`region/items.csv`) are covered. Caged
pets all share one item_id (82800, see fetch_snapshot.py's PET_CAGE_ITEM_ID
equivalent), so a per-item_id lookup would wrongly return the cage
listing's own sale rate for every pet species -- TSM's
`region/pets.csv` has genuinely separate per-species data, not wired up
here yet since nothing asked for pet coverage specifically.

Single-writer design, deliberately: unlike item_names.NameCache (fixed
2026-08-01 after a real lost-update race from multiple concurrent writers,
see that module's docstring), this cache is only ever written by
collect_all.py's background loop -- never from a live /api/snipes request.
Live requests only ever call SaleRateCache().get(item_id), read-only. With
exactly one writer, the whole class of lost-update race NameCache had is
impossible by construction, not just mitigated.

Usage:
  python tsm.py --refresh          # download + rebuild the cache now
  python tsm.py --status           # cache age / item count, no network
"""
import argparse
import csv
import io
import json
import logging
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE_PATH = DATA / "tsm_sale_rates.json"

log = logging.getLogger("tsm")

REGION_ITEMS_URL = "https://public-data.tradeskillmaster.com/retail/eu/region/items.csv"

# TSM's own docs say region files update ~daily -- polling much more often
# than that just re-downloads identical numbers. Human-specified, not tied
# to the Blizzard collector's 10-min cadence (a completely different
# upstream update rate).
REFRESH_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours


def _fetch_csv() -> dict[int, dict]:
    """One-shot download+parse of the EU region items CSV -> item_id ->
    {"sale_rate": float, "sold_per_day": float}. Never raises -- matches
    item_names.py/appearance.py's existing display/enrichment cache
    convention: a failed fetch just means stale-or-no data for this cycle,
    not a broken response for anything on the actual pricing path."""
    try:
        r = requests.get(REGION_ITEMS_URL, timeout=30)
        if r.status_code != 200:
            log.warning("TSM region items fetch failed: HTTP %s", r.status_code)
            return {}
    except Exception:
        log.exception("TSM region items fetch failed")
        return {}
    out: dict[int, dict] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            item_id = int(row["itemId"])
            out[item_id] = {
                "sale_rate": float(row["saleRate"]),
                "sold_per_day": float(row["soldPerDay"]),
                # avgSalePrice (added 2026-08-03, human request -- "region
                # sale avg"): TSM's region-wide average sale price, already
                # in copper (confirmed live -- item 2624/Thinking Cap:
                # avgSalePrice 28,500,000 = 2850g, in line with its
                # marketValue being six figures too). Same column as
                # saleRate/soldPerDay, so it shares their availability --
                # nothing special needed to handle "if it exists": a missing
                # entry in this dict already means "no TSM data for this
                # item" for every field at once, same as today.
                "avg_sale_price": float(row["avgSalePrice"]),
            }
        except (KeyError, ValueError):
            continue  # malformed row -- skip it, don't fail the whole batch
    return out


class SaleRateCache:
    """Read-mostly lookup over the cache file, same load-once-per-instance
    convention as item_names.NameCache/appearance.AppearanceCache. Only
    collect_all.py's background loop should ever call refresh_if_stale();
    live-request code should only call get()."""

    def __init__(self):
        data: dict = {}
        if CACHE_PATH.exists():
            try:
                data = json.loads(CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        self._fetched_at: float = data.get("fetched_at", 0)
        self._items: dict[int, dict] = {
            int(k): v for k, v in data.get("items", {}).items()
        }

    def refresh_if_stale(self, interval_seconds: float = REFRESH_INTERVAL_SECONDS) -> bool:
        """Re-fetches only if the cache is empty or older than
        interval_seconds. Returns True if a fetch actually happened. A
        failed fetch (network error, non-200) leaves the existing cache
        exactly as-is rather than wiping it -- serving stale data is better
        than serving none."""
        if self._items and time.time() - self._fetched_at < interval_seconds:
            return False
        fresh = _fetch_csv()
        if not fresh:
            return False
        self._items = fresh
        self._fetched_at = time.time()
        self._save()
        return True

    def _save(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "fetched_at": self._fetched_at,
            "items": {str(k): v for k, v in self._items.items()},
        }))

    def get(self, item_id: int) -> dict | None:
        """{"sale_rate": float, "sold_per_day": float, "avg_sale_price":
        float (copper)}, or None if TSM has no data for this item (never
        tracked, or a cache that hasn't been refreshed yet)."""
        return self._items.get(item_id)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="download and rebuild the cache now")
    ap.add_argument("--status", action="store_true", help="show cache age / item count")
    args = ap.parse_args()

    if args.refresh:
        cache = SaleRateCache()
        did_fetch = cache.refresh_if_stale(interval_seconds=0)  # force, ignore staleness check
        if not did_fetch:
            print("fetch failed -- see log output above")
            return
        print(f"cached {len(cache._items)} items -> {CACHE_PATH}")
        return

    if args.status:
        cache = SaleRateCache()
        if not cache._items:
            print("no cache -- run with --refresh")
            return
        age_hours = (time.time() - cache._fetched_at) / 3600
        stale = age_hours * 3600 > REFRESH_INTERVAL_SECONDS
        print(f"{len(cache._items)} items cached, "
              f"{age_hours:.1f} hours old{' (stale)' if stale else ''}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
