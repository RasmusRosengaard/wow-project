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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import db
from auth import current_active_user
from db import User, VisitorIP, get_async_session

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

# ip -> last-seen unix timestamp. Live "who's here now" buffer.
_recent_activity: dict[str, float] = {}
# ip -> /api/* hits since the last successful flush. Drained by
# flush_visitors(); accumulated into VisitorIP.hit_count there.
_pending_hits: dict[str, int] = {}


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
    global _pending_hits
    pending, _pending_hits = _pending_hits, {}
    if not pending:
        return 0

    now = datetime.now(timezone.utc)
    existing = (await session.execute(
        select(VisitorIP).where(VisitorIP.ip.in_(list(pending)))
    )).scalars().all()
    by_ip = {row.ip: row for row in existing}

    for ip, hits in pending.items():
        row = by_ip.get(ip)
        if row is None:
            session.add(VisitorIP(ip=ip, first_seen=now, last_seen=now, hit_count=hits))
        else:
            row.last_seen = now
            row.hit_count += hits
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


def _row(row: VisitorIP, now: datetime) -> dict:
    """One history entry. last_seen is emitted both as an ISO timestamp and
    as an age in seconds -- the page renders the relative form ("4 min ago")
    but needs the absolute one for the title/tooltip, and computing the age
    server-side avoids every client having to trust its own clock."""
    first_seen, last_seen = _as_utc(row.first_seen), _as_utc(row.last_seen)
    return {
        "ip": row.ip,
        "first_seen": first_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
        "last_seen_seconds_ago": max(0, round((now - last_seen).total_seconds())),
        "hit_count": row.hit_count,
    }


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
    return {
        "count": len(rows),
        "window_seconds": ACTIVE_WINDOW_SECONDS,
        # Key kept as "ips" (not renamed to "visitors") -- this endpoint
        # already shipped 2026-08-01 and this module's own admin page is
        # not necessarily the only reader.
        "ips": [_row(r, now) for r in rows],
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
    return {
        "count": len(rows),
        "limit": VISITOR_HISTORY_LIMIT,
        "active_window_seconds": ACTIVE_WINDOW_SECONDS,
        "visitors": [
            {**_row(r, now), "is_active": _as_utc(r.last_seen) >= active_cutoff}
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

    signups = []
    for u in rows:
        created = _as_utc(u.created_at) if u.created_at is not None else None
        signups.append({
            "email": u.email,
            "nickname": u.nickname,
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
