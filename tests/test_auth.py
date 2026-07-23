"""Tests for the real FastAPI-Users register/login/logout flow (db.py +
auth.py + dashboard.py's route protection). Uses a throwaway per-test SQLite
database (not the dependency-override bypass test_dashboard.py uses for its
snipe_check-focused tests) so this suite gives genuine coverage of the auth
machinery itself: password hashing, cookie issuance, session validity."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import dashboard
from db import Base, get_async_session

EMAIL = "test@example.com"
PASSWORD = "testpassword123"


async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def client(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(db_url)
    asyncio.run(_create_tables(engine))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    dashboard.app.dependency_overrides[get_async_session] = override_get_async_session
    try:
        with TestClient(dashboard.app) as c:
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


def test_dashboard_api_routes_require_auth(client):
    """/api/snipes and /api/status are gated the same way as /api/me --
    the security boundary is server-side, not just the frontend redirect."""
    UNCOLLECTED_REALM = 424242  # no data/events file -- guaranteed not to exist
    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 401
    assert client.get("/api/status", params={"sell": UNCOLLECTED_REALM}).status_code == 401

    register(client)
    login(client)
    # 400 (no data collected for this realm), not 401 -- proves auth passed
    # and we reached the actual business logic, not just skipped the gate.
    assert client.get("/api/snipes", params={"sell": UNCOLLECTED_REALM}).status_code == 400
