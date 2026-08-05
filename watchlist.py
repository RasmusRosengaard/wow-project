"""Watchlist -- track specific items across every EU realm, independent of
any sell realm (see .claude/docs/feature-watchlist.md). A user adds items either one at a
time by item id or in bulk via a pasted TSM group export (tsm_import.py),
sets a plain gold trigger price per item, and gets a Discord notification
when the item's current region-wide cheapest listing clears that price.

Mirrors wow_accounts.py's shape: own APIRouter, Depends(current_subscribed_user)
(premium-only, matching the "Coming soon (premium-only)" badge the
watchlist.html placeholder already shipped with), manual if/raise
HTTPException validation, an atomic INSERT...SELECT...WHERE cap (same TOCTOU
reasoning as that module's own docstring -- this app has two recorded races
from a plain SELECT-COUNT-then-INSERT).

Key product decisions, all human-made during a 2026-08-02 conversation (see
CLAUDE.md's watchlist.py row for the fuller trace):
- Matching is item_id-only (+ optional pet_species_id for caged pets) --
  no bonus/ilvl awareness, matching the rest of the product's 2026-07-26
  decision rather than reopening it here.
- trigger_price_copper is a plain absolute gold price the user sets --
  explicitly NOT a discount-vs-region-median "auto-price" the rest of the
  product uses elsewhere ("we only want to trigger for whatever price the
  user wants").
- Delivery is a per-user Discord webhook URL (User.discord_webhook_url) --
  the cheapest available real delivery mechanism (a plain HTTPS POST, no
  OAuth), versus in-app-only (cheaper but passive) or email (real new infra
  needed). No delivery happens for an account with no webhook set -- items
  are still tracked, just silently.

check_triggers() is the one function collect_all.py's background loop
calls, after each region sweep -- it does not run on its own cadence, it
rides the existing ~10-minute cycle (.claude/docs/feature-watchlist.md's open question
#6, resolved: no new scan cadence).
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

import db
import snipe_check
import tsm
from appearance import AppearanceCache
from auth import current_subscribed_user, has_active_subscription
from db import User, WatchlistItem, WowAccount, WowAccountRealm, get_async_session
from item_names import LIVE_RESOLVE_DEADLINE_SECONDS, NameCache
from tsm_import import TsmImportError, decode_group_export

log = logging.getLogger("watchlist")

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Pure UX limit on self-declared metadata, same non-security-critical
# reasoning as wow_accounts.MAX_ACCOUNTS_PER_USER -- picked generously
# enough that a single real TSM group import (the sample used to build this
# feature was 300 items) comfortably fits with room for individually-added
# items too, not a human-specified number the way most of this project's
# tuned constants are (worth revisiting if a real user hits it).
MAX_WATCHLIST_ITEMS_PER_USER = 500
LABEL_MAX_LEN = 200
WEBHOOK_URL_MAX_LEN = 500

# How long a listing has to stay under a trigger before the *same* item
# fires another Discord message -- without this, a listing that just sits
# there cheap would re-notify every single ~10-minute collect_all.py cycle
# forever. Not human-specified -- a reasonable default (a few times a day at
# most for a persistently-cheap item), worth tuning with real usage.
NOTIFY_COOLDOWN_SECONDS = 4 * 60 * 60


# ---------------------------------------------------------------------------
# Standing rule scan (experimental, added 2026-08-05, human request)
# ---------------------------------------------------------------------------
# A second, entirely different trigger shape from the per-item
# trigger_price_copper above: instead of "tell me when *this* item drops
# below *this* price", it is a standing rule over **every item in the region
# sweep** -- "anything cheap that TSM says is actually worth a lot".
#
# Both thresholds are human-specified (2026-08-05), per this project's
# convention for calibrating anything on the pricing path:
# The buy ceiling is **proportional to the item's TSM sale average**, not a
# single flat number (human's call, 2026-08-05, replacing the original flat
# 100g/3000g pair): "if item is over 5k sellavg, it can cost up to 500, and
# so on so basicly 10%, but under3k sellavg its 100 hardcoded."
#
#   TSM sale avg < 2,000g  ->  ignored entirely, however cheap it is
#   TSM sale avg >= 2,000g  ->  buy must be under 10% of the sale average
#
# So a 2,000g item qualifies up to 200g, a 5,000g item up to 500g, a
# 50,000g item up to 5,000g. The ratio is the thing that actually matters:
# "cheap" means cheap *relative to what the item sells for*, and the
# original flat 100g ceiling got that wrong at both ends -- it admitted 40g
# junk while hiding a 100g listing of an 11,000g item (real case, item
# 29726 "Pattern: Hood of Primal Life", which the human found by hand and
# which missed the old ceiling by a single copper: 100g is not < 100g).
#
# The minimum sale average is what keeps this from flooding, and it is not
# theoretical: an intermediate version of this rule used a flat 100g cap
# below the boundary *instead of* rejecting those items, and measuring it
# against the live sweep produced **6,580 hits** rather than the previous
# 7 -- almost all of them sub-1-gold stacked trade goods worth a few
# hundred gold ("buy 0g, avg 2,851g, 285,086x"). A huge multiple on a tiny
# absolute value is not a snipe. Same failure mode snipe_check.py's own
# MIN_VALUE_FLOOR_G exists to prevent, arrived at independently here.
RULE_MIN_SALE_AVG_COPPER = 2_000 * 10_000      # below this, ignored entirely
RULE_BUY_FRACTION_OF_SALE_AVG = 0.10           # buy ceiling, as a fraction of it


# Bounds on what a user may tune the two numbers to (2026-08-05). Not
# taste-policing -- these stop a setting that would make the feature behave
# pathologically: a fraction at or above 1.0 means "any price at all counts
# as a snipe", and a minimum of 0 reopens the sub-1g trade-goods flood that
# 6,580-hit measurement found. The upper fraction bound is deliberately
# generous; the point is to exclude nonsense, not to second-guess.
RULE_MIN_SALE_AVG_FLOOR_COPPER = 100 * 10_000      # can't tune below 100g

# The buy ceiling a user may pick, as whole percentages of the TSM sale
# average (human's call 2026-08-05: "maybe users to be able to choose
# 10-20-30-40-50-60%"). A fixed set rather than a free number, deliberately:
# it is the difference between "how aggressive do you want this" -- a
# question with a handful of sensible answers -- and a text box inviting
# 0.001% or 95%, neither of which produces a usable feed. The list doubles
# as the validation rule, so the API cannot drift from the dropdown.
RULE_BUY_PERCENT_CHOICES = (10, 20, 30, 40, 50, 60)


def _rule_max_buy_copper(sale_avg_copper: float, fraction: float | None = None) -> float:
    """Highest buy price that still counts as a find for an item with this
    TSM sale average. Callers must reject anything below the minimum sale
    average first -- this function deliberately does not, since "what is
    the ceiling" and "does this item qualify at all" are two different
    questions, and folding them together would make a too-cheap item look
    like it had a valid, very small ceiling."""
    if fraction is None:
        fraction = RULE_BUY_FRACTION_OF_SALE_AVG
    return sale_avg_copper * fraction


def _rule_thresholds_for(user: User) -> tuple[int, float]:
    """This user's (min sale average, buy fraction), falling back to the
    module defaults. A NULL column means "follow the current default", not
    "was never set to anything" -- so retuning the defaults moves every
    untouched account with it, which is the whole reason the migration
    deliberately did not backfill."""
    min_avg = user.sniper_min_sale_avg_copper
    frac = user.sniper_buy_fraction
    return (int(min_avg) if min_avg is not None else RULE_MIN_SALE_AVG_COPPER,
            float(frac) if frac is not None else RULE_BUY_FRACTION_OF_SALE_AVG)

# Deliberately **sell-realm-free** (human's call, 2026-08-05, choosing
# between this and reusing snipe_check.find_snipes() with the user's
# locked_sell_realm). Watchlist has no sell realm by design -- see
# _region_cheapest_by_item() -- so the two heuristics that survive here are
# exactly the two computable from region data alone:
#
#   - sus_item_suspect    -> snipe_check.is_sus_item(), a pure function
#   - sniper_filter's cluster comparison -> needs only other realms' floors
#
# price_suspect is **not** computable and is therefore not applied: it is
# `sell_p_g >= PRICE_SUSPECT_MULTIPLE * region_median_g`, and with no sell
# realm there is no sell_p_g at all. Stated plainly rather than silently
# approximated, because the human's requirement was "flagged items must
# never be sent" -- one of the three dashboard flags simply cannot be
# evaluated on this path, and pretending otherwise with a substituted
# number would be worse than saying so.
#
# The cluster thresholds are *imported*, never re-declared, so this rule and
# the dashboard's "Hide flagged (sniper filter)" checkbox can never drift
# apart -- the human explicitly worried about exactly that ("kræver måske
# den laves i backend også?").
#
# snipe_check.SNIPER_FILTER_HIGH_VALUE_EXEMPT_G is deliberately NOT honored
# here. That exemption only ever *suppresses* the flag (i.e. sends more),
# and on this path the hard requirement runs the other way -- never send a
# flagged item -- so the conservative reading wins. Worth revisiting if it
# turns out to be suppressing genuinely good high-value hits.
RULE_CLUSTER_N = snipe_check.SNIPER_FILTER_N
RULE_CLUSTER_CLOSE_MULTIPLE = snipe_check.SNIPER_FILTER_CLOSE_MULTIPLE
RULE_CLUSTER_MIN_REALMS = snipe_check.SNIPER_FILTER_MIN_REALMS

# Safety valve. The very first cycle after this ships evaluates the rule
# against the whole region sweep with an empty cooldown file, so without a
# cap a single cycle could fire hundreds of Discord messages at once (and
# Discord rate-limits a webhook hard enough that the overflow would be
# dropped anyway, just messily). Hits above the cap are simply not sent
# *and not marked notified*, so they are reconsidered next cycle rather
# than silently lost.
RULE_MAX_NOTIFICATIONS_PER_CYCLE = 10

# Per-(item, pet) cooldown state. The per-item path stores this on the
# WatchlistItem row, but a standing rule owns no rows -- and an in-memory
# dict would reset on every deploy/restart, re-notifying the same items.
# Lives on the Railway volume next to the collector's own cursors.
#
# A function, not a module-level constant: DATA is monkeypatched by the
# tests' `listings_dir` fixture, and a path computed at import time would
# ignore that and write into the developer's real data/ directory. Same
# reason conftest.py hard-fails any test that reaches a real database --
# a test must not be able to touch production-shaped state.
def _rule_state_path() -> Path:
    return DATA / "state" / "watchlist_rule.json"

# Every subscriber gets the rule (human's call, 2026-08-05, widening the
# assistant's more cautious superuser-only default) -- same premium-only
# audience as the rest of Watchlist, whose routes all sit behind
# Depends(current_subscribed_user) -- and each can turn it off individually
# via User.default_sniper_list_enabled ("Default sniper list" on
# watchlist.html), which defaults to on so the already-live behavior isn't
# silently switched off for anyone.
#
# Deliberately gated through auth.has_active_subscription() in Python
# rather than an equivalent SQL WHERE clause: that helper is the single
# definition of "subscribed" in this app (`is_superuser or
# subscription_status == "active"` -- superusers pass without a real Stripe
# subscription, founder/admin access), and re-expressing it as SQL here
# would be a second copy free to drift from it. The recipient list is
# bounded by "has a webhook set at all", so filtering in Python costs
# nothing.


def _item_json(item: WatchlistItem, name_cache: NameCache | None = None) -> dict:
    out = {
        "id": item.id,
        "item_id": item.item_id,
        "pet_species_id": item.pet_species_id,
        "trigger_price_g": (item.trigger_price_copper / 10000) if item.trigger_price_copper is not None else None,
        "trigger_percent": item.trigger_percent,
        "label": item.label,
    }
    if name_cache is not None:
        out["name"] = name_cache.get(item.item_id, item.pet_species_id)
        out["icon"] = name_cache.icon(item.item_id, item.pet_species_id)
        out["quality_color"] = name_cache.quality_color(item.item_id, item.pet_species_id)
    return out


async def _get_owned_item(item_id: int, user: User, session: AsyncSession) -> WatchlistItem:
    item = (await session.execute(
        select(WatchlistItem).where(WatchlistItem.id == item_id, WatchlistItem.owner_id == user.id)
    )).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "watchlist item not found")
    return item


async def _insert_item_atomic(owner_id: uuid.UUID, item_id: int, pet_species_id: int | None,
                              trigger_price_copper: int | None, label: str | None,
                              session: AsyncSession, trigger_percent: int | None = None) -> bool:
    """Same atomic INSERT...SELECT...WHERE cap pattern as
    wow_accounts._insert_account_atomic -- see that function's docstring for
    why this isn't a Python-side SELECT COUNT(*) then INSERT."""
    count_subq = (
        select(func.count()).select_from(WatchlistItem)
        .where(WatchlistItem.owner_id == owner_id)
        .scalar_subquery()
    )
    insert_stmt = insert(WatchlistItem).from_select(
        [WatchlistItem.owner_id, WatchlistItem.item_id, WatchlistItem.pet_species_id,
         WatchlistItem.trigger_price_copper, WatchlistItem.label, WatchlistItem.created_at,
         WatchlistItem.trigger_percent],
        select(
            literal(owner_id, type_=WatchlistItem.owner_id.type),
            literal(item_id, type_=WatchlistItem.item_id.type),
            literal(pet_species_id, type_=WatchlistItem.pet_species_id.type),
            literal(trigger_price_copper, type_=WatchlistItem.trigger_price_copper.type),
            literal(label, type_=WatchlistItem.label.type),
            literal(datetime.now(timezone.utc), type_=WatchlistItem.created_at.type),
            literal(trigger_percent, type_=WatchlistItem.trigger_percent.type),
        ).where(count_subq < MAX_WATCHLIST_ITEMS_PER_USER),
    )
    result = await session.execute(insert_stmt)
    return result.rowcount > 0


@router.get("")
async def list_watchlist(user: User = Depends(current_subscribed_user),
                         session: AsyncSession = Depends(get_async_session)) -> dict:
    items = (await session.execute(
        select(WatchlistItem).where(WatchlistItem.owner_id == user.id).order_by(WatchlistItem.id)
    )).scalars().all()

    def _build():
        name_cache = NameCache()
        ids = list({i.item_id for i in items})
        if ids:
            name_cache.ensure_many(ids, max_workers=16, deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
            name_cache.ensure_icons_many(ids, max_workers=16, deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
        return [_item_json(i, name_cache) for i in items]

    # NameCache lookups can make blocking Blizzard calls on a cold cache --
    # same asyncio.to_thread() precaution as dashboard.py's _build_rows(),
    # see CLAUDE.md's "Real production outage" note for why this matters
    # even for a small, low-frequency route like this one.
    item_rows = await asyncio.to_thread(_build)
    return {"items": item_rows, "discord_webhook_url": user.discord_webhook_url,
           "default_sniper_list_enabled": user.default_sniper_list_enabled,
           # The *resolved* percentage, not the raw column: NULL means
           # "follow the current default", so a user who never picked one
           # sees the live default rather than an empty control.
           "sniper_buy_percent": round(_rule_thresholds_for(user)[1] * 100),
           "sniper_buy_percent_choices": list(RULE_BUY_PERCENT_CHOICES),
           "sniper_buy_percent_default": round(RULE_BUY_FRACTION_OF_SALE_AVG * 100),
           "max_items": MAX_WATCHLIST_ITEMS_PER_USER}


class DiscordWebhookUpdate(BaseModel):
    discord_webhook_url: str | None = None


# Registered before the "/{item_id}" routes below (route order matters in
# FastAPI/Starlette -- a later "/{item_id}: int" pattern would otherwise
# swallow "/discord-webhook" as a failed int-conversion 422 instead of
# reaching this handler, confirmed live while writing this module's tests).
@router.patch("/discord-webhook")
async def update_discord_webhook(payload: DiscordWebhookUpdate,
                                 user: User = Depends(current_subscribed_user),
                                 session: AsyncSession = Depends(get_async_session)) -> dict:
    url = (payload.discord_webhook_url or "").strip() or None
    if url and len(url) > WEBHOOK_URL_MAX_LEN:
        raise HTTPException(400, f"webhook URL must be {WEBHOOK_URL_MAX_LEN} characters or fewer")
    if url and not url.startswith("https://discord.com/api/webhooks/"):
        raise HTTPException(400, "not a Discord webhook URL "
                            "(should start with https://discord.com/api/webhooks/)")
    user.discord_webhook_url = url
    await session.commit()
    return {"discord_webhook_url": user.discord_webhook_url}


class DefaultSniperListUpdate(BaseModel):
    enabled: bool
    # None means "leave the threshold alone", so the toggle and the
    # percentage can be saved independently.
    buy_percent: int | None = None


# Same registration-order constraint as /discord-webhook above: this must
# come before the "/{item_id}" routes or Starlette tries to parse
# "default-sniper-list" as an int and 422s.
@router.patch("/default-sniper-list")
async def update_default_sniper_list(payload: DefaultSniperListUpdate,
                                     user: User = Depends(current_subscribed_user),
                                     session: AsyncSession = Depends(get_async_session)) -> dict:
    """Toggle the standing rule scan for this account. Deliberately separate
    from the webhook route: the webhook is *where* notifications go, this is
    *whether* the standing rule is one of the things that sends them, and a
    user should be able to keep their per-item triggers while turning only
    the standing rule off."""
    user.default_sniper_list_enabled = bool(payload.enabled)
    if payload.buy_percent is not None:
        if payload.buy_percent not in RULE_BUY_PERCENT_CHOICES:
            raise HTTPException(400, "buy percentage must be one of "
                                + ", ".join(f"{p}%" for p in RULE_BUY_PERCENT_CHOICES))
        user.sniper_buy_fraction = payload.buy_percent / 100
    await session.commit()
    return {"default_sniper_list_enabled": user.default_sniper_list_enabled,
            "sniper_buy_percent": round(_rule_thresholds_for(user)[1] * 100)}


class WatchlistBatchUpdateItem(BaseModel):
    id: int
    trigger_price_g: float | None = None


class WatchlistBatchUpdateRequest(BaseModel):
    items: list[WatchlistBatchUpdateItem]


# Also registered before "/{item_id}" for the same route-order reason as
# /discord-webhook above.
@router.patch("/batch")
async def batch_update_items(payload: WatchlistBatchUpdateRequest,
                             user: User = Depends(current_subscribed_user),
                             session: AsyncSession = Depends(get_async_session)) -> dict:
    """Updates trigger prices for many items in a single request (added
    2026-08-02, human request: editing trigger prices on a large TSM import
    -- 300 items in the real sample used to build this feature -- one row
    at a time meant one PATCH per row, up to hundreds of round trips just
    to fill in prices after a bulk import). The frontend now collects
    edits locally and calls this once instead of static/watchlist.html's
    old per-row PATCH /{item_id}, which still exists for other callers but
    is no longer used by the batch-edit flow.

    Unknown or not-owned ids are silently skipped (not 404'd) -- same
    non-existence-leaking precedent as _get_owned_item(), but batched: one
    query fetches every id in this user's list, one loop applies the
    matched edits, one commit. A negative trigger price is skipped the
    same way rather than failing the whole batch over one bad row."""
    if not payload.items:
        return {"updated": 0, "skipped": 0}
    ids = [entry.id for entry in payload.items]
    owned = {item.id: item for item in (await session.execute(
        select(WatchlistItem).where(WatchlistItem.id.in_(ids), WatchlistItem.owner_id == user.id)
    )).scalars().all()}
    updated = 0
    skipped = 0
    for entry in payload.items:
        item = owned.get(entry.id)
        if item is None or (entry.trigger_price_g is not None and entry.trigger_price_g < 0):
            skipped += 1
            continue
        item.trigger_price_copper = (
            round(entry.trigger_price_g * 10000) if entry.trigger_price_g is not None else None
        )
        updated += 1
    await session.commit()
    return {"updated": updated, "skipped": skipped}


class WatchlistItemCreate(BaseModel):
    # Alternative to trigger_price_g, never both -- see db.WatchlistItem.
    trigger_percent: int | None = None
    item_id: int
    pet_species_id: int | None = None
    trigger_price_g: float | None = None
    label: str | None = None


@router.post("")
async def add_item(payload: WatchlistItemCreate, user: User = Depends(current_subscribed_user),
                   session: AsyncSession = Depends(get_async_session)) -> dict:
    if payload.item_id <= 0:
        raise HTTPException(400, "invalid item id")
    # Required for a manual add (2026-08-02, human request) -- a
    # newly-added item should always have something to actually trigger on.
    # TSM import is deliberately exempt: import_tsm_group() below calls
    # _insert_item_atomic() directly rather than through this model, since
    # a TSM export carries no per-item price data to require in the first
    # place -- see that function's own docstring.
    if payload.trigger_percent is not None:
        if payload.trigger_percent not in RULE_BUY_PERCENT_CHOICES:
            raise HTTPException(400, "trigger percentage must be one of "
                                + ", ".join(f"{p}%" for p in RULE_BUY_PERCENT_CHOICES))
    elif payload.trigger_price_g is None or payload.trigger_price_g <= 0:
        raise HTTPException(400, "a trigger price or percentage is required")
    label = (payload.label or "").strip() or None
    if label and len(label) > LABEL_MAX_LEN:
        raise HTTPException(400, f"label must be {LABEL_MAX_LEN} characters or fewer")
    # Mutually exclusive: a percentage add stores no gold price at all.
    trigger_copper = (None if payload.trigger_percent is not None
                      else (round(payload.trigger_price_g * 10000)
                            if payload.trigger_price_g is not None else None))

    inserted = await _insert_item_atomic(user.id, payload.item_id, payload.pet_species_id,
                                         trigger_copper, label, session,
                                         trigger_percent=payload.trigger_percent)
    await session.commit()
    if not inserted:
        raise HTTPException(400, f"maximum {MAX_WATCHLIST_ITEMS_PER_USER} watchlist items")

    item = (await session.execute(
        select(WatchlistItem).where(WatchlistItem.owner_id == user.id, WatchlistItem.item_id == payload.item_id)
        .order_by(WatchlistItem.id.desc())
    )).scalars().first()
    return _item_json(item)


class WatchlistItemUpdate(BaseModel):
    trigger_price_g: float | None = None
    trigger_percent: int | None = None


@router.patch("/{item_id}")
async def update_item(item_id: int, payload: WatchlistItemUpdate,
                      user: User = Depends(current_subscribed_user),
                      session: AsyncSession = Depends(get_async_session)) -> dict:
    item = await _get_owned_item(item_id, user, session)
    if payload.trigger_percent is not None:
        if payload.trigger_percent not in RULE_BUY_PERCENT_CHOICES:
            raise HTTPException(400, "trigger percentage must be one of "
                                + ", ".join(f"{p}%" for p in RULE_BUY_PERCENT_CHOICES))
        # Switching mode clears the other side rather than leaving a stale
        # value that would reappear if the user switched back -- the two are
        # mutually exclusive, see db.WatchlistItem.
        item.trigger_percent = payload.trigger_percent
        item.trigger_price_copper = None
    else:
        if payload.trigger_price_g is not None and payload.trigger_price_g < 0:
            raise HTTPException(400, "trigger price can't be negative")
        item.trigger_price_copper = (
            round(payload.trigger_price_g * 10000) if payload.trigger_price_g is not None else None
        )
        if payload.trigger_price_g is not None:
            item.trigger_percent = None
    await session.commit()
    return _item_json(item)


@router.delete("/{item_id}")
async def delete_item(item_id: int, user: User = Depends(current_subscribed_user),
                      session: AsyncSession = Depends(get_async_session)) -> dict:
    item = await _get_owned_item(item_id, user, session)
    await session.delete(item)
    await session.commit()
    return {"deleted": item_id}


class TsmImportRequest(BaseModel):
    export: str


@router.post("/import-tsm")
async def import_tsm_group(payload: TsmImportRequest, user: User = Depends(current_subscribed_user),
                           session: AsyncSession = Depends(get_async_session)) -> dict:
    # tsm_import.decode_group_export() runs real Lua bytecode via lupa --
    # CPU-bound, not a network call, but not guaranteed instant for an
    # arbitrarily large pasted group either; to_thread() out of caution,
    # same "any new blocking call site needs this checked" discipline as
    # every other route in this app (see CLAUDE.md's "Real production
    # outage" note).
    try:
        export = await asyncio.to_thread(decode_group_export, payload.export)
    except TsmImportError as exc:
        raise HTTPException(400, str(exc))

    existing_ids = set((await session.execute(
        select(WatchlistItem.item_id).where(WatchlistItem.owner_id == user.id)
    )).scalars().all())

    seen_in_batch: set[int] = set()
    imported = skipped_existing = skipped_cap = 0
    for tsm_item in export.items:
        if tsm_item.item_id in existing_ids or tsm_item.item_id in seen_in_batch:
            skipped_existing += 1
            continue
        seen_in_batch.add(tsm_item.item_id)
        inserted = await _insert_item_atomic(
            user.id, tsm_item.item_id, None, None, tsm_item.group_path[:LABEL_MAX_LEN], session,
        )
        if inserted:
            imported += 1
        else:
            skipped_cap += 1
    await session.commit()

    return {
        "group_name": export.group_name,
        "total_in_export": len(export.items),
        "imported": imported,
        "skipped_existing": skipped_existing,
        "skipped_cap": skipped_cap,
    }


def _region_cheapest_by_item(item_ids: list[int]) -> dict[tuple[int, int | None], tuple[int, int]]:
    """{(item_id, pet_species_id): (cheapest_copper, cr_id)} across every EU
    realm's current listings -- Watchlist has no sell realm to exclude, so
    this is a plain min() over the whole region-wide sweep, unlike
    snipe_check.py's sell-realm-relative comparison. Returns {} if no sweep
    has run yet."""
    listings_dir = DATA / "listings"
    if not item_ids or not any(listings_dir.glob("*.parquet")):
        return {}
    con = duckdb.connect()
    try:
        ids_sql = ",".join(str(int(i)) for i in item_ids)
        rows = con.execute(f"""
            SELECT item_id, pet_species_id, cr_id, buyout, quantity
            FROM read_parquet('{(listings_dir / "*.parquet").as_posix()}')
            WHERE item_id IN ({ids_sql}) AND buyout IS NOT NULL
        """).fetchall()
    finally:
        con.close()
    best: dict[tuple[int, int | None], tuple[int, int]] = {}
    for item_id, pet_species_id, cr_id, buyout, quantity in rows:
        unit_price = round(buyout / max(quantity, 1))
        key = (item_id, pet_species_id)
        if key not in best or unit_price < best[key][0]:
            best[key] = (unit_price, cr_id)
    return best


def _rule_candidates() -> list[dict]:
    """Every (item_id, pet_species_id) whose region-wide cheapest listing is
    region-wide cheapest listing, with the cluster numbers the sniper-filter
    comparison needs, in one DuckDB pass over the region sweep.

    Returns **every** item, not just cheap ones: the buy ceiling became
    per-item when it started depending on the item's TSM sale average (see
    _rule_max_buy_copper()), so it can no longer be expressed as a constant
    in this query. _rule_scan() applies it right after the TSM lookup
    instead. This costs nothing measurable -- the per-realm GROUP BY over
    every listing already dominates, and the old ceiling only ever trimmed
    the final result.

    The cluster mirrors snipe_check.py's `sniper_filter_cluster` exactly:
    per-realm floors first (so one realm spamming N copies of the same
    auction can't inflate the cluster), then the RULE_CLUSTER_N cheapest
    *other* realms by price, then their median. Because the buy candidate
    here is by construction the region-wide cheapest (rank 1), "the N
    cheapest other realms" is simply ranks 2..N+1 -- no separate anti-join
    against the buy realm is needed the way find_snipes() needs one, since
    there a candidate can be any realm's listing, not just the cheapest.

    Returns [] if no sweep has run yet."""
    listings_dir = DATA / "listings"
    if not any(listings_dir.glob("*.parquet")):
        return []
    con = duckdb.connect()
    try:
        rows = con.execute(f"""
            WITH realm_floor AS (
                -- Per-realm floor, unit price (buyout is for the whole
                -- stack -- the copper-vs-unit-price distinction this
                -- project has been bitten by before, see CLAUDE.md).
                SELECT item_id, pet_species_id, cr_id,
                       min(CAST(buyout AS DOUBLE) / greatest(quantity, 1)) AS realm_cheapest
                FROM read_parquet('{(listings_dir / "*.parquet").as_posix()}')
                WHERE buyout IS NOT NULL
                GROUP BY item_id, pet_species_id, cr_id
            ),
            ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                           PARTITION BY item_id, pet_species_id
                           ORDER BY realm_cheapest ASC
                       ) AS rk
                FROM realm_floor
            ),
            cluster AS (
                SELECT item_id, pet_species_id,
                       median(realm_cheapest) AS cluster_median,
                       count(*) AS cluster_realms
                FROM ranked
                WHERE rk BETWEEN 2 AND {int(RULE_CLUSTER_N) + 1}
                GROUP BY item_id, pet_species_id
            )
            SELECT r.item_id, r.pet_species_id, r.cr_id, r.realm_cheapest,
                   c.cluster_median, c.cluster_realms
            FROM ranked r
            LEFT JOIN cluster c
                   ON r.item_id = c.item_id
                  -- IS NOT DISTINCT FROM, not `=`/USING: pet_species_id is
                  -- NULL for every non-pet item, and a plain equality join
                  -- drops every one of those rows.
                  AND r.pet_species_id IS NOT DISTINCT FROM c.pet_species_id
            WHERE r.rk = 1
        """).fetchall()
    finally:
        con.close()
    return [{"item_id": int(item_id), "pet_species_id": pet_species_id,
             "cr_id": int(cr_id), "buy_copper": int(round(buy)),
             "cluster_median": cluster_median, "cluster_realms": int(cluster_realms or 0)}
            for item_id, pet_species_id, cr_id, buy, cluster_median, cluster_realms in rows]


def _rule_cluster_suspect(cand: dict) -> bool:
    """snipe_check.py's sniper_filter_suspect, region-only. Same
    "unknown isn't a claim" convention as the original: fewer than
    RULE_CLUSTER_MIN_REALMS other realms listing the item at all means
    there is not enough data to judge clustering, which is treated as
    *not* flagged rather than as an assumed pass."""
    if cand["cluster_realms"] < RULE_CLUSTER_MIN_REALMS or cand["cluster_median"] is None:
        return False
    return cand["cluster_median"] <= cand["buy_copper"] * RULE_CLUSTER_CLOSE_MULTIPLE


def _rule_scan(min_sale_avg_copper: int | None = None, buy_fraction: float | None = None,
               candidates: list[dict] | None = None) -> list[dict]:
    """The standing rule, applied end to end, for one pair of thresholds.

    Both are per-user now (see _rule_thresholds_for()), so `candidates` can
    be passed in pre-computed: the DuckDB pass depends on nothing
    user-specific and is by far the most expensive step, so the caller runs
    it once and reuses it across every distinct threshold pair rather than
    re-querying per user.

    Filter order is deliberate and load-bearing: the two cheap, purely-local tests (SQL, then the TSM cache)
    run first and cut the candidate set to a handful, so the later
    NameCache lookups -- which can make a *live Blizzard call* for an item
    nobody has resolved before -- only ever see a small set. Resolving
    thousands of unknown items in a background loop is precisely the shape
    that caused the 2026-08-01 rate-limit incident (see blizz.py's
    _TokenBucket comment); this ordering is what keeps that from recurring
    rather than any cap here."""
    if min_sale_avg_copper is None:
        min_sale_avg_copper = RULE_MIN_SALE_AVG_COPPER
    if candidates is None:
        candidates = _rule_candidates()
    if not candidates:
        return []

    sale_rates = tsm.SaleRateCache()
    hits = []
    for cand in candidates:
        entry = sale_rates.get(cand["item_id"])
        # No TSM average at all -> skip (human's call, 2026-08-05: "hvis sale
        # avg ikke findes på item'en så ignore for nu"). Same
        # "unknown isn't a claim" convention the rest of the pipeline uses,
        # just resolved in the conservative direction for an outbound
        # notification: silence, not a guess.
        avg = entry.get("avg_sale_price") if entry else None
        # No TSM average at all -> ignored (human's call: "hvis sale avg
        # ikke findes på item'en så ignore for nu"), and below the minimum
        # -> ignored however cheap it is, which is what stops the rule
        # drowning in sub-1g trade goods.
        if avg is None or avg < min_sale_avg_copper:
            continue
        if cand["buy_copper"] >= _rule_max_buy_copper(avg, buy_fraction):
            continue
        if _rule_cluster_suspect(cand):
            continue
        # dict(cand), not cand: the candidate list is shared across every
        # threshold pair, so annotating the original would leak one user's
        # computed fields into another user's pass.
        hits.append(dict(cand, region_sale_avg_copper=avg))

    if not hits:
        return []

    names = NameCache()
    appearances = AppearanceCache()
    kept = []
    for cand in hits:
        item_id = cand["item_id"]
        inventory_type = names.inventory_type(item_id)
        # Grey/white lives here rather than in is_sus_item() (2026-08-05):
        # the human wants POOR/COMMON gone from the sniper list but still
        # visible on the dashboard, and is_sus_item() is shared by both.
        # The set is still imported from snipe_check so there is only ever
        # one definition of "low quality".
        quality = names.quality(item_id)
        if quality is not None and quality.upper() in snipe_check.LOW_QUALITY_TYPES:
            continue
        if snipe_check.is_sus_item(item_id, inventory_type, names.base_level(item_id),
                                   names.item_class(item_id), names.item_subclass(item_id),
                                   names.quality(item_id), names.get(item_id),
                                   names.purchase_price(item_id)):
            continue
        # "hvis det er transmog test imod unique transmog - hvis ikke unique
        # --> ignore item". source_count() is None for anything with no
        # transmog appearance at all (caged pets, mounts, recipes,
        # consumables) -- those are simply not transmog, so the test does
        # not apply to them and they pass. Profession tools/gear are
        # likewise treated as not-transmog here: they carry an appearance
        # but are not part of the visible paperdoll system, which is the
        # same reason snipe_check.NON_TRANSMOG_INVENTORY_TYPES exists.
        # Note this is the *opposite* disposition from
        # snipe_check._filter_by_appearance(), and deliberately so: that
        # function answers "give me unique-transmog items" (so a profession
        # tool must be excluded), whereas this rule asks "if this is
        # transmog, is it unique?" (so a non-transmog item is simply not
        # subject to the test). Different predicate, not an inconsistency.
        sources = appearances.source_count(item_id)
        is_transmog = (sources is not None
                       and inventory_type not in snipe_check.NON_TRANSMOG_INVENTORY_TYPES)
        if is_transmog and sources != 1:
            continue
        cand["appearance_sources"] = sources
        kept.append(cand)
    names.save()
    return sorted(kept, key=lambda c: c["region_sale_avg_copper"], reverse=True)


def _rule_state_load() -> dict:
    try:
        return json.loads(_rule_state_path().read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        # A truncated/corrupt state file must not take the collector cycle
        # down with it -- worst case we re-notify once and rewrite it.
        log.exception("watchlist: unreadable rule state, starting fresh")
        return {}


def _rule_state_save(state: dict) -> None:
    """Temp-file-then-rename, same atomicity fix scan_region.py already
    applies to its parquet writes -- a crash mid-write must not leave a
    half-written JSON file that _rule_state_load() then throws away,
    re-notifying everything."""
    path = _rule_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


# One line per find inside a single embed, rather than one embed per find
# (2026-08-05, human request: "can we send all snipes for a user in 1
# message instead ... only carries relevant stuff"). Five facts, in the
# order a decision actually gets made: what it is, what it costs, what it's
# worth, where it is, and which of your accounts can reach it.
#
# Discord caps an embed description at 4096 characters. At roughly 110
# characters a line that is far more than RULE_MAX_NOTIFICATIONS_PER_CYCLE
# could ever produce, but the cap is enforced rather than assumed -- a
# silently-rejected webhook POST would look exactly like "no snipes today".
EMBED_DESCRIPTION_MAX = 4096


def _rule_line(cand: dict, name_cache: NameCache,
               account_labels: list[tuple[str, str | None]] | None) -> str:
    name = name_cache.get(cand["item_id"], cand["pet_species_id"])
    url = _undermine_url(cand["cr_id"], cand["item_id"])
    # Markdown link on the name only -- the whole line as a link would make
    # the prices look clickable too.
    label = f"[{name}]({url})" if url else name
    parts = [f"**{label}**",
             f"**{cand['buy_copper'] / 10000:,.0f}g**",
             f"avg {cand['region_sale_avg_copper'] / 10000:,.0f}g",
             _realm_label(cand["cr_id"])]
    if account_labels:
        parts.append(", ".join(lbl for lbl, _realm in account_labels))
    return " · ".join(parts)


def _rule_send_batch(webhook_url: str, cands: list[dict], name_cache: NameCache,
                     account_labels_by_realm: dict[int, list[tuple[str, str | None]]]) -> None:
    """All of one user's finds for this cycle, as a single Discord message."""
    if not cands:
        return
    lines = []
    for cand in cands:
        line = _rule_line(cand, name_cache, account_labels_by_realm.get(cand["cr_id"]))
        if sum(len(x) + 1 for x in lines) + len(line) > EMBED_DESCRIPTION_MAX:
            break
        lines.append(line)
    # Date + time in the title (2026-08-05, human request) so a stack of
    # messages in a busy channel is scannable without expanding any of them.
    #
    # Spelled UTC explicitly rather than using the server's local zone,
    # which is whatever the Railway container happens to be -- an unlabelled
    # time from a server is worse than no time at all. `%d %b`, not `%-d`:
    # the no-pad flag is glibc-only and raises ValueError on Windows, where
    # this project's tests actually run.
    #
    # The embed also carries a real `timestamp`, which Discord renders in
    # the *reader's* own timezone next to the footer -- so between the two
    # there's an unambiguous absolute time and a local one, without this
    # code having to guess where the reader is.
    now = datetime.now(timezone.utc)
    payload = {"embeds": [{
        "title": (f"{len(lines)} snipe{'' if len(lines) == 1 else 's'}"
                  f" · {now.strftime('%d %b %H:%M')} UTC"),
        "color": EMBED_COLOR_RULE,
        "description": "\n".join(lines),
        "timestamp": now.isoformat(),
        "footer": {"text": "Default sniper list"},
    }]}
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        log.exception("watchlist: rule Discord webhook POST failed (%s finds)", len(lines))


# undermine.exchange URL shape, taken from the human's own link
# (2026-08-05): https://undermine.exchange/#eu-draenor/204925 -- region-
# prefixed realm slug, then the item id. EU-only, matching this product's
# scope.
UNDERMINE_URL = "https://undermine.exchange/#eu-{slug}/{item_id}"


def _undermine_url(cr_id: int, item_id: int) -> str | None:
    """Deep link to the item's undermine.exchange page for the realm the
    listing is actually on. Returns None rather than a guessed URL when the
    realm lookup fails -- a dead link in a notification is worse than no
    link.

    A connected realm can bundle several named realms; any member's slug
    resolves to the same auction house, so the first is used."""
    try:
        import blizz
        for m in blizz.connected_realm_realms(cr_id):
            if m.get("slug"):
                return UNDERMINE_URL.format(slug=m["slug"], item_id=item_id)
    except Exception:
        log.exception("watchlist: undermine link lookup failed for %s", cr_id)
    return None


# Discord embed colors -- the two notification shapes stay visually
# distinct at a glance in a busy channel: a per-item trigger the user set
# themselves vs. a standing-rule find.
EMBED_COLOR_TRIGGER = 0xC8912B   # bullion, matching the app's own accent
EMBED_COLOR_RULE = 0x2E7D5B      # verified green


def _embed_message(title: str, url: str | None, color: int,
                   fields: list[tuple[str, str, bool]],
                   footer: str | None = None) -> dict:
    """Discord webhook payload as a single embed rather than a run-on
    `content` string. `fields` is [(name, value, inline)]. Still used by the
    per-item trigger path; the standing rule batches instead, see
    _rule_send_batch()."""
    embed = {
        "title": title,
        "color": color,
        "fields": [{"name": n, "value": v, "inline": i} for n, v, i in fields],
    }
    if url:
        embed["url"] = url
    if footer:
        embed["footer"] = {"text": footer}
    return {"embeds": [embed]}


def _realm_label(cr_id: int) -> str:
    """Every member realm name of the connected realm, not just the first
    (2026-08-02, human request -- matches the fix already made for
    dashboard.py's realm chips: a connected realm can bundle several named
    realms sharing one AH, e.g. Zul'jin and Sanguino, confirmed live -- a
    user whose account is registered under one name shouldn't be told
    about a listing "on Sanguino" when they only recognize "Zul'jin").
    Joined with " / ", with an explicit "(connected realm)" note when
    there's more than one name, since a Discord message doesn't have this
    app's own UI convention to lean on for context the way profile.html's
    realm chips do."""
    try:
        import blizz
        members = blizz.connected_realm_realms(cr_id)
        names = [m.get("name") for m in members if m.get("name")]
        if names:
            label = " / ".join(names)
            if len(names) > 1:
                label += " (connected realm)"
            return label
    except Exception:
        log.exception("watchlist: realm label lookup failed for %s", cr_id)
    return str(cr_id)


async def _account_labels_for_realm(owner_id: uuid.UUID, cr_id: int,
                                    session: AsyncSession) -> list[tuple[str, str | None]]:
    """Which of the user's own registered WoW accounts (wow_accounts.py)
    have a character on the buy realm, if any -- same cross-reference
    dashboard.html's "Your account" column does client-side, but here it's
    the only way to get that answer into a Discord message, which has no
    client-side JS of its own. A realm can legitimately be registered on
    more than one of a user's WoW accounts (no uniqueness constraint across
    accounts, only within one), so this returns every match, not just the
    first. Empty list if the user hasn't registered any account for this
    realm (or has no WoW accounts at all) -- the caller omits the mention
    entirely rather than saying "no account" every single time, since most
    users may never use this optional feature.

    Each result also carries the specific member realm name the user
    picked at registration time (WowAccountRealm.realm_name, added
    2026-08-02 -- see that column's docstring), or None for a registration
    made before that column existed. A connected realm can bundle several
    names (e.g. Zul'jin/Sanguino) under one id, so this is what lets the
    message say exactly which one this particular account is on instead of
    just the account label alone."""
    rows = (await session.execute(
        select(WowAccount.label, WowAccountRealm.realm_name)
        .join(WowAccountRealm, WowAccountRealm.wow_account_id == WowAccount.id)
        .where(WowAccount.owner_id == owner_id, WowAccountRealm.connected_realm_id == cr_id)
        .order_by(WowAccount.id)
    )).all()
    return [(label, realm_name) for label, realm_name in rows]


def _send_discord_notification(webhook_url: str, item: WatchlistItem, price_copper: int, cr_id: int,
                               name_cache: NameCache,
                               account_labels: list[tuple[str, str | None]] | None = None) -> None:
    name = name_cache.get(item.item_id, item.pet_species_id)
    price_g = price_copper / 10000
    title = f"\U0001F514 {name}" + (f" ({item.label})" if item.label else "")
    # Reads back in whichever mode the user actually set, so the message
    # explains why it fired rather than restating a number they never chose.
    if item.trigger_percent is not None:
        trigger_text = f"under {item.trigger_percent}% of sale avg"
    else:
        trigger_text = f"{item.trigger_price_copper / 10000:,.2f}g"
    fields = [
        ("Price", f"**{price_g:,.2f}g**", True),
        ("Your trigger", trigger_text, True),
        ("Realm", _realm_label(cr_id), False),
    ]
    if account_labels:
        parts = [f"{label} ({realm_name})" if realm_name else label
                for label, realm_name in account_labels]
        fields.append(("Log in on", ", ".join(parts), False))
    payload = _embed_message(title, _undermine_url(cr_id, item.item_id),
                             EMBED_COLOR_TRIGGER, fields, footer="Watchlist trigger")
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        log.exception("watchlist: Discord webhook POST failed for item %s", item.id)


async def _check_triggers_async() -> dict:
    # db.isolated_session(), not db.sessionmaker()'s process-wide singleton
    # -- see that function's docstring for why (asyncpg connections are
    # loop-bound, and this coroutine runs inside its own asyncio.run() loop,
    # separate from the main FastAPI loop that owns db.sessionmaker()'s pool).
    async with db.isolated_session() as session:
        rows = (await session.execute(
            select(WatchlistItem, User)
            .join(User, WatchlistItem.owner_id == User.id)
            .where(WatchlistItem.trigger_price_copper.isnot(None)
                   | WatchlistItem.trigger_percent.isnot(None))
        )).all()
        if not rows:
            return {"watched": 0, "notified": 0}

        item_ids = list({item.item_id for item, _user in rows})
        cheapest = await asyncio.to_thread(_region_cheapest_by_item, item_ids)
        if not cheapest:
            return {"watched": len(rows), "notified": 0}

        name_cache = NameCache()
        sale_rates = tsm.SaleRateCache()
        now = datetime.now(timezone.utc)
        notified = 0
        for item, user in rows:
            key = (item.item_id, item.pet_species_id)
            hit = cheapest.get(key)
            if hit is None:
                continue
            if item.trigger_percent is not None:
                # Percentage mode: the threshold is derived from the item's
                # TSM region sale average, exactly as the standing rule
                # does. No TSM data means no threshold to compare against,
                # so the item stays silent rather than being guessed at --
                # the same "unknown isn't a claim" resolution used there.
                entry = sale_rates.get(item.item_id)
                avg = entry.get("avg_sale_price") if entry else None
                if avg is None or hit[0] >= avg * (item.trigger_percent / 100):
                    continue
            elif hit[0] > item.trigger_price_copper:
                continue
            if item.last_notified_at is not None:
                # SQLite (tests) returns a naive datetime for a
                # DateTime(timezone=True) column even though we always
                # write datetime.now(timezone.utc) -- Postgres (production,
                # via asyncpg) round-trips tz-aware correctly. Normalize
                # rather than assume either driver's behavior.
                last_notified_at = item.last_notified_at
                if last_notified_at.tzinfo is None:
                    last_notified_at = last_notified_at.replace(tzinfo=timezone.utc)
                elapsed = (now - last_notified_at).total_seconds()
                if elapsed < NOTIFY_COOLDOWN_SECONDS:
                    continue
            if user.discord_webhook_url:
                price_copper, cr_id = hit
                account_labels = await _account_labels_for_realm(user.id, cr_id, session)
                await asyncio.to_thread(
                    _send_discord_notification, user.discord_webhook_url, item, price_copper, cr_id,
                    name_cache, account_labels,
                )
            item.last_notified_at = now
            notified += 1
        await session.commit()
        return {"watched": len(rows), "notified": notified}


async def _check_rule_async() -> dict:
    """The standing rule's own pass. Deliberately *not* folded into
    _check_triggers_async(): that function returns early when no user has
    any WatchlistItem at all, and the rule owns no rows, so riding along
    inside it would silently never run in exactly the state this feature
    is most likely to be tested in."""
    recipients_q = select(User).where(User.discord_webhook_url.isnot(None))

    async with db.isolated_session() as session:
        recipients = [u for u in (await session.execute(recipients_q)).scalars().all()
                      if has_active_subscription(u) and u.default_sniper_list_enabled]
        if not recipients:
            # Nobody to tell -- skip the scan entirely rather than burning a
            # DuckDB pass over the whole region sweep every 10 minutes for
            # output that would be discarded. Note this also means the
            # cooldown file stays untouched, so enabling a webhook later
            # starts from a clean slate rather than a backlog.
            return {"rule_recipients": 0, "rule_hits": 0, "rule_notified": 0}

        # One DuckDB pass, shared by everyone -- it depends on nothing
        # user-specific. Only the cheap per-threshold filtering repeats.
        candidates = await asyncio.to_thread(_rule_candidates)
        if not candidates:
            return {"rule_recipients": len(recipients), "rule_hits": 0, "rule_notified": 0}

        # Grouped by threshold pair so the scan runs once per *distinct*
        # setting rather than once per user; in practice almost everyone is
        # on the defaults, so this is usually a single pass.
        by_thresholds: dict[tuple[int, float], list[User]] = {}
        for user in recipients:
            by_thresholds.setdefault(_rule_thresholds_for(user), []).append(user)

        state = await asyncio.to_thread(_rule_state_load)
        now = time.time()
        name_cache = NameCache()
        notified = 0
        suppressed_by_cap = 0
        total_hits = 0
        for (min_avg, frac), group in by_thresholds.items():
            hits = await asyncio.to_thread(_rule_scan, min_avg, frac, candidates)
            total_hits += len(hits)
            for user in group:
                # Collect this user's whole cycle first, then send once.
                to_send = []
                for cand in hits:
                    # Keyed per (user, item, pet). It used to be per item
                    # alone, on the reasoning that a hit is a fact about the
                    # region -- but thresholds are per-user now, so an item
                    # can be a hit for one account and not another, and a
                    # shared key would let one user's message start a
                    # cooldown that silences someone else's first-ever
                    # notification for the same item.
                    key = f"{user.id}:{cand['item_id']}:{cand['pet_species_id']}"
                    last = state.get(key)
                    if last is not None and (now - last) < NOTIFY_COOLDOWN_SECONDS:
                        continue
                    if len(to_send) >= RULE_MAX_NOTIFICATIONS_PER_CYCLE:
                        # Not marked notified -- reconsidered next cycle.
                        suppressed_by_cap += 1
                        continue
                    to_send.append(cand)
                if not to_send:
                    continue
                labels_by_realm = {}
                for cand in to_send:
                    if cand["cr_id"] not in labels_by_realm:
                        labels_by_realm[cand["cr_id"]] = await _account_labels_for_realm(
                            user.id, cand["cr_id"], session)
                await asyncio.to_thread(_rule_send_batch, user.discord_webhook_url,
                                        to_send, name_cache, labels_by_realm)
                # Marked only after the send is attempted, so a crash before
                # this point leaves them to be retried next cycle.
                for cand in to_send:
                    state[f"{user.id}:{cand['item_id']}:{cand['pet_species_id']}"] = now
                notified += len(to_send)
        await asyncio.to_thread(_rule_state_save, state)
        return {"rule_recipients": len(recipients), "rule_hits": total_hits,
                "rule_notified": notified, "rule_suppressed_by_cap": suppressed_by_cap}


def check_triggers() -> dict:
    """Sync entry point -- called from collect_all.py's background loop
    (itself invoked via asyncio.to_thread(), a real OS thread with no
    running event loop), so a fresh asyncio.run() here doesn't raise on a
    nested loop. That alone isn't sufficient, though -- see
    _check_triggers_async()'s own docstring for why it can't reuse
    db.sessionmaker()'s shared engine even so."""
    start = time.monotonic()
    result = asyncio.run(_check_triggers_async())
    # Own asyncio.run(), own isolated_session -- and its own try/except, so
    # an experimental rule can never take the established per-item trigger
    # path down with it. Same "one subsystem's failure doesn't abort the
    # rest" convention collect_all.collect_all() already applies to every
    # block it runs.
    try:
        result.update(asyncio.run(_check_rule_async()))
    except Exception:
        log.exception("watchlist: standing rule scan failed")
        result["rule_error"] = True
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    log.info("watchlist.check_triggers: %s", result)
    return result
