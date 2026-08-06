"""Superuser-only admin surface: who's on the site right now, and the full
history of every client IP that has ever hit an /api/* route.

Extracted from dashboard.py 2026-08-04. The human's opening question was
whether this should be its own Railway service ("to sort aiming for
microservices"). It shouldn't, and the reason is structural rather than a
matter of taste: the thing being observed is *this process's own request
traffic*, so the middleware that records it has to run in the process that
serves it. A separate service could read the history table, but the writer
would still live here -- the split would buy a duplicated auth stack and a
second deploy target for zero isolation. So this is a module boundary, not
a process boundary. (It is also the necessary first step if that ever does
become a real service, so nothing is foreclosed.)

Two-layer design, and the layering is the whole point:

- The request path stays memory-only. track_activity() writes one dict
  entry and returns. It does NOT touch Postgres, because db.engine()'s
  comment documents a real outage -- QueuePool exhausted, login failing,
  from a single account browsing lightly -- and the dashboard auto-refreshes,
  so a per-request INSERT would push directly on the thing that already
  broke once.
- _visitor_flush_loop() batches that dict into db.VisitorIP roughly once a
  minute. One write per minute, not one per request.

Everything a superuser reads comes from the table, so the numbers survive a
redeploy (the in-memory dict does not). The dict is now only a write buffer
and a liveness signal, not the source of truth it was when this lived in
dashboard.py.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi_users.jwt import decode_jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import db
from auth import SECRET, cookie_transport, current_active_user, get_jwt_strategy
from db import User, VisitorIP, WatchlistItem, get_async_session

log = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

# "Currently on the site" = seen in the last 15 min.
ACTIVE_WINDOW_SECONDS = 15 * 60
# How often the in-memory buffer is written through to Postgres. A crash or
# redeploy loses at most this much activity -- acceptable for observability
# data, and the tradeoff that keeps the request path free of DB writes.
FLUSH_INTERVAL_SECONDS = 60
# Cheap, opportunistic pruning trigger -- avoids the dict growing unbounded
# over a long-running process without needing a separate background task.
_ACTIVITY_PRUNE_THRESHOLD = 1000
# Longest possible IPv6 representation; VisitorIP.ip is String(45) to match.
# A longer value can only come from a malformed/hostile X-Forwarded-For.
MAX_IP_LEN = 45
# Cap on how many history rows the admin page pulls at once. Generous
# relative to a table that only grows by distinct visitor, but bounded so
# the endpoint can't degrade into an unbounded scan years from now.
VISITOR_HISTORY_LIMIT = 500
# Same bounding rationale for the signup list. Far above any plausible near-
# term account count, so in practice the page shows everyone.
SIGNUP_LIST_LIMIT = 500

# Cap on watchlist items returned for one account's expanded row. A user can
# hold watchlist.MAX_WATCHLIST_ITEMS_PER_USER (500), and a TSM group import
# routinely creates hundreds -- bounded so one expansion can't render a
# thousand-row table or resolve a thousand uncached item names.
WATCHLIST_DETAIL_LIMIT = 500

# ip -> last-seen unix timestamp. Live "who's here now" buffer.
_recent_activity: dict[str, float] = {}
# ip -> /api/* hits since the last successful flush. Drained by
# flush_visitors(); accumulated into VisitorIP.hit_count there.
_pending_hits: dict[str, int] = {}
# ip -> most recent authenticated user id seen from it. Drained by
# flush_visitors() into VisitorIP.user_id. Only ever holds entries for
# requests that carried a valid auth cookie, so an anonymous request never
# clears an existing association -- see _client_user_id().
_pending_user_ids: dict[str, uuid.UUID] = {}


async def current_superuser(user: User = Depends(current_active_user)) -> User:
    """403, not 401, for a logged-in non-superuser -- current_active_user has
    already proved they're logged in, and this is a stricter check on top of
    that, the same convention has_active_subscription's 402 follows in
    dashboard.py. Every route in this module depends on it, so there is one
    gate rather than a repeated `if not user.is_superuser` per route."""
    if not user.is_superuser:
        raise HTTPException(403)
    return user


def _client_ip(request: Request) -> str:
    """Best-effort real client IP. Railway's edge proxy connects to this
    app over an internal address -- request.client.host would show that
    proxy hop, not the real visitor (confirmed live: matches the
    fd12:.../upstreamAddress format seen in `railway logs --http` output,
    not a real public IP) -- so X-Forwarded-For (set by the proxy;
    confirmed live to match that same command's own `srcIp` field) is
    checked first. A chain (multiple proxies) puts the original client
    first, so only the first entry is used.

    Truncated to MAX_IP_LEN: this value is attacker-controlled (it's a
    request header) and is now a primary key in Postgres, where an
    over-length value would raise instead of being silently accepted the
    way the old in-memory dict did."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:MAX_IP_LEN]
    return (request.client.host if request.client else "unknown")[:MAX_IP_LEN]


def _client_user_id(request: Request) -> uuid.UUID | None:
    """The authenticated account behind this request, or None.

    Decodes the ah_auth JWT **locally and only** -- no database round trip.
    That constraint is the whole reason this doesn't just depend on
    auth.current_active_user: this runs in middleware on every /api/*
    request, and this module's docstring documents a real outage caused by
    exactly that kind of per-request database work. The token already
    carries the user id in `sub`, signed with SECRET, so the id is
    available without asking Postgres who they are.

    A signature/expiry/audience failure means "treat as anonymous", never an
    error: this is observability sitting in front of every request, so it
    must not be able to reject traffic that the real auth dependency would
    have accepted or rejected on its own terms. A forged token can't get
    past the signature check, and one that *is* validly signed but belongs
    to a deleted/deactivated account is caught at read time -- the admin
    endpoints join to the user table, so a dangling id renders as anonymous
    rather than as a fabricated account.
    """
    token = request.cookies.get(cookie_transport.cookie_name)
    if not token:
        return None
    try:
        payload = decode_jwt(token, SECRET, get_jwt_strategy().token_audience)
        return uuid.UUID(payload["sub"])
    except Exception:
        return None


async def track_activity(request: Request, call_next):
    """Records a hit against /api/* routes only -- static asset/page loads
    aren't a meaningful "is someone using the app" signal the way an API
    call is.

    Registered by dashboard.py via app.middleware("http"); middleware is a
    property of the app, not of a router, so it can't be attached here.

    Deliberately does no I/O -- see this module's docstring for why the DB
    write is deferred to _visitor_flush_loop() instead."""
    if request.url.path.startswith("/api/"):
        now = time.time()
        ip = _client_ip(request)
        _recent_activity[ip] = now
        _pending_hits[ip] = _pending_hits.get(ip, 0) + 1
        user_id = _client_user_id(request)
        if user_id is not None:
            _pending_user_ids[ip] = user_id
        if len(_recent_activity) > _ACTIVITY_PRUNE_THRESHOLD:
            cutoff = now - ACTIVE_WINDOW_SECONDS
            for stale in [k for k, v in _recent_activity.items() if v < cutoff]:
                del _recent_activity[stale]
    return await call_next(request)


async def flush_visitors(session: AsyncSession) -> int:
    """Drain _pending_hits into visitor_ip. Returns the number of IPs written.

    The buffer is swapped out up front rather than read-then-cleared, so
    hits arriving mid-flush land in the fresh dict and are counted by the
    next pass instead of being dropped.

    Plain SELECT-then-UPDATE/INSERT rather than a dialect-specific upsert
    (Postgres in production, SQLite under test): there is exactly one writer
    -- this one task, in one web process -- so the read-modify-write has no
    concurrent counterparty to race, and the portable version needs no
    separate SQLite path in the tests.

    Takes its session as an argument so tests can drive one flush directly
    against their own session without the loop or the app running."""
    global _pending_hits, _pending_user_ids
    pending, _pending_hits = _pending_hits, {}
    # Swapped in the same breath as the hit buffer so the two can't drift:
    # a user id arriving between the two swaps would otherwise be attributed
    # to an IP whose hits had already been drained.
    pending_users, _pending_user_ids = _pending_user_ids, {}
    if not pending:
        return 0

    now = datetime.now(timezone.utc)
    existing = (await session.execute(
        select(VisitorIP).where(VisitorIP.ip.in_(list(pending)))
    )).scalars().all()
    by_ip = {row.ip: row for row in existing}

    for ip, hits in pending.items():
        # None when only anonymous traffic came from this IP this interval.
        # Assigned on insert, but on update only when we actually have one --
        # a logged-in user's later anonymous request (or a logged-out one)
        # must not wipe the association. See db.VisitorIP.user_id.
        user_id = pending_users.get(ip)
        row = by_ip.get(ip)
        if row is None:
            session.add(VisitorIP(ip=ip, first_seen=now, last_seen=now,
                                  hit_count=hits, user_id=user_id))
        else:
            row.last_seen = now
            row.hit_count += hits
            if user_id is not None:
                row.user_id = user_id
    await session.commit()
    return len(pending)


async def _visitor_flush_loop() -> None:
    """Periodic write-through of the activity buffer.

    Uses db.sessionmaker() (the shared process-wide engine), NOT
    db.isolated_session() -- that one exists for callers running in their
    own asyncio.run() loop from a background *thread*, where asyncpg's
    loop-bound connections crash. This task is created by dashboard.py's
    lifespan and so runs on the main FastAPI event loop, the same loop that
    populated engine()'s pool, which is exactly the case the shared engine
    is for.

    Survives any exception, per the same convention collect_all's loop
    follows -- observability must never be able to take the site down. A
    failed flush loses that interval's counts (the buffer was already
    swapped) rather than retrying, which is the right trade for data whose
    whole purpose is a rough activity picture."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        try:
            async with db.sessionmaker()() as session:
                await flush_visitors(session)
        except Exception:
            log.exception("visitor activity flush failed")


def _as_utc(value: datetime) -> datetime:
    """Normalise a timestamp read back from the database to tz-aware UTC.

    DateTime(timezone=True) is honoured by Postgres but not by SQLite, which
    has no native timestamp type and hands back a *naive* datetime -- so the
    same column arrives aware in production and naive under test, and
    arithmetic mixing the two raises TypeError. Everything written here is
    UTC (flush_visitors uses datetime.now(timezone.utc)), so attaching UTC
    to a naive value recovers the real instant rather than guessing."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _row(row: VisitorIP, now: datetime, users: dict[uuid.UUID, User] | None = None) -> dict:
    """One history entry. last_seen is emitted both as an ISO timestamp and
    as an age in seconds -- the page renders the relative form ("4 min ago")
    but needs the absolute one for the title/tooltip, and computing the age
    server-side avoids every client having to trust its own clock.

    `users` maps user id -> User for the ids referenced by the rows being
    rendered, fetched once by the caller rather than lazily per row (which
    would be a query per visitor). An id with no entry -- a deleted account
    whose FK was SET NULL between the two reads -- renders as anonymous, the
    same as an IP that was never authenticated."""
    first_seen, last_seen = _as_utc(row.first_seen), _as_utc(row.last_seen)
    account = (users or {}).get(row.user_id) if row.user_id else None
    return {
        "ip": row.ip,
        "first_seen": first_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
        "last_seen_seconds_ago": max(0, round((now - last_seen).total_seconds())),
        "hit_count": row.hit_count,
        # None for anonymous traffic. Email is the durable identifier (see
        # /signups); nickname is whatever they chose to show publicly and is
        # NULL until a first forum post.
        "user_email": account.email if account else None,
        "user_nickname": account.nickname if account else None,
        "is_superuser": bool(account.is_superuser) if account else False,
    }


async def _users_for(rows: list[VisitorIP], session: AsyncSession) -> dict[uuid.UUID, User]:
    """Accounts referenced by these visitor rows, in one query. Returns {}
    when none of them carry a user id, so an all-anonymous page costs no
    extra round trip at all."""
    ids = {r.user_id for r in rows if r.user_id is not None}
    if not ids:
        return {}
    found = (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {u.id: u for u in found}


@router.get("/active-users")
async def api_admin_active_users(user: User = Depends(current_superuser),
                                 session: AsyncSession = Depends(get_async_session)) -> dict:
    """Who's currently on the site: distinct client IPs that have hit any
    /api/* route within ACTIVE_WINDOW_SECONDS.

    Reads the table, not _recent_activity, so a redeploy doesn't blank the
    view -- but unflushed hits from the last FLUSH_INTERVAL_SECONDS are
    still only in memory, so an IP whose very first-ever request arrived
    seconds ago can lag by up to that interval before appearing. Merging the
    live dict in here would fix that at the cost of two sources of truth
    disagreeing about hit_count; a minute of lag on an observability page
    isn't worth that."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=ACTIVE_WINDOW_SECONDS)
    rows = (await session.execute(
        select(VisitorIP).where(VisitorIP.last_seen >= cutoff).order_by(VisitorIP.last_seen.desc())
    )).scalars().all()
    users = await _users_for(rows, session)
    return {
        "count": len(rows),
        "window_seconds": ACTIVE_WINDOW_SECONDS,
        # How many of those are a known account rather than anonymous
        # traffic -- the "what users are active" half of the 2026-08-06
        # request, as a number the page can put in a tile.
        "signed_in_count": sum(1 for r in rows if r.user_id in users),
        # Key kept as "ips" (not renamed to "visitors") -- this endpoint
        # already shipped 2026-08-01 and this module's own admin page is
        # not necessarily the only reader.
        "ips": [_row(r, now, users) for r in rows],
    }


@router.get("/visitors")
async def api_admin_visitors(user: User = Depends(current_superuser),
                             session: AsyncSession = Depends(get_async_session)) -> dict:
    """Full visitor history, most recently active first -- one entry per
    distinct IP ever seen, with first/last seen and lifetime hit count."""
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(VisitorIP).order_by(VisitorIP.last_seen.desc()).limit(VISITOR_HISTORY_LIMIT)
    )).scalars().all()
    active_cutoff = now - timedelta(seconds=ACTIVE_WINDOW_SECONDS)
    users = await _users_for(rows, session)
    return {
        "count": len(rows),
        "limit": VISITOR_HISTORY_LIMIT,
        "active_window_seconds": ACTIVE_WINDOW_SECONDS,
        "visitors": [
            {**_row(r, now, users), "is_active": _as_utc(r.last_seen) >= active_cutoff}
            for r in rows
        ],
    }


@router.get("/signups")
async def api_admin_signups(user: User = Depends(current_superuser),
                            session: AsyncSession = Depends(get_async_session)) -> dict:
    """Registered accounts, newest signup first (2026-08-04 human request:
    "new users actual signups also with mail").

    Distinct from /visitors, which counts anonymous traffic by IP -- this is
    the real account list, so the two answer different questions and neither
    subsumes the other.

    Fields are an explicit allowlist, not the whole row: a User also carries
    hashed_password and the Stripe customer/subscription ids, and none of
    those belong in a JSON response, even a superuser-only one. Email is
    included because it *is* the request -- and it's the only durable
    identifier an account has, since nickname stays NULL until a first forum
    post.

    created_at is NULL for every account predating the column (see db.User)
    -- reported as null rather than guessed at; the page shows "before
    tracking".

    Ordered created_at DESC NULLS LAST so real recent signups sort to the top
    and undated legacy accounts sink to the bottom -- Postgres defaults to
    NULLS FIRST on DESC, which would bury exactly what's being looked for."""
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(User).order_by(User.created_at.desc().nullslast()).limit(SIGNUP_LIST_LIMIT)
    )).scalars().all()
    total = (await session.execute(select(func.count()).select_from(User))).scalar() or 0

    # Watchlist size per account, as one grouped query rather than a count
    # per row (2026-08-06). Accounts with an empty watchlist simply don't
    # appear in the result, so .get(id, 0) is the correct read.
    counts = dict((await session.execute(
        select(WatchlistItem.owner_id, func.count())
        .group_by(WatchlistItem.owner_id)
    )).all())

    signups = []
    for u in rows:
        created = _as_utc(u.created_at) if u.created_at is not None else None
        signups.append({
            # Needed by the page to fetch this account's watchlist detail on
            # expand. A user id is already exposed implicitly by every other
            # field here, and this endpoint is superuser-only.
            "id": str(u.id),
            "email": u.email,
            "nickname": u.nickname,
            "watchlist_count": counts.get(u.id, 0),
            # The standing sniper-list rule (watchlist.py) -- whether they
            # use it, deliberately not what it would match. Its contents are
            # region-wide and identical for everyone on the same thresholds,
            # so listing them per account would be the same rows repeated.
            "default_sniper_list_enabled": bool(u.default_sniper_list_enabled),
            "has_discord_webhook": bool(u.discord_webhook_url),
            "created_at": created.isoformat() if created else None,
            "signed_up_seconds_ago": (max(0, round((now - created).total_seconds()))
                                      if created else None),
            # Straight from billing.py's Stripe webhook; None = never subscribed.
            "subscription_status": u.subscription_status,
            "is_verified": u.is_verified,
            "is_active": u.is_active,
            "is_superuser": u.is_superuser,
        })
    return {
        "count": len(signups),
        "total": total,
        "limit": SIGNUP_LIST_LIMIT,
        "signups": signups,
    }


@router.get("/watchlist/{owner_id}")
async def api_admin_watchlist(owner_id: uuid.UUID, user: User = Depends(current_superuser),
                              session: AsyncSession = Depends(get_async_session)) -> dict:
    """One account's watchlist items, for the admin page's collapsible row
    (2026-08-06 human request: "if they use the watchlist what items in a
    collapse list").

    Its own endpoint rather than nesting the items inside /signups, and
    fetched only when a row is actually expanded: a single account may hold
    up to watchlist.MAX_WATCHLIST_ITEMS_PER_USER (500) items, so embedding
    them would make the signup list's payload grow with the product's own
    success, for data almost none of which is on screen at any moment.

    Deliberately does NOT include the standing sniper-list rule's matches --
    /signups reports only whether the account has it switched on. Those
    matches are region-wide and identical for every account sharing the same
    thresholds, so rendering them per user would repeat one list N times.

    Names come from the shared NameCache. A cold entry makes a live blocking
    Blizzard call, so the whole build runs in asyncio.to_thread() and the
    resolve is bounded by the same deadline watchlist.list_watchlist() uses
    -- the identical precaution, for the identical reason (see CLAUDE.md's
    "Real production outage"). An unresolved item still renders, with its id
    standing in for the name."""
    items = (await session.execute(
        select(WatchlistItem).where(WatchlistItem.owner_id == owner_id)
        .order_by(WatchlistItem.id).limit(WATCHLIST_DETAIL_LIMIT)
    )).scalars().all()
    total = (await session.execute(
        select(func.count()).select_from(WatchlistItem)
        .where(WatchlistItem.owner_id == owner_id)
    )).scalar() or 0

    if not items:
        return {"count": 0, "total": 0, "limit": WATCHLIST_DETAIL_LIMIT, "items": []}

    def _build() -> list[dict]:
        # Imported here, not at module scope: item_names pulls in the
        # Blizzard client stack, and admin.py is imported by dashboard.py at
        # startup purely for its middleware.
        from item_names import LIVE_RESOLVE_DEADLINE_SECONDS, NameCache
        cache = NameCache()
        ids = list({i.item_id for i in items})
        cache.ensure_many(ids, max_workers=16,
                          deadline_seconds=LIVE_RESOLVE_DEADLINE_SECONDS)
        out = []
        for i in items:
            out.append({
                "item_id": i.item_id,
                "pet_species_id": i.pet_species_id,
                "name": cache.get(i.item_id, i.pet_species_id),
                "quality_color": cache.quality_color(i.item_id, i.pet_species_id),
                # Exactly one of these is set -- they're mutually exclusive
                # per row (see db.WatchlistItem), so the page can render
                # whichever mode the user actually chose.
                "trigger_price_g": (i.trigger_price_copper / 10000
                                    if i.trigger_price_copper is not None else None),
                "trigger_percent": i.trigger_percent,
                "label": i.label,
            })
        return out

    return {"count": len(items), "total": total, "limit": WATCHLIST_DETAIL_LIMIT,
            "items": await asyncio.to_thread(_build)}
