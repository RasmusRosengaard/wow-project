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
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import admin
import analyze
import auth
import billing
import blizz
import db
import fetch_snapshot
import forum
import mailer
import snipe_check
import speed_check
import watchlist
import wow_accounts
from auth import (UserCreate, UserRead, auth_backend, current_active_user, current_subscribed_user,
                  fastapi_users, has_active_subscription)
from db import User, get_async_session
from fetch_snapshot import ilvl_plausible
from item_names import LIVE_RESOLVE_DEADLINE_SECONDS, NameCache

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Root-logger console handler -- without this, log.info() calls here and in
# collect_all.py (population summary, per-cycle stats) are silently dropped
# (Python's root logger defaults to WARNING), and Railway's `railway logs`
# only captures stdout/stderr, so nothing else would ever surface them.
# stream=sys.stdout explicitly (2026-08-01, real bug fix -- an earlier same-
# day fix to fetch_snapshot.py's/scan_region.py's own setup_logging() never
# actually took effect in production: those functions are only called when
# those files run standalone via their own __main__ block, not when
# collect_all.py calls fetch_snapshot.fetch_once()/scan_region.sweep() as
# library functions from inside this process's background loop -- in that
# path, the "collector"/"scanner" loggers have no handlers of their own, so
# messages propagate to *this* root-logger handler instead, which is the
# one that was actually still defaulting to stderr. basicConfig() with no
# stream argument defaults to sys.stderr, same root cause as the other fix
# -- Railway tags anything on stderr as "severity":"error" regardless of
# the real Python log level, confirmed still happening live after the
# scan_region.py/fetch_snapshot.py fix alone).
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout)
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
# 10 min -> 60s (2026-08-05, human request: "get the notification as fast as
# possible"). Safe to shorten only because collect_all() no longer runs its
# two prewarm passes every cycle -- those are now pinned to their own wall
# clock (PREWARM_MIN_INTERVAL_SECONDS), so cadence and request bill are
# independent knobs. Before that change this line was effectively load-
# bearing for the rate limit: at ~1,628 requests a cycle, cutting the
# interval to 60s would have meant ~35,800 req/h against a 36,000/h ceiling.
# A cycle's own auction work is only ~128 requests (36 deep realms + ~92
# sweep), so the real floor now is how long a sweep takes (~1-2 min,
# sequential), not this number -- the loop sleeps *after* the cycle, so a
# short interval just means "start again promptly", never overlapping runs.
COLLECTION_INTERVAL_SECONDS = 60

# Real production data (this deployment's own /log page, 2026-07-23 evening)
# showed the "no fixed clock time" assumption above was overly cautious for
# at least this realm: 7 consecutive Draenor retrievals all landed within a
# ~1.5-minute band around :19-:20 past the hour. Poll tightly through a
# generous window around that mark so a real update gets caught within
# TIGHT_INTERVAL_SECONDS instead of up to the full 10-minute baseline; fall
# back to the normal cadence the rest of the hour so total request volume
# for a quiet 44 minutes/hour barely changes.
# Window narrowed 2026-08-02 (human-confirmed, more observations than the
# original 7: Blizzard "always" publishes between :18 and :26) from an
# earlier, more cautious :12-:28 estimate -- still wider (8 min) than the
# originally observed ~1.5-minute band since this schedule is shared across
# every deep-collected realm, not tuned per-realm -- other realms likely
# publish at a slightly different offset.
#
# Re-aimed :18-:26 -> :38-:48 on 2026-08-05 (human-confirmed): Blizzard
# re-phased Draenor's publish slot outright, it did not drift. Two
# consecutive Last-Modified values, 07:44:43 and 08:41:26 UTC (56m43s
# apart, so the hourly cadence itself was unchanged), both landed ~20
# minutes past the old window -- which had already crept from the
# originally observed :19-:20 to ~:23 before the step change. Every
# publish was therefore missing the tight window entirely and falling back
# to the 10-minute baseline: ~5 min average detection lag, 10 min worst
# case, against 45s when the window is aimed correctly. Freshness only, no
# correctness impact. Width kept deliberately modest (10 min) rather than
# spanning both the old and new slots, because collect_all() runs a *full
# unconditional region sweep every cycle* (~92 realms, no
# If-Modified-Since -- see collect_all.py) so a cycle costs ~127 requests
# and window width translates near-linearly into request volume.
#
# Revisit with a per-realm learned offset if this window turns out too
# narrow/wide -- now the more likely endgame than another hand-aimed
# window, since a slot that re-phases once will re-phase again, and one
# shared window cannot fit every deep-collected realm's own offset.
TIGHT_WINDOW_START_MINUTE = 38
TIGHT_WINDOW_END_MINUTE = 48
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
    tasks = []
    if ENABLE_BACKGROUND_COLLECTION:
        log.info("starting background collection loop (every %ss, or %ss during the "
                 "expected publish window :%s-:%s past the hour)",
                 COLLECTION_INTERVAL_SECONDS, TIGHT_INTERVAL_SECONDS,
                 TIGHT_WINDOW_START_MINUTE, TIGHT_WINDOW_END_MINUTE)
        tasks.append(asyncio.create_task(_collection_loop()))
    # Gated on DATABASE_URL rather than started unconditionally: db.engine()
    # raises SystemExit (a BaseException, which the loop's own `except
    # Exception` deliberately does not swallow) when it's unset, and a bare
    # local `python dashboard.py` run legitimately has no database. Same
    # spirit as ENABLE_BACKGROUND_COLLECTION defaulting off for local dev.
    if os.environ.get("DATABASE_URL"):
        log.info("starting visitor activity flush loop (every %ss)",
                 admin.FLUSH_INTERVAL_SECONDS)
        tasks.append(asyncio.create_task(admin._visitor_flush_loop()))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Realm Arbitrage", lifespan=lifespan)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """FileResponse/StaticFiles set Last-Modified/ETag but no Cache-Control,
    so browsers fall back to heuristic caching and can keep serving a page
    from before the latest deploy with no way to know it's stale -- a real
    live bug, 2026-07-31: a bought-out listing kept showing on a user's
    dashboard because their browser silently reused a cached dashboard.html
    from before that day's fix shipped, even on a plain reload. `no-cache`
    (not `no-store`) forces a conditional revalidation request every load --
    the server still answers with a cheap 304 when nothing changed, so this
    doesn't turn into a full re-download on every visit, it just guarantees
    the browser can never skip asking."""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Visitor activity tracking lives in admin.py (moved there 2026-08-04, when
# it grew from an in-memory debugging convenience into a persisted history
# with its own admin page). Middleware is a property of the app rather than
# of a router, so registration has to happen here even though the
# implementation doesn't.
app.middleware("http")(admin.track_activity)


@app.middleware("http")
async def ensure_anon_cookie(request: Request, call_next):
    """Sets the ah_anon cookie on the genuinely final response (2026-08-03),
    not a route-local injected `response: Response` parameter -- real bug
    found while testing: FastAPI's exception handling builds an entirely
    separate Response when a route raises HTTPException, and /api/snipes
    routinely raises HTTPException for anonymous requests (400 for an
    uncollected realm, 403 for a different-realm lock) -- the *common* case
    for a first-time anonymous visitor, not an edge case, so a fix that only
    covered the 200-success path would have left the cookie unset almost
    every real time it mattered. auth.resolve_or_create_anon_session stashes
    the resolved token on request.state.anon_token; middleware wraps the
    whole request/response cycle, so call_next()'s returned response is the
    real one about to be sent regardless of whether a route inside returned
    normally or raised. Only sets the cookie when it's actually new/changed
    (comparing against the incoming request cookie) so an already-valid
    session doesn't get a redundant Set-Cookie on every single call."""
    response = await call_next(request)
    token = getattr(request.state, "anon_token", None)
    if token is not None and request.cookies.get(auth.ANON_COOKIE_NAME) != token:
        response.set_cookie(auth.ANON_COOKIE_NAME, token, max_age=auth.COOKIE_MAX_AGE_SECONDS,
                            httponly=True, samesite="lax", secure=auth.COOKIE_SECURE)
    return response


app.include_router(admin.router)
# requires_verification=True (2026-08-09, human decision) makes a confirmed
# address a hard gate on **password** login: /auth/login answers 400
# LOGIN_USER_NOT_VERIFIED until the emailed link is clicked. This reverses the
# soft gate chosen on 2026-08-06 -- see .claude/docs/history.md.
#
# Google is deliberately unaffected, and needs no code here to stay that way:
# get_oauth_router below runs with is_verified_by_default=True, and
# auth.UserManager.oauth_callback covers the associate-by-email path, so a
# Google account is already verified by the time any login check runs.
#
# Only this router is gated. The anonymous tier, /api/snipes, /api/me and the
# rest of the free tier stay reachable without an account at all -- the gate is
# on logging *in*, not on what a logged-in free user may do.
app.include_router(fastapi_users.get_auth_router(auth_backend, requires_verification=True),
                   prefix="/auth", tags=["auth"])
app.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"])
# Email verification + password reset (2026-08-06). Both routers only mint and
# consume tokens; the actual sending happens in auth.UserManager's hooks via
# mailer.py, so these are mounted unconditionally -- with no RESEND_API_KEY the
# token still gets generated and logged, which is how local dev works.
#   /auth/request-verify-token  POST {email}          -> 202, always (no account enumeration)
#   /auth/verify                POST {token}          -> the verified user
#   /auth/forgot-password       POST {email}          -> 202, always
#   /auth/reset-password        POST {token, password}
app.include_router(fastapi_users.get_verify_router(UserRead), prefix="/auth", tags=["auth"])
app.include_router(fastapi_users.get_reset_password_router(), prefix="/auth", tags=["auth"])
# Google login (2026-08-06). Mounted only when credentials are configured, so a
# fresh checkout/CI can still import and serve everything else; /api/auth-config
# reports whether it's live so login.html can hide a button that would 404.
#
# Three arguments are load-bearing:
#   redirect_url  -- passed explicitly rather than letting the router call
#                    request.url_for(), whose scheme comes from proxy headers
#                    this app doesn't configure. Google matches redirect_uri
#                    byte-for-byte; an http:// guess behind Railway's HTTPS
#                    proxy fails the whole flow. See auth.public_base_url().
#   associate_by_email=True -- a Google login on an address that already has a
#                    password account links into that ONE account instead of
#                    erroring (human decision). Safe specifically because
#                    Google guarantees the address is verified; the standard
#                    warning against this setting applies to providers that
#                    don't, so it must not be copied blindly to a future one.
#   is_verified_by_default=True -- for the same reason: Google already proved
#                    the address, so demanding our own confirmation email on
#                    top would be pure friction.
# csrf_token_cookie_secure defaults to True and would silently drop the state
# cookie over local http:// -- exactly the trap auth.py documents for ah_auth,
# so it's wired to the same COOKIE_SECURE switch.
if auth.google_oauth_client is not None:
    app.include_router(
        fastapi_users.get_oauth_router(
            auth.google_oauth_client,
            auth.oauth_redirect_backend,
            auth.SECRET,
            redirect_url=f"{auth.public_base_url()}{auth.GOOGLE_CALLBACK_PATH}",
            associate_by_email=True,
            is_verified_by_default=True,
            csrf_token_cookie_secure=auth.COOKIE_SECURE,
        ),
        prefix="/auth/google", tags=["auth"])
app.include_router(billing.router)
app.include_router(forum.router)
app.include_router(forum.image_router)
app.include_router(wow_accounts.router)
app.include_router(watchlist.router)

# Realm name/slug never changes -- cache in-process for the life of the
# server rather than a file cache, to keep this dashboard-only concern out of
# item_names.py's on-disk cache.
_realm_info_cache: dict[int, dict] = {}

# The modifier-28 "item level" isn't officially documented (see CLAUDE.md);
# it's clearly wrong for items outside the modern ilvl-scaling system -- e.g.
# a classic fixed-stat wand showed "ilvl 1112" despite a catalog level in the
# 30s (caught 2026-07-23), and a modern raid item showed "ilvl 3031" against
# a real base of 610 (caught 2026-07-25). ilvl_plausible()/ILVL_* constants
# now live in fetch_snapshot.py, not here -- market_key() needs the exact
# same check (2026-07-25, see its docstring) to decide whether to pool an
# implausible type-28 value across listings, so this is the single source of
# truth for both display and matching rather than two copies that could
# silently drift apart.


def _realm_info(cr_id: int) -> dict:
    if cr_id not in _realm_info_cache:
        try:
            realms = blizz.connected_realm_realms(cr_id)
        except Exception:
            realms = []
        _realm_info_cache[cr_id] = realms[0] if realms else {"name": None, "slug": None, "category": None}
    return _realm_info_cache[cr_id]


def _parse_variant(bonus_key: str) -> dict:
    """Pull the item-level modifier (type 28) out of the raw bonus_key for a
    readable summary, without discarding the rest -- the raw string still
    rides along in the response for the tooltip."""
    parsed = fetch_snapshot.parse_bonus_key(bonus_key)
    return {"ilvl": parsed["mods"].get(28) or None, "bonus_count": len(parsed["bonus_ids"])}


def _variant_label(bk: str, item_id: int, names: NameCache | None) -> str:
    parsed = _parse_variant(bk) if bk else {"ilvl": None, "bonus_count": 0}
    ilvl_ok = False
    if parsed["ilvl"] and names is not None:
        base = names.base_level(item_id)
        ilvl_ok = ilvl_plausible(int(parsed["ilvl"]), base)
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
        # Language community the buy-side realm belongs to (2026-07-31, human
        # request) -- straight from Blizzard's own per-realm "category" field
        # (see blizz.connected_realm_realms()'s docstring), display-only, not
        # used for matching/filtering. None for a realm _realm_info() never
        # resolved (a transient Blizzard API failure).
        "buy_realm_category": _realm_info(r["buy_realm"]).get("category"),
        "item_id": r["item_id"],
        "variant": variant,
        "variant_raw": r["bonus_key"] or None,
        # pet_species_id/pet_quality_id (added 2026-07-26 alongside the
        # matching change -- see snipe_check.find_snipes()'s docstring):
        # matching/grouping is now (item_id, pet_species_id, pet_quality_id)
        # -- bonus/ilvl no longer distinguishes a match at all, so
        # dashboard.html groups rows by these instead of the old market_key.
        # Every caged pet shares one item_id (82800), so without these two
        # fields every pet species/quality would wrongly collapse into one
        # display group; NULL for ordinary gear, a no-op there.
        "pet_species_id": r["pet_species_id"],
        "pet_quality_id": r["pet_quality_id"],
        "buy_g": r["buy_g"],
        # sell_p_g/sell_copper is the sell realm's current cheapest live
        # listing (changed 2026-07-25 -- see snipe_check.find_snipes()'s
        # docstring for why the sold-price-percentile model was dropped).
        # No longer a separate "sell_now" field distinct from this -- they
        # were made the same number, so only one is kept.
        "sell_p_g": r["sell_p_g"],
        "appearance_sources": r["appearance_sources"],
        "buy_copper": r["buy_copper"],
        "sell_copper": r["sell_copper"],
        "discount_pct": r["discount_pct"],
        # EU median (added 2026-07-27, human request): the median (not mean --
        # see snipe_check.find_snipes()'s docstring for why median specifically)
        # current cheapest listing for this item across the rest of the
        # scanned region. Purely informational display column, doesn't gate
        # or filter anything.
        "region_median_g": r["region_median_g"],
        "region_median_copper": r["region_median_copper"],
        # price_suspect (added 2026-08-03, human request -- see
        # snipe_check.PRICE_SUSPECT_MULTIPLE's docstring): the sell realm's
        # own reference price is >= 10x the region median, a strong troll/
        # joke-listing signal on the *sell* side. Unconditional, same as
        # region_median_g above (pure SQL, no NameCache lookup needed) --
        # unlike sus_item_suspect this isn't gated behind names=true.
        # Never filters server-side -- dashboard.html's "Hide flagged
        # (sniper filter)" checkbox ORs this in alongside sus_item_suspect.
        "price_suspect": r["price_suspect"],
        # sniper_filter_suspect ("Sniper filter", added 2026-08-04, human
        # request -- see snipe_check.SNIPER_FILTER_N's docstring): this
        # buy-side candidate's price is corroborated by several other
        # unique realms clustering near it, so it's probably not actually
        # rare -- the "snipe" is more likely the sell realm being pricier
        # than usual, not this listing being a real steal. Same pure-SQL,
        # unconditional passthrough as price_suspect above; never filters
        # server-side -- dashboard.html's "Hide flagged (sniper filter)"
        # checkbox ORs it in alongside the other two.
        "sniper_filter_suspect": r["sniper_filter_suspect"],
        # TSM EU region-wide sale rate/sold-per-day (added 2026-08-01, human
        # request -- see snipe_check.find_snipes()'s min_sale_rate docstring
        # and tsm.py). Both None if TSM has no data for this item (never
        # tracked, a caged pet, or a cache that hasn't been refreshed yet).
        # Purely informational unless the client's own min-sale-rate filter
        # is set, same client-side-filtering convention every other
        # threshold in the rail already uses (see batchParams()'s comment
        # in dashboard.html) -- not passed as a server query param here.
        "region_sale_rate": r["region_sale_rate"],
        "region_sold_per_day": r["region_sold_per_day"],
        # TSM EU region-wide average sale price (added 2026-08-03, human
        # request -- "region sale avg from tsm, if it exist"), already in
        # copper -- same None-if-untracked convention as region_sale_rate
        # above (see snipe_check._filter_by_sale_rate()/tsm.py). Purely
        # informational, gates nothing -- same role region_median_copper
        # already has.
        "region_sale_avg_copper": r["region_sale_avg_copper"],
    }
    if names is not None:
        out["name"] = names.get(r["item_id"], r["pet_species_id"])
        out["icon"] = names.icon(r["item_id"], r["pet_species_id"])
        out["quality_color"] = names.quality_color(r["item_id"], r["pet_species_id"], r["pet_quality_id"])
        # Tier name (e.g. "EPIC"), not just the ring color -- backs the
        # dashboard's rarity filter (same client-side, OR'd-checkboxes
        # pattern as the item-class filter below).
        out["quality"] = names.quality(r["item_id"], r["pet_species_id"], r["pet_quality_id"])
        # Official Blizzard item_class/item_subclass ids (see item_names.py),
        # not the appearance_sources/max_appearance_sources filter above --
        # this backs the dashboard's item-class filter (weapon/armor/
        # container/profession/housing/battle pet/quest/mount), applied
        # entirely client-side against the cached batch, same as discount%/
        # gold/sell-now.
        out["item_class"] = names.item_class(r["item_id"])
        out["item_subclass"] = names.item_subclass(r["item_id"])
        # Same check find_snipes()'s --max-appearance-sources uses to
        # exclude profession tool/accessory slots from "unique transmog" --
        # exposed here (unconditionally, not just when that filter is
        # active) so the dashboard's client-side "Unique transmog only"
        # toggle can reproduce it exactly with zero extra API calls: it's
        # already fetched as a side effect of the same NameCache lookup
        # that resolved name/icon/quality above.
        out["is_profession_item"] = names.inventory_type(r["item_id"]) in snipe_check.NON_TRANSMOG_INVENTORY_TYPES
        # sus_item_suspect (added 2026-07-31 as legacy_jewelry_suspect, then
        # legacy_gear_suspect, renamed again same day to "sus items" per
        # human preference): flags old neck/ring/trinket items and the
        # confirmed class-starter armor pieces -- see
        # snipe_check.is_sus_item()'s comment for the live-verified examples
        # and its known twink-market blind spot. Same NameCache-driven
        # pattern as is_profession_item above -- costs nothing extra,
        # base_level()/inventory_type() are resolved by the same
        # _fetch_item_details() call that already backs item_class/quality.
        # Never filters server-side -- dashboard.html's "Hide flagged
        # (sniper filter)" checkbox is the only thing that can hide it.
        # item_class/item_subclass added 2026-08-05 for the Junk rule --
        # both come from the same already-cached _fetch_item_details() call
        # as inventory_type/base_level, so this stays free.
        out["sus_item_suspect"] = snipe_check.is_sus_item(
            r["item_id"], names.inventory_type(r["item_id"]), names.base_level(r["item_id"]),
            names.item_class(r["item_id"]), names.item_subclass(r["item_id"]),
            names.quality(r["item_id"]), names.get(r["item_id"]),
            names.purchase_price(r["item_id"]))
    return out


@app.get("/api/me")
async def api_me(request: Request, session: AsyncSession = Depends(get_async_session)) -> dict:
    """No longer gated by Depends(current_active_user) (changed 2026-08-03,
    alongside letting anonymous visitors use /snipes) -- it needs to return
    200 for a visitor with no account too, not 401. Resolves the real user
    manually via auth.resolve_user_from_request() (same helper api_snipes()
    already uses), and falls back to an anonymous session
    (auth.resolve_or_create_anon_session(), minting one on a first visit --
    see that function's docstring and dashboard.ensure_anon_cookie for why
    the cookie itself is set by middleware, not here) when there's no real
    user. This route does no slow work, so (unlike api_snipes()) it's fine
    to keep the pooled Depends(get_async_session) dependency here."""
    user = await auth.resolve_user_from_request(request)
    if user is not None:
        return {
            "email": user.email,
            "subscription_status": user.subscription_status,
            "subscription_current_period_end": (
                user.subscription_current_period_end.isoformat()
                if user.subscription_current_period_end else None
            ),
            "has_stripe_customer": user.stripe_customer_id is not None,
            "is_superuser": user.is_superuser,
            # Drives the "confirm your email" banner and the disabled state on
            # the three soft-gated actions (2026-08-06) -- see
            # auth.current_verified_user. Sending it here lets the frontend
            # explain the gate up front instead of letting the user discover it
            # by getting a 403 mid-action.
            "is_verified": user.is_verified,
            # Free tier only -- None for a subscriber/superuser (never enforced
            # for them) and for a free account that hasn't queried /api/snipes
            # yet. dashboard.html uses this to pre-select and lock the realm
            # dropdown before the user can even attempt a request that would
            # 403, rather than letting them find out by trying.
            "locked_sell_realm": user.locked_sell_realm,
            # Public display name for the Snipe Board (see forum.py) -- None
            # until the user has set one. snipeboard.html prompts for it inline
            # the first time this account tries to post.
            "nickname": user.nickname,
            "is_anonymous": False,
        }
    anon_token = await auth.resolve_or_create_anon_session(request, session)
    locked = (await session.execute(
        select(db.AnonSession.locked_sell_realm).where(db.AnonSession.token == anon_token)
    )).scalar_one()
    return {
        "email": None,
        "subscription_status": None,
        "subscription_current_period_end": None,
        "has_stripe_customer": False,
        "is_superuser": False,
        # Stated explicitly rather than omitted, matching this response's
        # existing style (has_stripe_customer, is_superuser) -- a visitor with
        # no account has no confirmed address, and the banner logic keys off
        # is_anonymous first anyway.
        "is_verified": False,
        "locked_sell_realm": locked,
        "nickname": None,
        # Explicit boolean (matches this response's existing style, e.g.
        # has_stripe_customer, over having the client infer anonymity from
        # email being null) -- dashboard.html's nav-swap and nudge-banner
        # logic both key off this directly.
        "is_anonymous": True,
        # Included so the nudge banner's copy reads these from the server
        # instead of hardcoding a second, driftable copy of the numbers.
        "anon_cap": ANON_SNIPE_CAP,
        "free_cap": SNIPE_TIER_CAPS["free"],
    }


NICKNAME_MAX_LEN = 50


class NicknameUpdate(BaseModel):
    nickname: str


@app.patch("/api/me/nickname")
async def update_nickname(payload: NicknameUpdate, user: User = Depends(current_active_user),
                          session: AsyncSession = Depends(get_async_session)) -> dict:
    nickname = payload.nickname.strip()
    if not nickname:
        raise HTTPException(400, "nickname can't be empty")
    if len(nickname) > NICKNAME_MAX_LEN:
        raise HTTPException(400, f"nickname must be {NICKNAME_MAX_LEN} characters or fewer")
    user.nickname = nickname
    await session.commit()
    return {"nickname": user.nickname}


# Free tier, added 2026-07-25 (human decision): a logged-in-but-unsubscribed
# user can now see the dashboard at all, capped to a small row budget --
# previously current_subscribed_user hard-gated everyone below "active
# subscription or superuser" straight to /subscribe with zero preview.
# Superuser isn't a real subscription tier, just generous founder/admin
# headroom. The client always requests the same generous top (dashboard.html's
# BATCH_TOP); the server is what actually enforces the real per-tier ceiling,
# so the frontend doesn't need to know its own tier ahead of time.
#
# free raised 250 -> 500 (2026-08-03, human decision, bundled with adding
# anonymous/no-account access -- see ANON_SNIPE_CAP below): the old 250
# number becomes the new "create a free account" incentive tier for a
# visitor with no account at all, and a real account now gets meaningfully
# more (500) as the actual reward for registering.
SNIPE_TIER_CAPS = {"free": 500, "subscribed": 2000, "superuser": 10000}

# Anonymous (no account at all) tier, added 2026-08-03 alongside the free-tier
# bump above -- deliberately today's *old* free-tier number, now used as the
# incentive to create a free account (see dashboard.html's anon nudge banner,
# which shows both ANON_SNIPE_CAP and SNIPE_TIER_CAPS["free"] side by side).
# A real, separately-named constant rather than an alias, since it no longer
# equals SNIPE_TIER_CAPS["free"] and the two are free to diverge further.
ANON_SNIPE_CAP = 250


def _snipe_cap(user: User) -> int:
    if user.is_superuser:
        return SNIPE_TIER_CAPS["superuser"]
    if user.subscription_status == "active":
        return SNIPE_TIER_CAPS["subscribed"]
    return SNIPE_TIER_CAPS["free"]


# Per-tier item-class quotas (added 2026-07-27, human-specified numbers) --
# passed to snipe_check.find_snipes()'s class_quotas param so a saturated
# category can't crowd every other one out of the batch (see
# snipe_check._apply_class_quotas()'s docstring for the real Housing case
# that motivated this). Free tier deliberately shows no Containers/
# Profession/Quest items at all (its 6 quotas sum to exactly its 250 cap) --
# a human product decision, not an oversight. Subscribed/superuser keep
# fixed floors for quest/profession/container (100/100/20, not scaled --
# free tier has zero of these to scale a ratio from) and scale the
# remaining budget using free tier's own weapon/armor/housing/mount/
# battlepet/recipe ratios (20%/40%/16%/2%/2%/20%).
#
# Recipe added 2026-07-28 (human decision): free tier's weapon quota was cut
# from 100 to 50 to make room for an equal 50 recipes (confirmed live via
# GET /data/wow/item-class/index that Recipe is item_class 9, distinct from
# Profession's 19 -- see snipe_check.CLASS_BUCKET_RULES), rather than raising
# the 250 free-tier cap itself. Subscribed/superuser weapon+recipe were
# rescaled the same way (each now 20% of the scaled budget instead of
# weapon's old 40%) -- one unit trimmed from weapon in both tiers, same as
# the pre-existing rounding-remainder convention below, to keep each tier's
# quotas summing to exactly its SNIPE_TIER_CAPS value.
#
# Doubled 2026-08-03 alongside SNIPE_TIER_CAPS["free"]'s 250 -> 500 bump --
# every value here is exactly 2x its old number, so the ratios (20%/40%/16%/
# 2%/2%/20%) SUBSCRIBED_CLASS_QUOTAS/SUPERUSER_CLASS_QUOTAS were derived from
# are unchanged, and those two dicts below did NOT need to change at all.
FREE_CLASS_QUOTAS = {
    "weapon": 100, "armor": 200, "housing": 80, "mount": 10, "battlepet": 10,
    "recipe": 100,
}
SUBSCRIBED_CLASS_QUOTAS = {
    "quest": 100, "profession": 100, "container": 20,
    "weapon": 355, "armor": 712, "housing": 285, "mount": 36, "battlepet": 36,
    "recipe": 356,
}
SUPERUSER_CLASS_QUOTAS = {
    "quest": 100, "profession": 100, "container": 20,
    "weapon": 1955, "armor": 3912, "housing": 1565, "mount": 196, "battlepet": 196,
    "recipe": 1956,
}

# Anonymous tier's class quotas (2026-08-03) -- the OLD FREE_CLASS_QUOTAS
# values, unchanged, kept as their own named constant now that the two have
# diverged (see ANON_SNIPE_CAP above). Sums to exactly ANON_SNIPE_CAP (250),
# same invariant the other three tiers' dicts are held to (see
# tests/test_dashboard.py::test_class_quotas_by_tier_sum_to_the_tier_cap and
# its anon-tier counterpart).
ANON_CLASS_QUOTAS = {
    "weapon": 50, "armor": 100, "housing": 40, "mount": 5, "battlepet": 5,
    "recipe": 50,
}


def _class_quotas(user: User) -> dict[str, int]:
    if user.is_superuser:
        return SUPERUSER_CLASS_QUOTAS
    if user.subscription_status == "active":
        return SUBSCRIBED_CLASS_QUOTAS
    return FREE_CLASS_QUOTAS


async def _enforce_realm_lock(user: User, sell: int, session: AsyncSession) -> None:
    """Free tier only (SNIPE_TIER_CAPS) -- to bound how many distinct
    expensive DuckDB queries a non-paying account can generate, it's locked
    to the first sell realm it ever queries via /api/snipes, written once on
    first use. A subscriber or superuser is never restricted -- checked via
    has_active_subscription so this stays in sync with every other
    subscription check in the app rather than re-deriving the same logic.

    Uses an atomic `UPDATE ... WHERE locked_sell_realm IS NULL` rather than
    the read-then-write ORM pattern this used to be (2026-07-31, real bug
    fix, human report during a repo-wide bug audit): `current_active_user`
    resolves a fresh `User` from a fresh session on every request, so two
    concurrent first-ever requests from the same free account -- for two
    *different* realms -- could both observe `locked_sell_realm is None`
    before either committed, both pass the old `if user.locked_sell_realm is
    None` check, and both proceed to run a real DuckDB query, defeating the
    lock's entire stated purpose. A single UPDATE statement's WHERE clause
    is evaluated and applied atomically at the database level -- exactly one
    of any number of concurrent requests can ever be the one that flips the
    column from NULL, so only that one gets `rowcount` back; every other
    concurrent loser looks up what the winner actually locked to (a plain
    SELECT, not session.refresh(user) -- that requires `user` to already be
    an attached/persistent instance in *this exact* session, which isn't
    guaranteed for every caller) rather than assuming it won.

    synchronize_session=False on the UPDATE (2026-07-31, found while testing
    the race fix above): SQLAlchemy's default "evaluate" synchronize strategy
    updates in-memory ORM objects already in this session's identity map by
    re-evaluating the WHERE clause against their *current Python-side
    attribute values* -- not by checking what the real SQL UPDATE actually
    matched in the database. In the exact race this function defends
    against, a session holding a stale (still-None) `user.locked_sell_realm`
    would get that attribute silently set to `sell` in memory by this
    synchronization step even when the real UPDATE matched zero rows
    (correctly, since the DB's actual value was already locked to something
    else) -- the in-memory object would disagree with the database. Disabled
    so `result.rowcount` (checked below) is the only source of truth.

    The atomic mechanism itself is factored out into _atomic_lock_first_realm
    (2026-08-03, added alongside the anonymous-visitor equivalent below,
    _enforce_anon_realm_lock) so both policies share one tested primitive
    instead of a second hand-copied UPDATE."""
    if has_active_subscription(user):
        return
    locked_to = await _atomic_lock_first_realm(
        session, User, User.id, user.id, User.locked_sell_realm, sell)
    user.locked_sell_realm = locked_to  # keep the in-memory object consistent too
    if locked_to != sell:
        raise HTTPException(
            403,
            f"Free tier is locked to sell realm {locked_to} -- "
            "subscribe to query any realm",
        )


async def _atomic_lock_first_realm(session: AsyncSession, model, pk_column, pk_value,
                                   lock_column, sell: int) -> int:
    """Core of the free-tier/anonymous realm lock (extracted 2026-08-03 from
    _enforce_realm_lock, whose original docstring above has the full
    reasoning for why this must be one atomic UPDATE...WHERE...IS NULL rather
    than read-then-write, and why synchronize_session=False is required):
    atomically claims `sell` as pk_value's locked realm if this is the
    first-ever request for this identity, otherwise returns whatever realm
    already won -- shared verbatim by _enforce_realm_lock (User) and
    _enforce_anon_realm_lock (db.AnonSession) so the two policies can never
    drift apart on the one part that actually has to be correct under
    concurrency."""
    result = await session.execute(
        update(model).where(pk_column == pk_value, lock_column.is_(None))
        .values({lock_column.key: sell})
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount:
        return sell
    return (await session.execute(select(lock_column).where(pk_column == pk_value))).scalar_one()


async def _enforce_anon_realm_lock(anon_token: str, sell: int, session: AsyncSession) -> None:
    """Anonymous-visitor equivalent of _enforce_realm_lock, added 2026-08-03
    to let a visitor with no account use /snipes while still bounding how
    many distinct expensive DuckDB queries one anonymous visitor can
    generate. No subscription bypass here (unlike _enforce_realm_lock) --
    there is no such thing as a subscribed anonymous session; subscribing
    means registering an account, which moves a visitor onto the User-based
    path entirely. Deliberately doesn't mention cookies anywhere in the error
    message (human decision -- don't hint that clearing cookies resets the
    lock)."""
    locked_to = await _atomic_lock_first_realm(
        session, db.AnonSession, db.AnonSession.token, anon_token, db.AnonSession.locked_sell_realm, sell)
    if locked_to != sell:
        raise HTTPException(
            403,
            f"Anonymous browsing is locked to sell realm {locked_to} -- "
            "log in or subscribe to query any realm",
        )


@app.get("/api/snipes")
async def api_snipes(request: Request, sell: int, items: str | None = None,
                min_discount: float = 0.3,
                min_gold: float | None = None, max_gold: float | None = None,
                min_sell_now: float | None = None,
                max_appearance_sources: int | None = None,
                max_per_item: int | None = None,
                top: int = 50, sort: str = Query("discount"), names: bool = False) -> dict:
    # auth.resolve_user_from_request(), not Depends(current_active_user)
    # (2026-08-01, real production incident: Postgres pool exhaustion broke
    # login under barely any traffic) -- current_active_user's Depends()
    # chain holds a pooled connection checked out for this route's entire
    # duration, including the 30-175s of unrelated DuckDB/Blizzard work
    # below; this resolves the user via its own short-lived session that
    # closes in milliseconds, well before any of that slow work starts. See
    # auth.resolve_user_from_request()'s own docstring for the full
    # reasoning and how it stays behaviorally identical to
    # current_active_user (only the `active` check applies -- this app
    # never uses verified=True/superuser=True).
    #
    # A None result (no cookie, an invalid/garbage cookie, or an inactive
    # account) no longer 401s (changed 2026-08-03, alongside letting
    # anonymous visitors use /snipes) -- it now falls through to the
    # anonymous path below instead. This is a deliberate widening: a stale
    # cookie degrading gracefully to anonymous browsing is better UX than a
    # hard failure, and there's no meaningful security boundary being
    # loosened -- both paths are already unauthenticated as far as this
    # route is concerned.
    user = await auth.resolve_user_from_request(request)
    # Realm lock enforced *before* data-readiness/sort validation --
    # deliberate (see test_auth.py::test_free_tier_locks_to_first_sell_realm,
    # which asserts exactly this order): an account (or anonymous visitor)
    # commits its one realm choice by asking about that realm at all,
    # whether or not this particular query turns out ready. (A bug audit
    # initially flagged this ordering as a bug and reversed it, but that
    # missed this existing, already-tested, deliberate behavior -- reverted
    # 2026-07-31 once the conflict was caught by the full test suite.)
    #
    # _enforce_realm_lock()'s own signature/behavior is unchanged (still
    # takes an explicit session -- its existing test_dashboard.py coverage,
    # including a real two-session TOCTOU race regression test, depends on
    # controlling that session precisely and would be awkward to preserve
    # otherwise). Only what changed is *where* the session comes from: a
    # short-lived one opened just for this call, not a route-level
    # Depends(get_async_session) session that would stay open for the rest
    # of this function's slow work too. The anonymous branch below follows
    # the exact same short-lived-session discipline.
    async with db.sessionmaker()() as session:
        if user is not None:
            await _enforce_realm_lock(user, sell, session)
            cap, quotas = _snipe_cap(user), _class_quotas(user)
        else:
            anon_token = await auth.resolve_or_create_anon_session(request, session)
            await _enforce_anon_realm_lock(anon_token, sell, session)
            cap, quotas = ANON_SNIPE_CAP, ANON_CLASS_QUOTAS
    top = min(top, cap)
    not_ready = snipe_check.check_data_ready(sell)
    if not_ready:
        raise HTTPException(400, not_ready)
    if sort not in snipe_check.SORT_COLUMNS:
        raise HTTPException(400, f"sort must be one of {sorted(snipe_check.SORT_COLUMNS)}")

    item_ids = snipe_check.parse_items(items, None)

    def _run_query() -> list:
        # con.close() in `finally` (added 2026-08-01, human report of rising
        # memory usage): this used to leave the connection to Python's own
        # GC to collect whenever it got around to it -- fine for the
        # DuckDB-side Python wrapper object itself, but class_quotas (always
        # active, every tier, every call -- see find_snipes()'s docstring)
        # materializes the *entire* unfiltered region-wide candidate set
        # into a TEMP TABLE with no row-count cap (confirmed live: 450,568
        # rows on Draenor alone), and DuckDB's own native (non-Python-heap)
        # buffers for that aren't guaranteed to be released back to the OS
        # until the connection is explicitly closed, not just dereferenced.
        # Explicit close() makes DuckDB free that memory deterministically
        # on every request instead of hoping GC timing lines up.
        con = analyze.connect(sell)
        try:
            return snipe_check.find_snipes(con, sell, items=item_ids, min_discount=min_discount,
                                           min_gold=min_gold, max_gold=max_gold, min_sell_now=min_sell_now,
                                           max_appearance_sources=max_appearance_sources,
                                           max_per_item=max_per_item,
                                           class_quotas=quotas,
                                           # Junk/decoy pre-filter (2026-08-01, human
                                           # request), always on for the live API --
                                           # see snipe_check.MIN_VALUE_FLOOR_G's
                                           # comment. Frees the tier's top-N/class-
                                           # quota budget from rows that are under
                                           # MIN_VALUE_FLOOR_G by both sell price and
                                           # EU median at once, not user-configurable (the
                                           # existing min_sell_now query param is
                                           # the user-adjustable floor, this is a
                                           # baseline beneath it).
                                           min_value_floor_g=snipe_check.MIN_VALUE_FLOOR_G,
                                           top=top, sort=sort)
        finally:
            con.close()

    # find_snipes() can still make blocking Blizzard API calls mid-query --
    # when max_appearance_sources is set, _filter_by_appearance() calls
    # NameCache().inventory_type() per candidate row, a cache-miss fallback
    # to a real API call (see item_names.py). (Before 2026-07-26 this route
    # also ran _populate_market_keys()'s base-level/noise-bonus-id
    # resolution here -- removed along with market_key-based matching, see
    # snipe_check.find_snipes()'s docstring; that was the original trigger
    # for needing to_thread() below, confirmed live 2026-07-25 as a real
    # outage after a fresh deploy hit a cold NameCache and had to resolve
    # many never-before-seen items sequentially, freezing the *entire*
    # single-process server for every other request. Still true today for
    # the appearance-filter path, so to_thread() stays.) Running any of
    # this directly in this async route body would freeze the whole server
    # for as long as it takes -- to_thread() keeps the rest of the app
    # responsive while this one request does its (possibly slow) work.
    rows = await asyncio.to_thread(_run_query)

    # names=true's per-row NameCache lookups (name/icon/quality/item_class/...)
    # each fall back to a blocking Blizzard API call on a cache miss (see
    # item_names.py) -- on a sell realm queried for the first time, most rows'
    # items have never been resolved, so this loop used to run item-by-item,
    # synchronously, directly on the event loop: real production symptom
    # confirmed live 2026-07-26, a realm switch to any never-before-queried
    # realm hung until the request/proxy timed out (the same class of bug as
    # the 2026-07-25 outage this route's _run_query offload already fixed --
    # that fix only covered find_snipes()'s own blocking calls, not this
    # separate per-row translation step added afterward). Fixed the same way
    # (asyncio.to_thread), plus a concurrent prefetch (ensure_many/
    # ensure_icons_many, same pattern as _resolve_base_levels) so a cold realm
    # resolves its distinct items in parallel instead of one blocking call at
    # a time.
    def _build_rows() -> list[dict]:
        name_cache = NameCache() if names else None
        if name_cache is not None:
            item_ids = [r["item_id"] for r in rows]
            # LIVE_RESOLVE_DEADLINE_SECONDS (2026-08-01, real incident): this
            # is a live request a human is waiting on, not the background
            # prewarm loop -- see item_names.ensure_many()'s docstring for
            # why an unbounded wait here became a real problem the same day
            # blizz.api_get() gained a shared rate limiter.
            name_cache.ensure_many(item_ids, max_workers=24,
                                   deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
            name_cache.ensure_icons_many(item_ids, max_workers=24,
                                         deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
        out = [_row_to_json(r, name_cache) for r in rows]
        if name_cache is not None:
            name_cache.save()
        return out

    out_rows = await asyncio.to_thread(_build_rows)
    return {
        "rows": out_rows, "count": len(out_rows), "caveat": snipe_check.CAVEAT,
        "region": blizz.REGION, "sell_realm_slug": _realm_info(sell)["slug"],
    }


def _speed_row_to_json(r: dict, names: NameCache | None) -> dict:
    """Deliberately its own serializer, not _row_to_json(). That one is
    shaped around a snipe (buy_realm/sell_now/discount/appearance...), none
    of which exists here -- reusing it would have meant either faking those
    fields or widening a function the whole snipe path depends on. Both
    `_g` and `_copper` variants are exposed per CLAUDE.md's money rule."""
    def g(copper):
        return None if copper is None else copper / 10_000

    return {
        "realm": r["cr_id"],
        "realm_name": _realm_info(r["cr_id"])["name"] or str(r["cr_id"]),
        "realm_category": _realm_info(r["cr_id"]).get("category"),
        # Slug for the per-row Undermine Exchange link. dashboard.html builds
        # its equivalent from the *sell* realm, which this page has no concept
        # of -- here the useful destination is the realm the listing is
        # actually on, so the slug rides along per row.
        "realm_slug": _realm_info(r["cr_id"]).get("slug"),
        "item_id": r["item_id"],
        "auction_id": r["auction_id"],
        # Real item level, from the listing's upgrade-track bonus id -- NOT
        # modifier 28, which reports junk for this item family (see
        # speed_check.ILVL_BONUS_IDS). None when the listing carries no
        # upgrade id at all.
        "ilvl": r["ilvl"],
        "name": names.get(r["item_id"]) if names else None,
        "icon": names.icon(r["item_id"]) if names else None,
        "quality": names.quality(r["item_id"]) if names else None,
        "variant": _variant_label(r["bonus_key"] or "", r["item_id"], names),
        "variant_raw": r["bonus_key"] or None,
        "quantity": r["quantity"],
        "price_copper": r["unit_price"],
        "price_g": g(r["unit_price"]),
        "speed_region_median_copper": r["speed_region_median"],
        "speed_region_median_g": g(r["speed_region_median"]),
        "plain_cheapest_copper": r["plain_cheapest"],
        "plain_cheapest_g": g(r["plain_cheapest"]),
        "gap_x": r["gap_x"],
        "speed_realm_count": r["speed_realm_count"],
        "speed_listing_count": r["speed_listing_count"],
    }


@app.get("/api/speed")
async def api_speed(request: Request, items: str | None = None,
                    min_gold: float | None = None, max_gold: float | None = None,
                    min_gap: float | None = None, name_contains: str | None = None,
                    tarnished: bool = False, armor: str | None = None,
                    quality: str | None = None, ilvl: str | None = None,
                    top: int = 50, sort: str = Query("price"), names: bool = False) -> dict:
    """Experimental +Speed listing census (2026-08-12). **Shares no filter,
    threshold or pricing logic with /api/snipes** -- no discount, no sell
    realm, no AH cut, no junk/value floor, no class quotas, no appearance or
    sale-rate filter. Raw region-scan listings carrying bonus id 42, with
    context columns attached for sorting only (see
    speed_check.find_speed_listings()).

    Auth, and the open question in it: this route requires a logged-in,
    verified account, unlike /api/snipes' anonymous tier. Reason -- it is
    region-wide by construction, so unlike the snipe path there is no sell
    realm for _enforce_realm_lock() to pin, and the free tier's one-realm
    lock simply has nothing to bite on here. Gating the whole route was the
    conservative default for an experimental signal that may carry real
    value; **it is a product decision the human should confirm**, and
    opening it up is a one-line change (drop the 401/403 below and mirror
    api_snipes()'s anonymous fallthrough).

    auth.resolve_user_from_request() rather than Depends(current_verified_user)
    for the same reason api_snipes() uses it (2026-08-01 Postgres pool
    exhaustion incident): the Depends() chain holds a pooled connection
    checked out for this route's entire duration, including the slow DuckDB
    scan below. This resolves the user through its own session that closes
    in milliseconds.
    """
    user = await auth.resolve_user_from_request(request)
    if user is None:
        raise HTTPException(401, "log in to use the +Speed scan")
    # 403, not 401, so the frontend can tell "not logged in" from "logged in
    # but unverified" -- same convention as auth.current_verified_user.
    if not user.is_verified:
        raise HTTPException(403, "confirm your email address to use the +Speed scan")

    not_ready = speed_check.check_data_ready()
    if not_ready:
        raise HTTPException(400, not_ready)
    if sort not in speed_check.SORT_COLUMNS:
        raise HTTPException(400, f"sort must be one of {sorted(speed_check.SORT_COLUMNS)}")
    # Row cap reuses the existing per-tier snipe numbers rather than
    # inventing a second set -- no new product numbers were mandated for
    # this experimental feature, and picking my own would be exactly the
    # kind of un-asked-for calibration CLAUDE.md rules out.
    top = min(top, _snipe_cap(user))

    item_ids = snipe_check.parse_items(items, None)
    # `tarnished=true` resolves to the server-side constant rather than the
    # frontend posting the phrase itself -- same reasoning as /api/me sending
    # the tier caps instead of letting the page hardcode a second, driftable
    # copy. name_contains stays available for anything else.
    needle = speed_check.TARNISHED_NAME_MATCH if tarnished else name_contains
    armor_types = [a for a in (armor or "").split(",") if a.strip()] or None
    qualities = [q for q in (quality or "").split(",") if q.strip()] or None
    # Validated here rather than inside the worker so a typo'd filter is a
    # clean 400 instead of a 500 from the thread.
    for value, valid, label in ((armor_types, speed_check.ARMOR_TYPES, "armor"),
                                (qualities, speed_check.QUALITY_ALIASES, "quality")):
        for v in value or []:
            if v.casefold() not in valid:
                raise HTTPException(400, f"{label} must be one of {sorted(valid)}")
    try:
        ilvls = [int(v) for v in (ilvl or "").split(",") if v.strip()] or None
    except ValueError:
        raise HTTPException(400, "ilvl must be comma-separated integers, e.g. 253,266")

    def _run_query() -> tuple[list, int | None]:
        con = speed_check.connect()
        try:
            # Sweep timestamp read on the same connection as the rows, so the
            # freshness shown always describes the data actually returned
            # rather than a separately-fetched (possibly newer) sweep.
            swept = speed_check.latest_sweep_ts(con)
            ids = item_ids
            if needle or armor_types or qualities:
                # Blocking (NameCache -> Blizzard on a miss); safe here
                # because this whole function runs in a worker thread.
                ids = speed_check.resolve_item_filter(
                    con, name_contains=needle, qualities=qualities,
                    armor_types=armor_types, items=item_ids)
                if not ids:
                    return [], swept
            return speed_check.find_speed_listings(
                con, items=ids, min_gold=min_gold, max_gold=max_gold,
                min_gap=min_gap, ilvls=ilvls, top=top, sort=sort), swept
        finally:
            con.close()

    # to_thread for both stages, per the blocking-event-loop failure mode
    # this app has already hit twice (2026-07-25 and 2026-07-26, see
    # .claude/docs/matching.md): the DuckDB scan is a multi-second CPU/IO
    # burn over every realm's parquet, and names=true's NameCache lookups
    # fall back to blocking Blizzard calls on a cold cache. Neither may run
    # on the event loop.
    rows, swept_ts = await asyncio.to_thread(_run_query)

    def _build_rows() -> list[dict]:
        name_cache = NameCache() if names else None
        if name_cache is not None:
            ids = [r["item_id"] for r in rows]
            name_cache.ensure_many(ids, max_workers=24,
                                   deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
            name_cache.ensure_icons_many(ids, max_workers=24,
                                         deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
        out = [_speed_row_to_json(r, name_cache) for r in rows]
        if name_cache is not None:
            name_cache.save()
        return out

    out_rows = await asyncio.to_thread(_build_rows)
    return {"rows": out_rows, "count": len(out_rows), "caveat": speed_check.CAVEAT,
            "region": blizz.REGION, "tertiary": "Speed",
            "bonus_id": speed_check.SPEED_BONUS_ID,
            "name_filter": needle,
            "tarnished_match": speed_check.TARNISHED_NAME_MATCH,
            "armor_types": sorted(speed_check.ARMOR_TYPES),
            "armor_filter": armor_types, "quality_filter": qualities,
            # When the region sweep behind these rows actually ran (epoch
            # seconds). Blizzard republishes AH data hourly, so a user
            # needs to know how old this is before acting on it.
            "collected_ts": swept_ts,
            "ilvl_filter": ilvls, "tracked_ilvls": speed_check.TRACKED_ILVLS,
            "known_ilvls": sorted(set(speed_check.ILVL_BONUS_IDS.values()), reverse=True)}


def _realms_payload(cr_ids: list[int]) -> list[dict]:
    realms = []
    for cr_id in cr_ids:
        info = _realm_info(cr_id)
        realms.append({"id": cr_id, "name": info.get("name") or str(cr_id), "slug": info.get("slug")})
    realms.sort(key=lambda r: r["name"])
    return realms


@app.get("/api/realms")
def api_realms() -> dict:
    # No auth dependency (dropped 2026-08-03, alongside letting anonymous
    # visitors use /snipes) -- this route returns zero per-user data
    # (_realms_payload(_list_snapshotted_realms()) is the same for every
    # caller), so its previous Depends(current_active_user) gate was only
    # ever a "must be logged in" checkpoint, not a real data-sensitivity or
    # cost boundary. Now open to anonymous visitors too, same as /api/config.
    return {"realms": _realms_payload(_list_snapshotted_realms())}


_connected_realm_members_cache: dict[int, list[dict]] = {}


def _connected_realm_members(cr_id: int) -> list[dict]:
    """Like _realm_info(), but keeps every member realm of a connected
    realm, not just the first -- a connected realm can bundle several named
    realms sharing one AH (typically older/merged low-pop realms), and
    _realm_info() deliberately only exposes the first name (the "primary"
    display name used everywhere a buy-side realm is shown). This is a
    second, separate in-process cache rather than widening _realm_info_cache
    itself, since every other caller of _realm_info() genuinely wants just
    the one display name and shouldn't have to know about the full list."""
    if cr_id not in _connected_realm_members_cache:
        try:
            realms = blizz.connected_realm_realms(cr_id)
        except Exception:
            realms = []
        _connected_realm_members_cache[cr_id] = realms
    return _connected_realm_members_cache[cr_id]


@app.get("/api/realms/eu")
def api_realms_eu(user: User = Depends(current_subscribed_user)) -> dict:
    """Every EU connected realm (contrast /api/realms, which is scoped to
    realms this app has snapshot data for) -- backs profile.html's
    WoW-account realm-registration picker (wow_accounts.py). Gated by
    current_subscribed_user, not current_active_user: can trigger up to
    ~92 individual Blizzard calls on a cold _connected_realm_members_cache
    (blizz.list_connected_realms() + one connected_realm_realms() per id),
    so it's gated the same as the paid feature it exists to support.
    Plain `def`, not `async def` -- Starlette runs synchronous routes in a
    worker thread automatically, so this is already off the event loop
    without needing asyncio.to_thread() (contrast api_snipes(), which is
    `async def` and therefore does need it -- see "Real production
    outage" in CLAUDE.md).

    Fanned out to one entry per member realm name, not one per connected-
    realm id (2026-08-02, human request: "this works with connectedrealms,
    so if choose realm that has connected [realms], it auto adds them as
    well") -- multiple entries can share the same `id`. A user searching by
    any member realm's name (not just the connected realm's "primary" one)
    finds it and registers it under the shared connected-realm id, which is
    what actually gates matching against snipe rows throughout this app
    (see snipe_check.find_snipes()'s buy_realm) -- so adding any one member
    name already covers the whole connected realm; profile.html's
    realmNameById dedupes back down to one (the first/primary) name per id
    purely for chip display, same convention _realm_info() already uses
    for buy-side realm names elsewhere."""
    entries = []
    for cr_id in blizz.list_connected_realms():
        members = _connected_realm_members(cr_id)
        if not members:
            entries.append({"id": cr_id, "name": str(cr_id), "slug": None})
            continue
        for member in members:
            entries.append({"id": cr_id, "name": member.get("name") or str(cr_id), "slug": member.get("slug")})
    entries.sort(key=lambda r: r["name"])
    return {"realms": entries}


def _list_snapshotted_realms() -> list[int]:
    """Every realm with at least one raw snapshot file -- the precondition
    /api/realms uses. Used to be two separate checks (one requiring
    diff_snapshots.py to have run) before 2026-07-25, when pricing turned
    out to only ever need the latest snapshot -- see
    snipe_check.check_data_ready()."""
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


@app.get("/api/auth-config")
def api_auth_config() -> dict:
    """Which optional auth mechanisms this deployment actually has configured
    (2026-08-06). Public and free of per-user data by construction -- it
    reports only whether two env vars are set, never their values.

    Its own route rather than a field on /api/status because /api/status
    requires a `sell` realm id, and login.html/register.html have no realm
    context -- they'd have to invent one just to find out whether to draw a
    button.

    google: hides the "Continue with Google" button on a deployment where the
    OAuth router isn't mounted, so it can't 404. email: lets /verify explain
    that mail delivery isn't configured (local dev) instead of telling the user
    to check an inbox nothing was ever sent to."""
    return {
        "google": auth.google_oauth_client is not None,
        "email": mailer.configured(),
    }


@app.get("/api/status")
def api_status(sell: int) -> dict:
    # No auth dependency (dropped 2026-08-03) -- same reasoning as
    # /api/realms above: this returns zero per-user data (purely
    # file-mtime-derived), so the previous gate was only "must be logged
    # in," not a real data-sensitivity or cost boundary.
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
        # Renamed from events_exist 2026-07-25 -- diff_snapshots.py no
        # longer runs automatically (see collect_all.py), so "does this
        # realm have an events file" stopped being a meaningful freshness
        # signal. has_data just means "has at least one snapshot ever been
        # retrieved," matching what dashboard.html's ticker actually cares
        # about (has real data ever landed for this realm).
        "has_data": last_modified is not None,
    }


@app.get("/api/config")
def api_config() -> dict:
    return {"default_sell": getattr(app.state, "default_sell", None)}


@app.get("/")
def index() -> FileResponse:
    """Public marketing landing page (changed 2026-07-26 -- the sniper tool
    itself moved to /snipes, see that route below). Deliberately no auth
    check, same reasoning as /pricing/log -- this is what a logged-out
    visitor sees first."""
    return FileResponse(ROOT / "static" / "landing.html")


@app.get("/snipes")
def snipes_page() -> FileResponse:
    """The actual sniper dashboard -- moved here from `/` (2026-07-26) so
    `/` could become a real marketing landing page instead of bouncing
    straight into the tool. Auth itself is still enforced client-side by
    dashboard.html's own init() (checks /api/me, redirects to /login), same
    as before the move -- this route itself stays public so the static
    file can be served at all."""
    return FileResponse(ROOT / "static" / "dashboard.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "login.html")


@app.get("/register")
def register_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "register.html")


@app.get("/verify")
def verify_page() -> FileResponse:
    """Landing page for the link in a verification email (2026-08-06). The
    token arrives as `?token=...` but is consumed by a POST to /auth/verify
    that verify.html makes itself -- FastAPI-Users' verify router takes the
    token in a JSON body, and a GET that mutated state would be triggered by
    every mail-client link prefetcher and corporate link scanner that touches
    the message."""
    return FileResponse(ROOT / "static" / "verify.html")


@app.get("/forgot-password")
def forgot_password_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "forgot-password.html")


@app.get("/reset-password")
def reset_password_page() -> FileResponse:
    """Same token-in-the-query, POST-to-consume shape as /verify above."""
    return FileResponse(ROOT / "static" / "reset-password.html")


@app.get("/subscribe")
def subscribe_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "subscribe.html")


@app.get("/pricing")
def pricing_page() -> FileResponse:
    """Deliberately public, no auth check (like /log) -- a pricing page a
    visitor can't see before signing up defeats its own purpose."""
    return FileResponse(ROOT / "static" / "pricing.html")


@app.get("/profile")
def profile_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "profile.html")


@app.get("/snipe-board")
def snipe_board_page() -> FileResponse:
    """Public page, no auth check -- snipeboard.html itself decides what to
    show (post button vs. "log in to post" link) based on /api/me, same
    client-side-gate convention as /snipes. Named "Snipe Board" (renamed
    2026-07-29 from an initial "Forum") -- backing module/routes stay
    forum.py/`/api/forum/*` (module name doesn't need to track the
    user-facing label, same precedent as dashboard.py serving `/snipes`)."""
    return FileResponse(ROOT / "static" / "snipeboard.html")


@app.get("/speed")
def speed_page() -> FileResponse:
    """Experimental +Speed listing census (2026-08-12). Public route serving
    the static file only -- speed.html gates itself client-side off /api/me
    like every other page here, and the data behind it is gated for real by
    /api/speed's own auth check (see api_speed()'s docstring, including the
    open product question about that gate)."""
    return FileResponse(ROOT / "static" / "speed.html")


@app.get("/watchlist")
def watchlist_page() -> FileResponse:
    """Public "coming soon" placeholder (added 2026-07-31) for the TSM-
    group-import cross-realm item-tracking idea sketched in
    .claude/docs/feature-watchlist.md -- design idea only, no tracking/alerting logic
    exists yet. Public/no-auth-check, same reasoning as /log/ /snipe-board:
    nothing here is sensitive, and static/watchlist.html itself swaps its
    nav based on /api/me the same way those pages do."""
    return FileResponse(ROOT / "static" / "watchlist.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    """Serves the shell only -- no auth check on the HTML itself, same
    client-side-gate convention every other page here follows. That's safe
    because the page ships no data: every number on it comes from
    /api/admin/*, and those are gated server-side by
    admin.current_superuser. A non-superuser who guesses this URL gets an
    empty page telling them so."""
    return FileResponse(ROOT / "static" / "admin.html")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt() -> FileResponse:
    """Crawlers only ever read this from the origin root, so it can't just live
    under the /static mount below."""
    return FileResponse(ROOT / "static" / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml() -> FileResponse:
    """Hand-maintained -- there are four indexable URLs and they change about
    never. Generate it only if that stops being true."""
    return FileResponse(ROOT / "static" / "sitemap.xml", media_type="application/xml")


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
