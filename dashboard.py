#!/usr/bin/env python3
"""FastAPI dashboard: a live, auto-refreshing view of snipe_check.find_snipes()
results plus pipeline freshness. Read-only web layer over the same data/ files
the CLI tools use -- it does not run the pipeline itself (run_cycle.py /
Task Scheduler still own that; this just displays what they've written).

Usage:
  python dashboard.py --sell 1403                  # serves on http://127.0.0.1:8000
  python dashboard.py --sell 1403 --host 0.0.0.0 --port 8080

--sell only prefills the UI's default sell-realm id; every request re-reads
"sell" from its own query param, so one running dashboard can inspect any
sell realm you've collected.
"""
import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import analyze
import blizz
import snipe_check
from auth import UserCreate, UserRead, auth_backend, current_active_user, fastapi_users
from db import User
from item_names import NameCache

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

log = logging.getLogger("dashboard")

# Server-side collection (Stage 4) -- off by default so a local `python
# dashboard.py` run for quick dev/testing doesn't also spin up an hourly
# all-realm collector competing with whatever's already running locally
# (Task Scheduler, a manual run_cycle.py). Railway sets this to "true".
ENABLE_BACKGROUND_COLLECTION = os.environ.get("ENABLE_BACKGROUND_COLLECTION", "false").lower() == "true"
COLLECTION_INTERVAL_SECONDS = 60 * 60  # matches Blizzard's hourly dump cadence


async def _collection_loop() -> None:
    import collect_all as collect_all_module
    while True:
        try:
            await asyncio.to_thread(collect_all_module.collect_all)
        except Exception:
            log.exception("background collection cycle failed")
        await asyncio.sleep(COLLECTION_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if ENABLE_BACKGROUND_COLLECTION:
        log.info("starting background collection loop (every %ss)", COLLECTION_INTERVAL_SECONDS)
        task = asyncio.create_task(_collection_loop())
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title="AH Snipe Dashboard", lifespan=lifespan)

app.include_router(fastapi_users.get_auth_router(auth_backend), prefix="/auth", tags=["auth"])
app.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"])

# Realm name/slug never changes -- cache in-process for the life of the
# server rather than a file cache, to keep this dashboard-only concern out of
# item_names.py's on-disk cache.
_realm_info_cache: dict[int, dict] = {}

# The modifier-28 "item level" isn't officially documented (see CLAUDE.md);
# it's clearly wrong for items outside the modern ilvl-scaling system -- e.g.
# a classic fixed-stat wand showed "ilvl 1112" despite a catalog level in the
# 30s. Only trust it when it's within a generous multiple of the item's own
# base level; otherwise fall back to a plain bonus-count summary.
ILVL_PLAUSIBILITY_MULTIPLE = 5


def _realm_info(cr_id: int) -> dict:
    if cr_id not in _realm_info_cache:
        try:
            realms = blizz.connected_realm_realms(cr_id)
        except Exception:
            realms = []
        _realm_info_cache[cr_id] = realms[0] if realms else {"name": None, "slug": None}
    return _realm_info_cache[cr_id]


def _parse_variant(bonus_key: str) -> dict:
    """Pull the item-level modifier (type 28) out of the raw bonus_key for a
    readable summary, without discarding the rest -- the raw string still
    rides along in the response for the tooltip."""
    ilvl = None
    bonus_count = 0
    for part in bonus_key.split("|"):
        if part.startswith("b:") and part[2:]:
            bonus_count = len(part[2:].split(","))
        elif part.startswith("m:"):
            for pair in part[2:].split(","):
                t, _, v = pair.partition("=")
                if t == "28" and v:
                    ilvl = v
    return {"ilvl": ilvl, "bonus_count": bonus_count}


def _variant_label(bk: str, item_id: int, names: NameCache | None) -> str:
    parsed = _parse_variant(bk) if bk else {"ilvl": None, "bonus_count": 0}
    ilvl_ok = False
    if parsed["ilvl"] and names is not None:
        base = names.base_level(item_id)
        ilvl_ok = base is not None and int(parsed["ilvl"]) <= base * ILVL_PLAUSIBILITY_MULTIPLE
    if ilvl_ok:
        return f"ilvl {parsed['ilvl']}"
    if bk:
        return f"{parsed['bonus_count']} bonus" + ("es" if parsed["bonus_count"] != 1 else "")
    return "-"


def _row_to_json(r: dict, names: NameCache | None) -> dict:
    if r["pet_species_id"] is not None:
        variant = f"pet:{r['pet_species_id']}/{r['pet_quality_id']}"
    else:
        variant = _variant_label(r["bonus_key"] or "", r["item_id"], names)
    out = {
        "buy_realm": r["buy_realm"],
        "buy_realm_name": _realm_info(r["buy_realm"])["name"] or str(r["buy_realm"]),
        "item_id": r["item_id"],
        "variant": variant,
        "variant_raw": r["bonus_key"] or None,
        "buy_g": r["buy_g"],
        "sell_p_g": r["sell_p_g"],
        "buy_copper": r["buy_copper"],
        "sell_copper": r["sell_copper"],
        "per_day": r["per_day"],
        "discount_pct": r["discount_pct"],
    }
    if names is not None:
        out["name"] = names.get(r["item_id"], r["pet_species_id"])
        out["icon"] = names.icon(r["item_id"], r["pet_species_id"])
        out["quality_color"] = names.quality_color(r["item_id"], r["pet_species_id"], r["pet_quality_id"])
    return out


@app.get("/api/me")
async def api_me(user: User = Depends(current_active_user)) -> dict:
    return {"email": user.email, "subscription_status": user.subscription_status}


@app.get("/api/snipes")
def api_snipes(sell: int, items: str | None = None, min_discount: float = 0.3,
                min_per_day: float = 0.5, sell_percentile: float = 0.25,
                top: int = 50, sort: str = Query("discount"), names: bool = False,
                user: User = Depends(current_active_user)) -> dict:
    events_path = DATA / "events" / f"{sell}.parquet"
    if not events_path.exists():
        raise HTTPException(400, f"{events_path} not found -- run diff_snapshots.py --cr-id {sell} first")
    if not any((DATA / "listings").glob("*.parquet")):
        raise HTTPException(400, "data/listings/*.parquet not found -- run scan_region.py first")
    if sort not in snipe_check.SORT_COLUMNS:
        raise HTTPException(400, f"sort must be one of {sorted(snipe_check.SORT_COLUMNS)}")

    item_ids = snipe_check.parse_items(items, None)
    con = analyze.connect(sell)
    rows = snipe_check.find_snipes(con, sell, items=item_ids, min_discount=min_discount,
                                   min_per_day=min_per_day, sell_percentile=sell_percentile,
                                   top=top, sort=sort)
    name_cache = NameCache() if names else None
    out_rows = [_row_to_json(r, name_cache) for r in rows]
    if name_cache is not None:
        name_cache.save()
    return {
        "rows": out_rows, "count": len(out_rows), "caveat": snipe_check.CAVEAT,
        "region": blizz.REGION, "sell_realm_slug": _realm_info(sell)["slug"],
    }


@app.get("/api/status")
def api_status(sell: int, user: User = Depends(current_active_user)) -> dict:
    state_path = DATA / "state" / f"{sell}.json"
    last_modified = None
    if state_path.exists():
        last_modified = json.loads(state_path.read_text()).get("last_modified")

    listings_dir = DATA / "listings"
    listing_files = list(listings_dir.glob("*.parquet")) if listings_dir.exists() else []
    listings_updated = max((f.stat().st_mtime for f in listing_files), default=None)

    return {
        "sell": sell,
        "last_modified": last_modified,
        "listings_updated": listings_updated,
        "events_exist": (DATA / "events" / f"{sell}.parquet").exists(),
    }


@app.get("/api/config")
def api_config() -> dict:
    return {"default_sell": getattr(app.state, "default_sell", None)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "dashboard.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "login.html")


@app.get("/register")
def register_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "register.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sell", type=int, required=True,
                    help="default sell-realm connected-realm id (prefills the UI)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    import uvicorn
    app.state.default_sell = args.sell
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
