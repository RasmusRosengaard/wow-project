"""Tests for the real FastAPI-Users register/login/logout flow (db.py +
auth.py + dashboard.py's route protection). Uses a throwaway per-test SQLite
database (not the dependency-override bypass test_dashboard.py uses for its
snipe_check-focused tests) so this suite gives genuine coverage of the auth
machinery itself: password hashing, cookie issuance, session validity."""
import asyncio
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from fastapi_users.router.oauth import CSRF_TOKEN_COOKIE_NAME, generate_state_token
from httpx_oauth.oauth2 import OAuth2Token
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth
import billing
import dashboard
import db
import mailer
from db import Base, OAuthAccount, User, get_async_session

EMAIL = "test@example.com"
PASSWORD = "testpassword123"


async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _activate_subscription(session_factory, email: str) -> None:
    """No billing flow exists in this test DB -- flip subscription_status
    directly, the same field billing.py's webhook handler would write."""
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.subscription_status = "active"
        await session.commit()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(db_url)
    asyncio.run(_create_tables(engine))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    dashboard.app.dependency_overrides[get_async_session] = override_get_async_session
    # db.sessionmaker() itself, not just the get_async_session dependency
    # (2026-08-01): this file exercises the *real* auth path deliberately
    # (no dependency_overrides[current_active_user] bypass -- see this
    # file's own module docstring), so /api/snipes' new
    # auth.resolve_user_from_request()/_enforce_realm_lock() calls, which
    # open their own sessions via db.sessionmaker() directly rather than a
    # FastAPI Depends(), need to land in this same throwaway SQLite DB too.
    # get_async_session() (db.py) already calls the same module-level
    # sessionmaker() internally, so this one patch covers both paths.
    monkeypatch.setattr(db, "sessionmaker", lambda: session_factory)
    try:
        with TestClient(dashboard.app) as c:
            c.session_factory = session_factory  # tests can reach into the DB directly
            yield c
    finally:
        dashboard.app.dependency_overrides.pop(get_async_session, None)
        asyncio.run(engine.dispose())


def register(client, email=EMAIL, password=PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password})


def login(client, email=EMAIL, password=PASSWORD):
    return client.post("/auth/login", data={"username": email, "password": password})


def test_register_creates_user(client):
    r = register(client)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == EMAIL
    assert body["is_active"] is True
    assert body["subscription_status"] is None
    assert "hashed_password" not in body  # never leak the hash


def test_register_duplicate_email_rejected(client):
    register(client)
    r = register(client)
    assert r.status_code == 400
    assert r.json()["detail"] == "REGISTER_USER_ALREADY_EXISTS"


def test_login_sets_cookie_and_grants_access(client):
    register(client)
    r = login(client)
    assert r.status_code == 204
    assert "ah_auth" in r.cookies

    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


def test_login_wrong_password_rejected(client):
    register(client)
    r = login(client, password="wrongpassword")
    assert r.status_code == 400
    assert r.json()["detail"] == "LOGIN_BAD_CREDENTIALS"


def test_unauthenticated_request_to_api_me_returns_anonymous_session(client):
    """/api/me no longer 401s with no cookie at all (changed 2026-08-03,
    letting anonymous visitors use /snipes) -- it mints an anonymous session
    (db.AnonSession) and returns 200 with an anonymous-shaped response
    instead."""
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["is_anonymous"] is True
    assert body["email"] is None
    assert body["locked_sell_realm"] is None
    assert "ah_anon" in r.cookies


def test_logout_revokes_access(client):
    register(client)
    login(client)
    assert client.get("/api/me").status_code == 200

    r = client.post("/auth/logout")
    assert r.status_code == 204
    # No longer 401 (changed 2026-08-03, see
    # test_unauthenticated_request_to_api_me_returns_anonymous_session) --
    # a logged-out request now falls through to an anonymous session
    # instead of erroring, same as any other request with no valid ah_auth
    # cookie. The real thing this test needs to prove is that the *account*
    # is genuinely logged out (no longer the real user), not that /api/me
    # errors.
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["is_anonymous"] is True
    assert me.json()["email"] is None


def test_unauthenticated_dashboard_routes_reach_anonymous_business_logic(client):
    """/api/snipes and /api/status no longer require login at all (changed
    2026-08-03, letting anonymous visitors use /snipes -- this inverts the
    premise this test used to assert, "these routes 401 with no cookie").
    /api/status never carried per-user data, so it's simply open now.
    /api/snipes goes down the new anonymous realm-lock path
    (dashboard._enforce_anon_realm_lock) instead of the User-based one, but
    reaches the exact same "400 for an uncollected realm" business logic a
    logged-in account would (see test_dashboard_api_routes_reachable_when_logged_in
    below) -- the security boundary these routes used to have (login
    required) is gone by design, not a regression."""
    UNCOLLECTED_REALM = 424242  # no snapshot ever collected -- guaranteed not to exist
    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 400
    assert client.get("/api/status", params={"sell": UNCOLLECTED_REALM}).status_code == 200


def test_dashboard_api_routes_reachable_when_logged_in(client):
    """A real account (logged in, not subscribed) reaches the same business
    logic an anonymous visitor does above -- login changes which cap/lock
    applies downstream (see test_dashboard.py's _snipe_cap tests and
    test_free_tier_locks_to_first_sell_realm below), not whether the route
    is reachable at all."""
    UNCOLLECTED_REALM = 424242
    register(client)
    login(client)
    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 400
    assert client.get("/api/status", params={"sell": UNCOLLECTED_REALM}).status_code == 200


def test_dashboard_api_routes_require_active_subscription(client):
    """An active subscription still reaches the same business logic (400 for
    an uncollected realm) the free tier does above -- what a real
    subscription actually changes is the row cap (dashboard._snipe_cap),
    not reachability."""
    UNCOLLECTED_REALM = 424242
    register(client)
    login(client)
    asyncio.run(_activate_subscription(client.session_factory, EMAIL))

    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 400
    assert client.get("/api/me").json()["subscription_status"] == "active"


async def _make_superuser(session_factory, email: str) -> None:
    """Founder/admin access -- no public API sets this, has to be flipped
    directly in the DB (see auth.has_active_subscription)."""
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.is_superuser = True
        await session.commit()


def test_superuser_bypasses_subscription_check(client):
    """A superuser with subscription_status still None must still pass the
    gate -- is_superuser is a founder/admin bypass, not a Stripe concept."""
    UNCOLLECTED_REALM = 424242
    register(client)
    login(client)
    asyncio.run(_make_superuser(client.session_factory, EMAIL))

    me = client.get("/api/me").json()
    assert me["is_superuser"] is True
    assert me["subscription_status"] is None  # confirms this isn't just testing an active sub

    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 400


def test_free_tier_locks_to_first_sell_realm(client):
    """Free tier (2026-07-25): locks to whichever sell realm /api/snipes is
    first queried with, to bound how many distinct expensive queries a
    non-paying account can generate. Real DB persistence, unlike
    test_dashboard.py's dependency-override tests -- this exercises the
    actual write via dashboard._enforce_realm_lock. Neither realm needs to
    be collected -- the lock check runs before the "is this realm
    collected" business logic, so an uncollected realm still returns 400
    (not 403) on a permitted request."""
    REALM_A, REALM_B = 111111, 222222
    register(client)
    login(client)

    r = client.get("/api/snipes", params={"sell": REALM_A})
    assert r.status_code == 400  # uncollected, but past the lock -- proves it was set
    assert client.get("/api/me").json()["locked_sell_realm"] == REALM_A

    r = client.get("/api/snipes", params={"sell": REALM_B})
    assert r.status_code == 403
    assert "locked" in r.json()["detail"].lower()

    # The locked realm itself is still always reachable.
    assert client.get("/api/snipes", params={"sell": REALM_A}).status_code == 400


def test_active_subscription_is_never_realm_locked(client):
    REALM_A, REALM_B = 111111, 222222
    register(client)
    login(client)
    asyncio.run(_activate_subscription(client.session_factory, EMAIL))

    assert client.get("/api/snipes", params={"sell": REALM_A}).status_code == 400
    assert client.get("/api/snipes", params={"sell": REALM_B}).status_code == 400
    assert client.get("/api/me").json()["locked_sell_realm"] is None


def test_superuser_is_never_realm_locked(client):
    REALM_A, REALM_B = 111111, 222222
    register(client)
    login(client)
    asyncio.run(_make_superuser(client.session_factory, EMAIL))

    assert client.get("/api/snipes", params={"sell": REALM_A}).status_code == 400
    assert client.get("/api/snipes", params={"sell": REALM_B}).status_code == 400


# auth.resolve_user_from_request() (2026-08-01) replaced /api/snipes'
# Depends(current_active_user) with a manually-resolved user -- these tests
# exercise the real path directly (not test_dashboard.py's bypass), the same
# way test_unauthenticated_dashboard_routes_reach_anonymous_business_logic
# above already covers "no cookie at all". This fills in the cases that test
# wasn't designed to cover: a cookie that doesn't decode to a real session,
# and an otherwise-valid session for a deactivated account. Both used to
# 401; since 2026-08-03 (letting anonymous visitors use /snipes) both now
# fall through to the anonymous path instead -- resolve_user_from_request()
# returning None no longer means "reject the request," it means "no real
# account, try anonymous."

def test_api_snipes_invalid_cookie_falls_back_to_anonymous(client):
    """A garbage/malformed cookie value must resolve to "no user", the same
    as no cookie at all -- not raise or 500. Mirrors
    JWTStrategy.read_token()'s own contract (returns None on any
    jwt.PyJWTError, never raises) that resolve_user_from_request() relies
    on rather than reimplementing its own try/except. Since 2026-08-03 this
    is the *correct*, intended behavior -- a stale/garbage cookie degrades
    gracefully to anonymous browsing rather than hard-failing."""
    client.cookies.set("ah_auth", "not-a-real-jwt")
    r = client.get("/api/snipes", params={"sell": 424242})
    assert r.status_code == 400  # uncollected realm, same as a fresh anonymous visitor
    assert "ah_anon" in r.cookies  # a real anonymous session was minted for this request


def test_api_snipes_inactive_user_falls_back_to_anonymous(client):
    """A valid, correctly-signed cookie for an account that's since been
    deactivated resolves to no user (proves resolve_user_from_request()
    actually checks is_active, mirroring
    fastapi_users.current_user(active=True)'s own behavior, rather than just
    trusting that the token decoded to *some* real user) -- and, since
    2026-08-03, falls through to the anonymous path instead of 401ing. The
    deactivated account loses its real-account tier/lock, but can still
    browse anonymously like anyone else with no cookie at all."""
    register(client)
    login(client)
    assert client.get("/api/me").status_code == 200  # sanity: really logged in first

    async def _deactivate(session_factory, email: str) -> None:
        async with session_factory() as session:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
            user.is_active = False
            await session.commit()
    asyncio.run(_deactivate(client.session_factory, EMAIL))

    assert client.get("/api/snipes", params={"sell": 424242}).status_code == 400


# Anonymous-visitor tests (2026-08-03) -- real cookie-flow coverage of
# db.AnonSession/auth.resolve_or_create_anon_session/
# dashboard._enforce_anon_realm_lock, mirroring the equivalent User-based
# tests above (test_free_tier_locks_to_first_sell_realm in particular) line
# for line, so the two policies are proven to behave identically.

def test_anonymous_visitor_can_reach_api_me(client):
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["is_anonymous"] is True
    assert body["email"] is None
    assert body["locked_sell_realm"] is None
    assert body["anon_cap"] == 250
    assert body["free_cap"] == 500
    assert "ah_anon" in r.cookies


def test_anonymous_visitor_locks_to_first_sell_realm(client):
    """Mirrors test_free_tier_locks_to_first_sell_realm exactly, but with no
    account at all -- proves the anonymous path enforces the identical
    realm-lock policy via dashboard._enforce_anon_realm_lock /
    _atomic_lock_first_realm, the same shared core the User-based lock uses."""
    REALM_A, REALM_B = 111111, 222222

    r = client.get("/api/snipes", params={"sell": REALM_A})
    assert r.status_code == 400  # uncollected, but past the lock -- proves it was set
    assert client.get("/api/me").json()["locked_sell_realm"] == REALM_A

    r = client.get("/api/snipes", params={"sell": REALM_B})
    assert r.status_code == 403
    assert "locked" in r.json()["detail"].lower()

    # The locked realm itself is still always reachable.
    assert client.get("/api/snipes", params={"sell": REALM_A}).status_code == 400


def test_anonymous_session_persists_across_requests_via_cookie(client):
    """A visitor's ah_anon token is reused across requests (not reissued
    every time once it's already valid), and correctly hits the same locked
    row on a later request."""
    first = client.get("/api/me")
    assert "ah_anon" in first.cookies

    second = client.get("/api/me")
    assert "ah_anon" not in second.cookies  # not reissued -- the existing token was already valid

    client.get("/api/snipes", params={"sell": 111111})
    assert client.get("/api/me").json()["locked_sell_realm"] == 111111




# --- Email verification, password reset, Google OAuth (2026-08-06) ----------
#
# Every test above still passes untouched, and that is the point of the "soft
# gate" (human decision): a freshly-registered, unverified account still reaches
# /api/snipes, /api/status and /api/me exactly as before. Only three write
# routes require a confirmed address. The originally-scoped hard gate would have
# meant rewriting most of this file.


@pytest.fixture
def sent_mail(monkeypatch):
    """Captures every email instead of sending one. Returns the list of
    (to, subject, html) tuples, which grows as the test runs.

    Patched even though mailer.send() is already a no-op without RESEND_API_KEY
    (CI sets none): a developer with a real key in .env would otherwise send
    live email from a test run, and the tests need the token out of the body
    anyway."""
    captured = []

    async def fake_send(to, subject, html):
        captured.append((to, subject, html))
        return True

    monkeypatch.setattr(mailer, "send", fake_send)
    return captured


def token_from(html: str) -> str:
    """Pulls the token out of a link in a captured email body. Deliberately
    reads the real rendered HTML rather than intercepting the token earlier --
    a link that carries a malformed or truncated token is exactly the kind of
    break this should catch."""
    match = re.search(r"[?&]token=([^\"&<\s]+)", html)
    assert match, f"no token= link found in email body: {html[:400]}"
    return match.group(1)


async def _is_verified(session_factory, email: str) -> bool:
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        return user.is_verified


async def _verify_user(session_factory, email: str) -> None:
    """Marks an account verified directly, the way the grandfather migration
    did for every pre-existing account -- for tests about what a verified
    account can do, rather than about the verification flow itself."""
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.is_verified = True
        await session.commit()


def test_registration_sends_verification_email(client, sent_mail):
    r = register(client)
    assert r.status_code == 201
    # New accounts start unverified -- the whole premise of the gate.
    assert r.json()["is_verified"] is False
    assert asyncio.run(_is_verified(client.session_factory, EMAIL)) is False

    assert len(sent_mail) == 1
    to, subject, html = sent_mail[0]
    assert to == EMAIL
    assert "confirm" in subject.lower()
    assert "/verify?token=" in html


def test_verify_token_marks_account_verified(client, sent_mail):
    register(client)
    token = token_from(sent_mail[0][2])

    r = client.post("/auth/verify", json={"token": token})
    assert r.status_code == 200
    assert r.json()["is_verified"] is True
    assert asyncio.run(_is_verified(client.session_factory, EMAIL)) is True


def test_verify_token_is_single_use(client, sent_mail):
    """Replaying a consumed token reports ALREADY_VERIFIED rather than
    succeeding again -- verify.html shows its own panel for this, so the
    distinction is load-bearing, not cosmetic."""
    register(client)
    token = token_from(sent_mail[0][2])
    assert client.post("/auth/verify", json={"token": token}).status_code == 200

    r = client.post("/auth/verify", json={"token": token})
    assert r.status_code == 400
    assert r.json()["detail"] == "VERIFY_USER_ALREADY_VERIFIED"


def test_verify_rejects_garbage_token(client, sent_mail):
    register(client)
    r = client.post("/auth/verify", json={"token": "not-a-real-token"})
    assert r.status_code == 400
    assert r.json()["detail"] == "VERIFY_USER_BAD_TOKEN"
    assert asyncio.run(_is_verified(client.session_factory, EMAIL)) is False


def test_request_verify_token_resends_without_leaking_account_existence(client, sent_mail):
    """The resend affordance (verify.html, register.html, the dashboard banner)
    always gets 202, whether or not the address has an account -- otherwise the
    endpoint would be a way to enumerate registered emails. So the response is
    identical; only whether a mail was actually produced differs."""
    register(client)
    sent_mail.clear()

    r = client.post("/auth/request-verify-token", json={"email": EMAIL})
    assert r.status_code == 202
    assert len(sent_mail) == 1

    r = client.post("/auth/request-verify-token", json={"email": "nobody@example.com"})
    assert r.status_code == 202  # same status...
    assert len(sent_mail) == 1   # ...but no mail: no such account


def test_api_me_reports_is_verified_for_both_tiers(client, sent_mail):
    """Both branches of /api/me carry the field -- the frontend banner logic
    reads it without first checking which shape it got."""
    anon = client.get("/api/me").json()
    assert anon["is_anonymous"] is True
    assert anon["is_verified"] is False

    register(client)
    login(client)
    assert client.get("/api/me").json()["is_verified"] is False

    asyncio.run(_verify_user(client.session_factory, EMAIL))
    assert client.get("/api/me").json()["is_verified"] is True


# --- The soft gate: three write routes require a confirmed address ----------


def _post_forum(client):
    """Minimal valid Snipe Board post. The image bytes don't need to be a real
    PNG -- forum.py keys off the declared content type, not the payload."""
    return client.post("/api/forum/posts",
                       files={"image": ("snipe.png", b"fake-png-bytes", "image/png")})


@pytest.fixture
def stripe_unconfigured(monkeypatch):
    """Guarantees no test in this file can reach the real Stripe API.

    Not hypothetical: writing these tests, an assertion against
    /billing/checkout returned 200 locally, because billing.py reads
    STRIPE_SECRET_KEY at import time and a dev .env holds the **live** key --
    the test had created a real Checkout Session over the network. CI has no
    .env and would have 500'd, so the suite would have been red there and
    quietly hitting live Stripe here.

    Clearing PRICE_ID trips create_checkout_session's own "Stripe is not
    configured" guard, which is the first thing it does after the auth
    dependency resolves -- so a 500 here still proves the request got *past*
    current_verified_user, which is the only thing these tests care about.
    tests/test_billing.py takes the opposite approach for the same reason
    (patch in a fake sk_test_ key and mock Session.create) because it needs the
    call to succeed."""
    monkeypatch.setattr(billing, "PRICE_ID", None)
    monkeypatch.setattr(billing.stripe, "api_key", None)


def test_unverified_account_cannot_start_checkout(client, sent_mail, stripe_unconfigured):
    register(client)
    login(client)
    r = client.post("/billing/checkout")
    # 403, specifically -- NOT 401. subscribe.html branches on exactly this to
    # say "confirm your email" instead of bouncing an already-logged-in user
    # to /login. Also note this is reached before the Stripe-configured check,
    # so an unverified account never touches Stripe at all.
    assert r.status_code == 403


def test_unverified_account_cannot_post_to_snipe_board(client, sent_mail):
    register(client)
    login(client)
    asyncio.run(_set_nickname(client.session_factory, EMAIL, "Snipehunter"))
    assert _post_forum(client).status_code == 403


def test_verified_account_passes_the_soft_gate(client, sent_mail, stripe_unconfigured):
    """The same two requests as the two tests above, after verifying -- so a 403
    there proves the gate, and a non-403 here proves the gate is the only thing
    that was blocking them."""
    register(client)
    login(client)
    token = token_from(sent_mail[0][2])
    assert client.post("/auth/verify", json={"token": token}).status_code == 200
    asyncio.run(_set_nickname(client.session_factory, EMAIL, "Snipehunter"))

    assert _post_forum(client).status_code == 200
    # 500, not 200: the stripe_unconfigured fixture deliberately leaves Stripe
    # unconfigured, so the route gets past the auth dependency and then trips
    # its own "Stripe is not configured" guard. That 500 is the proof the gate
    # opened -- an unverified account gets 403 and never reaches that line.
    # Checkout actually succeeding is test_billing.py's job.
    assert client.post("/billing/checkout").status_code == 500


def test_soft_gate_leaves_the_read_paths_open(client, sent_mail):
    """The whole point of the soft gate (human decision, 2026-08-06): an
    unverified account keeps full free-tier access to the product itself.
    Anything less would just push the user back to anonymous browsing, which
    since 2026-08-03 reaches the same data anyway."""
    UNCOLLECTED_REALM = 424242
    register(client)
    login(client)
    assert asyncio.run(_is_verified(client.session_factory, EMAIL)) is False

    assert client.get("/api/me").status_code == 200
    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 400
    assert client.get("/api/status", params={"sell": UNCOLLECTED_REALM}).status_code == 200
    assert client.get("/api/realms").status_code == 200


async def _set_nickname(session_factory, email: str, nickname: str) -> None:
    """forum.create_post() requires a nickname before a first post, checked
    before the verification gate matters -- set it directly so these tests are
    about the gate and nothing else."""
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.nickname = nickname
        await session.commit()


# --- Password reset ---------------------------------------------------------


def test_forgot_password_flow_replaces_the_password(client, sent_mail):
    NEW_PASSWORD = "a-brand-new-password-456"
    register(client)
    sent_mail.clear()

    r = client.post("/auth/forgot-password", json={"email": EMAIL})
    assert r.status_code == 202
    assert len(sent_mail) == 1
    to, subject, html = sent_mail[0]
    assert to == EMAIL
    assert "reset" in subject.lower()
    assert "/reset-password?token=" in html

    r = client.post("/auth/reset-password",
                    json={"token": token_from(html), "password": NEW_PASSWORD})
    assert r.status_code == 200

    # Both directions matter: the new password must work AND the old one must
    # not. Asserting only the first would pass even if reset did nothing.
    assert login(client, password=NEW_PASSWORD).status_code == 204
    assert login(client, password=PASSWORD).status_code == 400


def test_forgot_password_does_not_leak_account_existence(client, sent_mail):
    r = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert r.status_code == 202  # identical to the real-account response
    assert sent_mail == []      # but nothing was sent


def test_reset_password_rejects_garbage_token(client, sent_mail):
    register(client)
    r = client.post("/auth/reset-password",
                    json={"token": "not-a-real-token", "password": "irrelevant-123"})
    assert r.status_code == 400
    assert r.json()["detail"] == "RESET_PASSWORD_BAD_TOKEN"
    # The original password still works -- a rejected reset changed nothing.
    assert login(client).status_code == 204


# --- Google OAuth login -----------------------------------------------------
#
# The routes only exist because conftest.py sets GOOGLE_OAUTH_CLIENT_ID/SECRET
# before auth.py is imported (dashboard.py mounts them conditionally). No test
# below ever reaches Google: /authorize builds a URL locally, and the callback
# tests replace the two client methods that would make network calls.

GOOGLE_ACCOUNT_ID = "people/1234567890"


@pytest.fixture
def fake_google(monkeypatch):
    """Stands in for Google's token exchange and profile lookup.

    Patches the two methods on the live auth.google_oauth_client instance --
    the same object the router captured at import time, so the patch is seen by
    the mounted route. get_id_email's return shape mirrors httpx-oauth's real
    GoogleOAuth2, which resolves identity through the **People API**
    (people/me?personFields=emailAddresses) and therefore yields a
    "people/<id>" resource name rather than an OIDC `sub` -- worth mirroring
    exactly, since that string is what lands in oauth_account.account_id."""
    state = {"email": EMAIL, "account_id": GOOGLE_ACCOUNT_ID}

    async def fake_get_access_token(code, redirect_uri, code_verifier=None):
        return OAuth2Token({"access_token": "fake-google-access-token",
                            "expires_at": 9999999999})

    async def fake_get_id_email(token):
        return state["account_id"], state["email"]

    monkeypatch.setattr(auth.google_oauth_client, "get_access_token", fake_get_access_token)
    monkeypatch.setattr(auth.google_oauth_client, "get_id_email", fake_get_id_email)
    return state


def google_callback(client):
    """Drives /auth/google/callback with a self-consistent state+CSRF pair.

    Both halves are required: the router compares the csrftoken inside the
    signed state JWT against the fastapiusersoauthcsrf cookie
    (secrets.compare_digest) and 400s on a mismatch. Forging them here is what
    makes it possible to test the callback without a browser round trip through
    Google.

    follow_redirects=False so the 302 itself is assertable -- following it would
    turn the response into whatever /snipes returns and hide the very thing
    these tests are about."""
    csrf = "test-csrf-token-value"
    state = generate_state_token({"csrftoken": csrf}, auth.SECRET)
    client.cookies.set(CSRF_TOKEN_COOKIE_NAME, csrf)
    try:
        return client.get("/auth/google/callback",
                          params={"code": "fake-auth-code", "state": state},
                          follow_redirects=False)
    finally:
        client.cookies.delete(CSRF_TOKEN_COOKIE_NAME)


async def _oauth_accounts(session_factory):
    async with session_factory() as session:
        return (await session.execute(select(OAuthAccount))).scalars().all()


async def _count_users(session_factory) -> int:
    async with session_factory() as session:
        return len((await session.execute(select(User))).scalars().all())


def test_google_authorize_returns_a_google_url_and_sets_csrf_cookie(client):
    r = client.get("/auth/google/authorize")
    assert r.status_code == 200
    url = r.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert auth.GOOGLE_OAUTH_CLIENT_ID in url
    # The redirect_uri must be the explicitly configured one, not something
    # derived from the request -- Google matches it byte-for-byte, and a scheme
    # guessed from proxy headers behind Railway's HTTPS terminator would break
    # the whole flow. conftest.py pins PUBLIC_BASE_URL so this is exact.
    assert quote(f"{auth.public_base_url()}{auth.GOOGLE_CALLBACK_PATH}", safe="") in url
    # Without this cookie the callback cannot validate state at all.
    assert CSRF_TOKEN_COOKIE_NAME in r.cookies


def test_google_callback_creates_verified_account_and_redirects(client, fake_google):
    r = google_callback(client)

    # 302 to /snipes, NOT the bare 204 plain CookieTransport would return --
    # the callback is a real browser navigation from Google, so a 204 would
    # leave the user on a blank page (see auth.RedirectCookieTransport).
    assert r.status_code == 302
    assert r.headers["location"] == "/snipes"
    assert "ah_auth" in r.cookies

    # The cookie is a working session for the same app, interchangeable with one
    # from /auth/login -- both backends share the JWT strategy and cookie name.
    me = client.get("/api/me").json()
    assert me["is_anonymous"] is False
    assert me["email"] == EMAIL
    # Verified with no confirmation email: Google already proved the address
    # (is_verified_by_default=True), so demanding our own would be friction.
    assert me["is_verified"] is True


def test_google_signup_sends_no_verification_email(client, fake_google, sent_mail):
    """UserManager.on_after_register early-returns for an already-verified user.
    Without that guard, a Google signup would be emailed a "confirm your
    address" link it has no reason to click."""
    assert google_callback(client).status_code == 302
    assert sent_mail == []


def test_google_login_links_to_existing_password_account(client, fake_google, sent_mail):
    """associate_by_email=True (human decision, 2026-08-06): the same address
    must end up as ONE account with two ways in, not a duplicate. This is the
    case where getting it wrong silently splits a paying customer's account in
    two, so it is asserted on the row counts, not just the response."""
    register(client)
    login(client)
    asyncio.run(_activate_subscription(client.session_factory, EMAIL))
    client.post("/auth/logout")
    assert asyncio.run(_count_users(client.session_factory)) == 1

    assert google_callback(client).status_code == 302

    # Still one account -- linked, not duplicated.
    assert asyncio.run(_count_users(client.session_factory)) == 1
    accounts = asyncio.run(_oauth_accounts(client.session_factory))
    assert len(accounts) == 1
    assert accounts[0].oauth_name == "google"
    assert accounts[0].account_id == GOOGLE_ACCOUNT_ID

    # And it is the *same* account: the subscription set up before the Google
    # login is still there afterwards.
    me = client.get("/api/me").json()
    assert me["email"] == EMAIL
    assert me["subscription_status"] == "active"
    # Linking also verified it -- Google vouched for the address the existing
    # password account had never confirmed.
    assert me["is_verified"] is True


def test_google_login_twice_reuses_the_same_link(client, fake_google):
    """A returning Google user must not accumulate a new oauth_account row per
    login."""
    assert google_callback(client).status_code == 302
    client.post("/auth/logout")
    assert google_callback(client).status_code == 302

    assert asyncio.run(_count_users(client.session_factory)) == 1
    assert len(asyncio.run(_oauth_accounts(client.session_factory))) == 1


def test_google_callback_rejects_mismatched_csrf_token(client, fake_google):
    """The state JWT and the cookie must agree. Without this check a forged
    callback could log a victim into an attacker's Google account."""
    state = generate_state_token({"csrftoken": "the-real-token"}, auth.SECRET)
    client.cookies.set(CSRF_TOKEN_COOKIE_NAME, "a-different-token")
    r = client.get("/auth/google/callback",
                   params={"code": "fake-auth-code", "state": state},
                   follow_redirects=False)
    client.cookies.delete(CSRF_TOKEN_COOKIE_NAME)
    assert r.status_code == 400
    assert asyncio.run(_count_users(client.session_factory)) == 0


def test_google_callback_rejects_unsigned_state(client, fake_google):
    """A state value we did not sign is refused -- proves the JWT signature is
    actually checked and not merely parsed."""
    client.cookies.set(CSRF_TOKEN_COOKIE_NAME, "whatever")
    r = client.get("/auth/google/callback",
                   params={"code": "fake-auth-code", "state": "not-a-jwt"},
                   follow_redirects=False)
    client.cookies.delete(CSRF_TOKEN_COOKIE_NAME)
    assert r.status_code == 400
    assert asyncio.run(_count_users(client.session_factory)) == 0


def test_auth_config_reports_what_is_configured(client, monkeypatch):
    """Drives login.html/register.html hiding the Google button on a deployment
    with no credentials. Read live from auth/mailer, not baked in at import,
    which is what makes this monkeypatchable at all."""
    r = client.get("/api/auth-config").json()
    assert r["google"] is True  # conftest.py configures it
    assert r["email"] is False  # no RESEND_API_KEY in tests

    monkeypatch.setattr(auth, "google_oauth_client", None)
    assert client.get("/api/auth-config").json()["google"] is False
