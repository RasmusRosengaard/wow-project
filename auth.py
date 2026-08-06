"""FastAPI-Users wiring: email/password register+login, cookie-based sessions
(not bearer/JWT-in-header -- the frontend is plain static HTML/JS, not an
SPA managing tokens, so letting the browser handle the cookie automatically
is the simpler fit, matching the no-build-step convention in static/).

Also (2026-08-06) email verification, password reset and Google OAuth login.
The three arrived together because they share one prerequisite -- an email
sender, see mailer.py -- and because a Google-created account has no usable
password, which makes self-service reset the only way such a user could ever
also log in with a password.
"""
import logging
import os
import uuid

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from httpx_oauth.clients.google import GoogleOAuth2
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import db
import mailer
from db import OAuthAccount, User, get_user_db

log = logging.getLogger(__name__)

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

# Absolute public origin, e.g. https://realm-arbitrage.com (added 2026-08-06).
# Two things need a real absolute URL and can't derive one safely from the
# request:
#
#   - Verification/reset links in emails. A link that says http:// works but
#     looks untrustworthy, and some mail clients warn on it.
#   - Google's redirect_uri, which must match what's registered in the Google
#     Cloud console **byte for byte** or the whole flow dies at the consent
#     screen with redirect_uri_mismatch.
#
# billing.py builds its Stripe URLs from str(request.base_url) instead, which
# infers the scheme from proxy headers. That is fine there (a Stripe
# success_url over http still redirects), but this app runs uvicorn without
# configuring forwarded_allow_ips for Railway's proxy -- admin._client_ip()
# parsing X-Forwarded-For by hand exists precisely because those headers aren't
# handled for us -- so the inferred scheme can't be trusted where an exact
# match is mandatory.
#
# Falls back to request.base_url when unset, so local dev and the test suite
# need no extra env var.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Google OAuth (2026-08-06). None when unconfigured rather than a hard failure
# at import: a fresh checkout, local dev without a Google project, and CI (no
# secrets) all still have to be able to `import auth`. dashboard.py mounts the
# OAuth router only when this is not None, and /api/status reports it so the
# frontend can hide a button that would 404. Same tolerate-missing-credentials
# posture billing.py takes with STRIPE_SECRET_KEY.
#
# Scopes are left at httpx-oauth's defaults (userinfo.profile +
# userinfo.email) deliberately -- do NOT narrow them to openid+email:
# GoogleOAuth2.get_id_email() resolves the address through the **People API**
# (https://people.googleapis.com/v1/people/me?personFields=emailAddresses),
# not the OIDC userinfo endpoint, so it needs userinfo.profile and the People
# API must be enabled on the Google Cloud project. Both scopes are in Google's
# non-sensitive tier, so no app-verification review is triggered.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
google_oauth_client = (
    GoogleOAuth2(GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET)
    if GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET else None
)

GOOGLE_CALLBACK_PATH = "/auth/google/callback"


def public_base_url(request: Request | None = None) -> str:
    """The origin to build user-facing absolute URLs from, no trailing slash.
    PUBLIC_BASE_URL when set (production), else derived from the request."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:8000"


class UserRead(schemas.BaseUser[uuid.UUID]):
    subscription_status: str | None = None


class UserCreate(schemas.BaseUserCreate):
    pass


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        """Kicks off email verification immediately on registration (2026-08-06)
        -- request_verify() mints the token and calls on_after_request_verify()
        below, which is what actually sends the mail.

        Skipped for an account that is already verified, which is exactly the
        Google case: get_oauth_router runs with is_verified_by_default=True, so
        a brand-new Google account arrives here already verified and must not be
        emailed a "confirm your address" link it has no reason to click.

        Deliberately does not raise on send failure -- mailer.send() swallows
        everything (see its docstring). An account whose email didn't go out is
        recoverable via the resend affordance on /verify; a 500 on a
        successfully-created account is not."""
        if user.is_verified:
            return
        await self.request_verify(user, request)

    async def on_after_request_verify(self, user: User, token: str,
                                      request: Request | None = None) -> None:
        link = f"{public_base_url(request)}/verify?token={token}"
        subject, html = mailer.verification_email(link)
        await mailer.send(user.email, subject, html)

    async def on_after_forgot_password(self, user: User, token: str,
                                       request: Request | None = None) -> None:
        link = f"{public_base_url(request)}/reset-password?token={token}"
        subject, html = mailer.password_reset_email(link)
        await mailer.send(user.email, subject, html)

    # Providers whose `account_email` is trusted to be genuinely confirmed.
    # Explicitly an allowlist keyed on the provider name, not a blanket "any
    # OAuth login verifies you": the reasoning below holds only for a provider
    # that actually validates addresses, and a future provider that doesn't
    # must not silently inherit it by being added to dashboard.py.
    EMAIL_VERIFYING_OAUTH_PROVIDERS = frozenset({"google"})

    async def oauth_callback(self, oauth_name: str, *args, **kwargs) -> User:
        """Marks the account verified when a trusted provider vouches for the
        address, including on the **association** path.

        FastAPI-Users' own `is_verified_by_default` only applies to a
        newly-created account (confirmed by reading BaseUserManager.oauth_callback:
        the `associate_by_email` branch never touches `is_verified`). Without
        this override, a real and not-rare sequence dead-ends: register with
        email+password, never click the confirmation link, then log in with
        Google on that same address. Google has just proven the person controls
        that mailbox -- which is the entire thing our own confirmation email
        exists to establish -- yet the account would stay unverified, still
        blocked from subscribing and posting, with a "confirm your email" banner
        immediately after a successful login. Fixing it here rather than asking
        the user to go find the emailed link is the honest behavior.

        Writes through `self.user_db.update()` rather than mutating and
        committing by hand, so it goes through the same adapter path every other
        user write in FastAPI-Users uses."""
        user = await super().oauth_callback(oauth_name, *args, **kwargs)
        if not user.is_verified and oauth_name in self.EMAIL_VERIFYING_OAUTH_PROVIDERS:
            user = await self.user_db.update(user, {"is_verified": True})
        return user


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


class RedirectCookieTransport(CookieTransport):
    """CookieTransport that ends a successful login with a 302 to /snipes
    instead of a bare 204 (2026-08-06, for the Google OAuth callback only).

    Necessary, not cosmetic. FastAPI-Users' OAuth callback finishes with
    `backend.login(strategy, user)` and returns whatever that produces
    (confirmed by reading the installed package's router/oauth.py), and plain
    CookieTransport.get_login_response() returns HTTP 204 with a Set-Cookie
    header. For /auth/login that's correct -- login.html fetch()es it and does
    its own window.location -- but the OAuth callback is a **full browser
    navigation** Google performs, so a 204 leaves the user staring at a blank
    page: logged in, cookie set, no way to tell it worked.

    Reuses the inherited _set_login_cookie() rather than calling
    response.set_cookie() with its own arguments. That's a private method, and
    the coupling is intentional: it guarantees the OAuth path can never drift
    from the email/password path on cookie name, max-age, secure, httponly or
    samesite. tests/test_auth.py asserts the 302 and the cookie together, so a
    breaking change in a future FastAPI-Users release surfaces as a red test
    rather than as a silently-insecure cookie."""

    async def get_login_response(self, token: str) -> Response:
        response = RedirectResponse("/snipes", status_code=302)
        return self._set_login_cookie(response, token)


# A second backend over the SAME JWTStrategy and the SAME ah_auth cookie -- the
# only difference is the response shape above, so a session created by Google
# login and one created by /auth/login are byte-for-byte interchangeable and
# either can be read by resolve_user_from_request(). `name` only affects
# generated route names.
oauth_redirect_backend = AuthenticationBackend(
    name="cookie_redirect",
    transport=RedirectCookieTransport(cookie_name=cookie_transport.cookie_name,
                                      cookie_max_age=COOKIE_MAX_AGE_SECONDS,
                                      cookie_secure=COOKIE_SECURE),
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

current_active_user = fastapi_users.current_user(active=True)

# The "soft gate" (human decision, 2026-08-06). Requires a confirmed email
# address on top of being logged in, and is applied to exactly three write
# routes -- billing's /checkout, forum's create_post, and the discord_webhook_url
# setter -- never to the read paths (/api/me, /api/snipes, /api/status,
# /api/realms).
#
# Why not the hard gate originally scoped on 2026-07-26 (block every
# current_active_user route): the anonymous tier shipped 2026-08-03, so a
# visitor with no account at all can already run realm-locked /api/snipes
# queries. Blocking an unverified *account* from the same thing protects no
# resource -- it would only push that person back to anonymous browsing while
# making the product look broken. What's actually worth protecting is anything
# that takes money, publishes to other users, or points our sender at a URL.
#
# FastAPI-Users raises 403 for logged-in-but-unverified vs 401 for not logged
# in, so the frontend can tell the two apart -- same reasoning as
# current_subscribed_user's deliberate 402 below.
current_verified_user = fastapi_users.current_user(active=True, verified=True)


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
    `active` check is applied here, deliberately -- this is the
    `current_active_user` equivalent, not the `current_verified_user` one.

    That distinction became load-bearing on 2026-08-06, when
    `current_verified_user` was added. This helper's only callers are
    `dashboard.api_me()` and `dashboard.api_snipes()`, and those are
    precisely the routes the soft gate leaves open to an unverified
    account (and to no account at all) -- so "active only" is the correct
    and complete check *for these callers*, not an omission. Anything that
    does need verification must go through
    `Depends(current_verified_user)` instead of reaching for this
    function; it will never enforce that, by design. `superuser=True` is
    still used nowhere. Returns `None` (never raises) on any
    failure -- no cookie, invalid/expired token, unknown user, inactive
    user -- callers are responsible for turning that into a 401 themselves,
    matching how `current_active_user` only raises at the FastAPI
    dependency-resolution layer, not inside this helper.

    `db.sessionmaker()` uses `expire_on_commit=False`, so the returned
    `User`'s already-loaded scalar columns (id, email, is_active,
    is_verified, is_superuser, subscription_status, locked_sell_realm,
    nickname) stay safely readable after this function returns and the
    session/connection has closed -- nothing downstream needs a live
    session for what it does with `user`. `User.oauth_accounts` is safe for
    the same reason only because it's mapped `lazy="joined"` (see db.py);
    a lazy collection would raise MissingGreenlet out here."""
    token = request.cookies.get(cookie_transport.cookie_name)
    if token is None:
        return None
    async with db.sessionmaker()() as session:
        user_db = SQLAlchemyUserDatabase(session, User, OAuthAccount)
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
