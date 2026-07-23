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
import datetime
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import analyze
import billing
import blizz
import snipe_check
from auth import UserCreate, UserRead, auth_backend, current_active_user, current_subscribed_user, fastapi_users
from db import User
from item_names import NameCache

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Root-logger console handler -- without this, log.info() calls here and in
# collect_all.py (population summary, per-cycle stats) are silently dropped
# (Python's root logger defaults to WARNING), and Railway's `railway logs`
# only captures stdout/stderr, so nothing else would ever surface them.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dashboard")

# Server-side collection (Stage 4) -- off by default so a local `python
# dashboard.py` run for quick dev/testing doesn't also spin up a background
# all-realm collector. Railway sets this to "true".
ENABLE_BACKGROUND_COLLECTION = os.environ.get("ENABLE_BACKGROUND_COLLECTION", "false").lower() == "true"
# Blizzard republishes AH data roughly hourly, but at no fixed clock time --
# polling only once an hour from whenever this container happened to boot
# could sit out of phase with the real publish moment by up to an hour.
# Poll every ~10 min instead (matching fetch_snapshot.py's own original
# local-collector cadence) so the lag after a real update stays small;
# fetch_once()'s If-Modified-Since check keeps the no-op polls cheap.
COLLECTION_INTERVAL_SECONDS = 10 * 60

# Real production data (this deployment's own /log page, 2026-07-23 evening)
# showed the "no fixed clock time" assumption above was overly cautious for
# at least this realm: 7 consecutive Draenor retrievals all landed within a
# ~1.5-minute band around :19-:20 past the hour. Poll tightly through a
# generous window around that mark so a real update gets caught within
# TIGHT_INTERVAL_SECONDS instead of up to the full 10-minute baseline; fall
# back to the normal cadence the rest of the hour so total request volume
# for a quiet 44 minutes/hour barely changes. The window is deliberately
# wider (16 min) than the observed band (~1.5 min) since this schedule is
# shared across every deep-collected realm, not tuned per-realm -- other
# realms likely publish at a slightly different offset. Revisit with a
# per-realm learned offset if this window turns out too narrow/wide once
# more realms have enough /log history to check.
TIGHT_WINDOW_START_MINUTE = 12
TIGHT_WINDOW_END_MINUTE = 28
TIGHT_INTERVAL_SECONDS = 45


def _next_poll_interval_seconds(now: datetime.datetime | None = None) -> int:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if TIGHT_WINDOW_START_MINUTE <= now.minute < TIGHT_WINDOW_END_MINUTE:
        return TIGHT_INTERVAL_SECONDS
    return COLLECTION_INTERVAL_SECONDS


async def _collection_loop() -> None:
    import collect_all as collect_all_module
    while True:
        try:
            await asyncio.to_thread(collect_all_module.collect_all)
        except Exception:
            log.exception("background collection cycle failed")
        await asyncio.sleep(_next_poll_interval_seconds())


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if ENABLE_BACKGROUND_COLLECTION:
        log.info("starting background collection loop (every %ss, or %ss during the "
                 "expected publish window :%s-:%s past the hour)",
                 COLLECTION_INTERVAL_SECONDS, TIGHT_INTERVAL_SECONDS,
                 TIGHT_WINDOW_START_MINUTE, TIGHT_WINDOW_END_MINUTE)
        task = asyncio.create_task(_collection_loop())
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title="AH Snipe Dashboard", lifespan=lifespan)

app.include_router(fastapi_users.get_auth_router(auth_backend), prefix="/auth", tags=["auth"])
app.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"])
app.include_router(billing.router)

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
        "sell_now_g": r["sell_now_g"],
        "sell_now_copper": r["sell_now_copper"],
        "appearance_sources": r["appearance_sources"],
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
    return {
        "email": user.email,
        "subscription_status": user.subscription_status,
        "subscription_current_period_end": (
            user.subscription_current_period_end.isoformat()
            if user.subscription_current_period_end else None
        ),
        "has_stripe_customer": user.stripe_customer_id is not None,
        "is_superuser": user.is_superuser,
    }


@app.get("/api/snipes")
def api_snipes(sell: int, items: str | None = None, min_discount: float = 0.3,
                min_per_day: float = 0.5, sell_percentile: float = 0.25,
                min_gold: float | None = None, max_gold: float | None = None,
                min_sell_now: float | None = None,
                min_sales: int = 2, max_appearance_sources: int | None = None,
                max_per_item: int | None = None,
                top: int = 50, sort: str = Query("discount"), names: bool = False,
                user: User = Depends(current_subscribed_user)) -> dict:
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
                                   min_gold=min_gold, max_gold=max_gold, min_sell_now=min_sell_now,
                                   min_sales=min_sales,
                                   max_appearance_sources=max_appearance_sources,
                                   max_per_item=max_per_item,
                                   top=top, sort=sort)
    name_cache = NameCache() if names else None
    out_rows = [_row_to_json(r, name_cache) for r in rows]
    if name_cache is not None:
        name_cache.save()
    return {
        "rows": out_rows, "count": len(out_rows), "caveat": snipe_check.CAVEAT,
        "region": blizz.REGION, "sell_realm_slug": _realm_info(sell)["slug"],
    }


def _list_collected_realms() -> list[int]:
    """A realm is "collected" once diff_snapshots.py has produced its events
    file -- that's the same precondition /api/snipes already 400s on, so this
    is exactly the set of realms the picker can usefully offer."""
    events_dir = DATA / "events"
    if not events_dir.exists():
        return []
    ids = []
    for p in events_dir.glob("*.parquet"):
        try:
            ids.append(int(p.stem))
        except ValueError:
            continue
    return sorted(ids)


def _realms_payload(cr_ids: list[int]) -> list[dict]:
    realms = []
    for cr_id in cr_ids:
        info = _realm_info(cr_id)
        realms.append({"id": cr_id, "name": info.get("name") or str(cr_id), "slug": info.get("slug")})
    realms.sort(key=lambda r: r["name"])
    return realms


@app.get("/api/realms")
def api_realms(user: User = Depends(current_subscribed_user)) -> dict:
    return {"realms": _realms_payload(_list_collected_realms())}


def _list_snapshotted_realms() -> list[int]:
    """Every realm with at least one raw snapshot file -- the precondition
    for /api/log, distinct from _list_collected_realms() (which requires
    diff_snapshots.py to have run). In practice these almost always
    coincide since collect_all.py re-diffs on every new snapshot, but the
    log is about *retrieval*, not inference, so it checks the raw source."""
    snap_dir = DATA / "snapshots"
    if not snap_dir.exists():
        return []
    ids = []
    for p in snap_dir.iterdir():
        if p.is_dir() and any(p.glob("*.parquet")):
            try:
                ids.append(int(p.name))
            except ValueError:
                continue
    return sorted(ids)


@app.get("/api/log/realms")
def api_log_realms() -> dict:
    """Public -- unlike /api/realms, this powers the public /log page (no
    login required, see CLAUDE.md's "Auction house API log" entry): realm
    names/slugs aren't sensitive, they're just Blizzard's own public realm
    directory, so there's nothing to gate here."""
    return {"realms": _realms_payload(_list_snapshotted_realms())}


@app.get("/api/log")
def api_log(sell: int) -> dict:
    """Public: every timestamp a NEW auction-house snapshot was actually
    retrieved for this realm, newest first. Backed directly by
    data/snapshots/{sell}/*.parquet filenames (each is the epoch second of
    that snapshot's Last-Modified header) rather than a separate log --
    fetch_snapshot.py's If-Modified-Since check means a file only gets
    written when Blizzard actually published something new, so the file
    list already *is* an honest, complete retrieval log with no extra
    logging infrastructure needed."""
    snap_dir = DATA / "snapshots" / str(sell)
    if not snap_dir.exists():
        raise HTTPException(404, f"no snapshots collected for realm {sell}")
    timestamps = sorted((int(p.stem) for p in snap_dir.glob("*.parquet")), reverse=True)
    info = _realm_info(sell)
    return {
        "realm_id": sell,
        "realm_name": info.get("name") or str(sell),
        "count": len(timestamps),
        "timestamps": timestamps,
    }


@app.get("/api/status")
def api_status(sell: int, user: User = Depends(current_subscribed_user)) -> dict:
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


@app.get("/subscribe")
def subscribe_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "subscribe.html")


@app.get("/profile")
def profile_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "profile.html")


@app.get("/log")
def log_page() -> FileResponse:
    """Public page, no auth check -- unlike every other page here, it must
    NOT redirect to /login on a 401 since its APIs never return one."""
    return FileResponse(ROOT / "static" / "log.html")


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
