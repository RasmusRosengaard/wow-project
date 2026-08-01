"""Tests for the real FastAPI-Users register/login/logout flow (db.py +
auth.py + dashboard.py's route protection). Uses a throwaway per-test SQLite
database (not the dependency-override bypass test_dashboard.py uses for its
snipe_check-focused tests) so this suite gives genuine coverage of the auth
machinery itself: password hashing, cookie issuance, session validity."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import dashboard
import db
from db import Base, User, get_async_session

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


def test_unauthenticated_request_returns_401(client):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_logout_revokes_access(client):
    register(client)
    login(client)
    assert client.get("/api/me").status_code == 200

    r = client.post("/auth/logout")
    assert r.status_code == 204
    assert client.get("/api/me").status_code == 401


def test_dashboard_api_routes_require_login(client):
    """/api/snipes and /api/status are gated the same way as /api/me --
    the security boundary is server-side, not just the frontend redirect.
    Login alone (current_active_user) is the actual gate now -- a free tier
    (2026-07-25 human decision) means logged-in-but-unsubscribed reaches the
    same business logic a subscriber does, just capped on row count
    downstream (see test_dashboard.py's _snipe_cap tests), not turned away
    with a 402 like before."""
    UNCOLLECTED_REALM = 424242  # no snapshot ever collected -- guaranteed not to exist
    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 401
    assert client.get("/api/status", params={"sell": UNCOLLECTED_REALM}).status_code == 401

    register(client)
    login(client)
    # Logged in but not subscribed -- reaches real business logic now (400
    # for an uncollected realm), same as a subscriber would, proving login
    # alone is sufficient to pass the gate.
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
# way test_dashboard_api_routes_require_login above already covers "no
# cookie -> 401" and "valid cookie -> real business logic reached". This
# fills in the cases that test wasn't designed to cover: a cookie that
# doesn't decode to a real session, and an otherwise-valid session for a
# deactivated account.

def test_api_snipes_invalid_cookie_returns_401(client):
    """A garbage/malformed cookie value must resolve to "no user", the same
    as no cookie at all -- not raise or 500. Mirrors
    JWTStrategy.read_token()'s own contract (returns None on any
    jwt.PyJWTError, never raises) that resolve_user_from_request() relies
    on rather than reimplementing its own try/except."""
    client.cookies.set("ah_auth", "not-a-real-jwt")
    assert client.get("/api/snipes", params={"sell": 424242}).status_code == 401


def test_api_snipes_inactive_user_returns_401(client):
    """A valid, correctly-signed cookie for an account that's since been
    deactivated must still be rejected -- proves resolve_user_from_request()
    actually checks is_active (mirroring
    fastapi_users.current_user(active=True)'s own behavior) rather than
    just trusting that the token decoded to *some* real user."""
    register(client)
    login(client)
    assert client.get("/api/me").status_code == 200  # sanity: really logged in first

    async def _deactivate(session_factory, email: str) -> None:
        async with session_factory() as session:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
            user.is_active = False
            await session.commit()
    asyncio.run(_deactivate(client.session_factory, EMAIL))

    assert client.get("/api/snipes", params={"sell": 424242}).status_code == 401


