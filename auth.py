"""FastAPI-Users wiring: email/password register+login, cookie-based sessions
(not bearer/JWT-in-header -- the frontend is plain static HTML/JS, not an
SPA managing tokens, so letting the browser handle the cookie automatically
is the simpler fit, matching the no-build-step convention in static/).
"""
import os
import uuid

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import db
from db import User, get_user_db

SECRET = os.environ.get("SECRET")
if not SECRET:
    raise SystemExit("Set SECRET in .env (see README) -- signs session cookies "
                     "and password-reset/verification tokens. Any long random string.")

COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days

# CookieTransport defaults to Secure=True, which is correct in production
# (Railway serves HTTPS) but silently drops the cookie on every non-HTTPS
# request -- local dev/tests run over plain http://. COOKIE_SECURE=false in
# .env for local work; leave unset (defaults true) for the real deployment.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() not in ("false", "0", "")


class UserRead(schemas.BaseUser[uuid.UUID]):
    subscription_status: str | None = None


class UserCreate(schemas.BaseUserCreate):
    pass


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


cookie_transport = CookieTransport(cookie_name="ah_auth", cookie_max_age=COOKIE_MAX_AGE_SECONDS,
                                   cookie_secure=COOKIE_SECURE)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=COOKIE_MAX_AGE_SECONDS)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

current_active_user = fastapi_users.current_user(active=True)


async def resolve_user_from_request(request: Request) -> User | None:
    """Manually resolves the current user from the request's auth cookie,
    using a session opened and closed within this function -- not the
    request-scoped `Depends(current_active_user)` chain, which holds a
    Postgres connection checked out for the *entire* request (FastAPI only
    releases a `Depends()`-with-`yield` resource after the full response
    has been sent, confirmed by reading `fastapi/routing.py`'s
    `solve_dependencies`/`AsyncExitStack` handling). Added 2026-08-01 for
    `dashboard.api_snipes()`, the one route in this app that does 30-175s
    of unrelated DuckDB/Blizzard work after authenticating -- holding a
    pooled connection open for all of that, on every single call at every
    tier, was the direct cause of a real production incident (the Postgres
    pool exhausted, breaking login, under barely any real traffic).

    Mirrors `fastapi_users.current_user(active=True)`'s actual behavior
    exactly (confirmed by reading the installed package's
    `Authenticator._authenticate()`/`JWTStrategy.read_token()`): only the
    `active` check is applied here -- this app never uses `verified=True`
    or `superuser=True` anywhere (see `current_active_user`'s own
    definition above), so replicating just that one check is complete, not
    a partial reimplementation. Returns `None` (never raises) on any
    failure -- no cookie, invalid/expired token, unknown user, inactive
    user -- callers are responsible for turning that into a 401 themselves,
    matching how `current_active_user` only raises at the FastAPI
    dependency-resolution layer, not inside this helper.

    `db.sessionmaker()` uses `expire_on_commit=False`, so the returned
    `User`'s already-loaded scalar columns (id, email, is_active,
    is_superuser, subscription_status, locked_sell_realm, nickname) stay
    safely readable after this function returns and the session/connection
    has closed -- nothing downstream needs a live session for what it does
    with `user`."""
    token = request.cookies.get(cookie_transport.cookie_name)
    if token is None:
        return None
    async with db.sessionmaker()() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)
        user = await get_jwt_strategy().read_token(token, user_manager)
    if user is None or not user.is_active:
        return None
    return user


# Anonymous-visitor session cookie (2026-08-03, see db.AnonSession) --
# deliberately a DIFFERENT name than ah_auth's cookie_transport.cookie_name:
# CookieTransport/JWTStrategy would otherwise try to JWT-decode this value as
# if it were a real auth token. httponly/samesite/secure explicitly match
# what CookieTransport applies by default for ah_auth, so the two cookies
# behave consistently even though this one is hand-rolled via
# response.set_cookie() rather than going through fastapi-users at all.
ANON_COOKIE_NAME = "ah_anon"


async def resolve_or_create_anon_session(request: Request, session: AsyncSession) -> str:
    """Returns the anonymous-visitor token for this request, minting one on a
    visitor's first-ever request. Used by dashboard.py's
    api_me()/api_snipes() -- both call this only once
    resolve_user_from_request() has already returned None, i.e. this is the
    "no real account" fallback path, never a replacement for real auth.

    Does NOT set the ah_anon cookie directly (that used to be a `response:
    Response` parameter here, removed 2026-08-03 -- a real bug found while
    testing: FastAPI's exception handling builds an entirely separate
    Response when a route raises HTTPException, which never sees mutations
    made to a route-local injected Response parameter -- and /api/snipes
    routinely raises HTTPException for anonymous requests, 400 for an
    uncollected realm or 403 for a different-realm lock, which is the
    *common* case here, not an edge case). Instead this stashes the resolved
    token on `request.state.anon_token`; dashboard.py's ensure_anon_cookie
    middleware reads it after call_next() and sets the cookie on the
    genuinely final response, which is unaffected by exception handling
    since middleware wraps the whole request/response cycle.

    The defensive re-SELECT when a cookie value is already present guards
    against a stale/garbage cookie (e.g. a manually-edited dev cookie, or a
    row that was somehow removed) not matching any real row -- in that case
    a fresh token is minted rather than trusting the client-supplied value
    blindly, same "never trust client input, degrade gracefully" posture
    resolve_user_from_request() already has for a bad ah_auth cookie."""
    token = request.cookies.get(ANON_COOKIE_NAME)
    if token is not None:
        existing = (await session.execute(
            select(db.AnonSession.token).where(db.AnonSession.token == token)
        )).scalar_one_or_none()
        if existing is not None:
            request.state.anon_token = token
            return token
    token = uuid.uuid4().hex
    session.add(db.AnonSession(token=token))
    await session.commit()
    request.state.anon_token = token
    return token


def has_active_subscription(user: User) -> bool:
    """Single source of truth for the sniper-page gate (dashboard.py) --
    billing.py's Stripe webhook is the only writer of subscription_status.
    Superusers (is_superuser, FastAPI-Users' existing field -- there's no
    public API to set it, has to be flipped directly in the DB) always pass,
    no real subscription needed: founder/admin access, not a Stripe concept."""
    return user.is_superuser or user.subscription_status == "active"


async def current_subscribed_user(user: User = Depends(current_active_user)) -> User:
    """Stricter than current_active_user -- requires login AND an active
    Stripe subscription. 402 (not 401/403) so the frontend can tell "you're
    not logged in" apart from "you're logged in but not subscribed" and
    redirect to /login vs /subscribe accordingly."""
    if not has_active_subscription(user):
        raise HTTPException(402, "Active subscription required")
    return user
