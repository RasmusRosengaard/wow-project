"""Watchlist -- track specific items across every EU realm, independent of
any sell realm (see FEATURE_WATCHLIST.md). A user adds items either one at a
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
rides the existing ~10-minute cycle (FEATURE_WATCHLIST.md's open question
#6, resolved: no new scan cadence).
"""
import asyncio
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
from auth import current_subscribed_user
from db import User, WatchlistItem, get_async_session
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


def _item_json(item: WatchlistItem, name_cache: NameCache | None = None) -> dict:
    out = {
        "id": item.id,
        "item_id": item.item_id,
        "pet_species_id": item.pet_species_id,
        "trigger_price_g": (item.trigger_price_copper / 10000) if item.trigger_price_copper is not None else None,
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
                              session: AsyncSession) -> bool:
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
         WatchlistItem.trigger_price_copper, WatchlistItem.label, WatchlistItem.created_at],
        select(
            literal(owner_id, type_=WatchlistItem.owner_id.type),
            literal(item_id, type_=WatchlistItem.item_id.type),
            literal(pet_species_id, type_=WatchlistItem.pet_species_id.type),
            literal(trigger_price_copper, type_=WatchlistItem.trigger_price_copper.type),
            literal(label, type_=WatchlistItem.label.type),
            literal(datetime.now(timezone.utc), type_=WatchlistItem.created_at.type),
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
    if payload.trigger_price_g is None or payload.trigger_price_g <= 0:
        raise HTTPException(400, "trigger price is required and must be greater than 0")
    label = (payload.label or "").strip() or None
    if label and len(label) > LABEL_MAX_LEN:
        raise HTTPException(400, f"label must be {LABEL_MAX_LEN} characters or fewer")
    trigger_copper = round(payload.trigger_price_g * 10000) if payload.trigger_price_g is not None else None

    inserted = await _insert_item_atomic(user.id, payload.item_id, payload.pet_species_id,
                                         trigger_copper, label, session)
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


@router.patch("/{item_id}")
async def update_item(item_id: int, payload: WatchlistItemUpdate,
                      user: User = Depends(current_subscribed_user),
                      session: AsyncSession = Depends(get_async_session)) -> dict:
    item = await _get_owned_item(item_id, user, session)
    if payload.trigger_price_g is not None and payload.trigger_price_g < 0:
        raise HTTPException(400, "trigger price can't be negative")
    item.trigger_price_copper = (
        round(payload.trigger_price_g * 10000) if payload.trigger_price_g is not None else None
    )
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


def _realm_label(cr_id: int) -> str:
    try:
        import blizz
        members = blizz.connected_realm_realms(cr_id)
        if members:
            return members[0].get("name", str(cr_id))
    except Exception:
        log.exception("watchlist: realm label lookup failed for %s", cr_id)
    return str(cr_id)


def _send_discord_notification(webhook_url: str, item: WatchlistItem, price_copper: int, cr_id: int,
                               name_cache: NameCache) -> None:
    name = name_cache.get(item.item_id, item.pet_species_id)
    price_g = price_copper / 10000
    trigger_g = item.trigger_price_copper / 10000
    realm = _realm_label(cr_id)
    label_suffix = f" ({item.label})" if item.label else ""
    content = (
        f"\U0001F514 **{name}**{label_suffix} is at **{price_g:,.2f}g** on {realm} "
        f"-- your trigger was {trigger_g:,.2f}g."
    )
    try:
        requests.post(webhook_url, json={"content": content}, timeout=10)
    except Exception:
        log.exception("watchlist: Discord webhook POST failed for item %s", item.id)


async def _check_triggers_async() -> dict:
    async with db.sessionmaker()() as session:
        rows = (await session.execute(
            select(WatchlistItem, User)
            .join(User, WatchlistItem.owner_id == User.id)
            .where(WatchlistItem.trigger_price_copper.isnot(None))
        )).all()
        if not rows:
            return {"watched": 0, "notified": 0}

        item_ids = list({item.item_id for item, _user in rows})
        cheapest = await asyncio.to_thread(_region_cheapest_by_item, item_ids)
        if not cheapest:
            return {"watched": len(rows), "notified": 0}

        name_cache = NameCache()
        now = datetime.now(timezone.utc)
        notified = 0
        for item, user in rows:
            key = (item.item_id, item.pet_species_id)
            hit = cheapest.get(key)
            if hit is None or hit[0] > item.trigger_price_copper:
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
                await asyncio.to_thread(
                    _send_discord_notification, user.discord_webhook_url, item, price_copper, cr_id, name_cache,
                )
            item.last_notified_at = now
            notified += 1
        await session.commit()
        return {"watched": len(rows), "notified": notified}


def check_triggers() -> dict:
    """Sync entry point -- called from collect_all.py's background loop
    (itself invoked via asyncio.to_thread(), a real OS thread with no
    running event loop), so a fresh asyncio.run() here is safe and doesn't
    conflict with anything."""
    start = time.monotonic()
    result = asyncio.run(_check_triggers_async())
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    log.info("watchlist.check_triggers: %s", result)
    return result
