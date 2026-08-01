"""Tests for wow_accounts.py: per-user WoW-account/realm registration.
Mirrors test_forum.py's dependency-override bypass style for the
business-logic tests, plus real-DB-backed atomicity tests for the account/
realm caps modeled directly on test_dashboard.py's
test_enforce_realm_lock_concurrent_requests_only_one_wins (see that test's
own docstring for why two sequential, independently-loaded sessions is
enough to prove the atomic statement -- not anything held in a Python
object -- is the real source of truth)."""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth
import dashboard
import wow_accounts
from db import Base, User, WowAccount, WowAccountRealm, get_async_session

client = TestClient(dashboard.app)

FAKE_USER = User(id=uuid.uuid4(), email="wow-accounts@example.com", hashed_password="x",
                 is_active=True, is_superuser=False, is_verified=True,
                 subscription_status="active")

NON_SUBSCRIBED_USER = User(id=uuid.uuid4(), email="free@example.com", hashed_password="x",
                           is_active=True, is_superuser=False, is_verified=True,
                           subscription_status=None)


@pytest.fixture(autouse=True)
def bypass_auth():
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
    dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: FAKE_USER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)
    dashboard.app.dependency_overrides.pop(auth.current_subscribed_user, None)


@pytest.fixture(autouse=True)
def bypass_get_async_session(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_wow_accounts.db'}"
    engine = create_async_engine(db_url)

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create_tables())

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    dashboard.app.dependency_overrides[get_async_session] = override_get_async_session
    yield session_factory
    dashboard.app.dependency_overrides.pop(get_async_session, None)
    asyncio.run(engine.dispose())


def test_list_accounts_empty_by_default():
    r = client.get("/api/wow-accounts")
    assert r.status_code == 200
    assert r.json() == {"accounts": []}


def test_create_account_success():
    r = client.post("/api/wow-accounts", json={"label": "  Main  "})
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "Main"  # stripped
    assert body["realms"] == []
    assert isinstance(body["id"], int)


def test_create_account_rejects_empty_label():
    r = client.post("/api/wow-accounts", json={"label": "   "})
    assert r.status_code == 400


def test_create_account_rejects_label_too_long():
    r = client.post("/api/wow-accounts", json={"label": "x" * 51})
    assert r.status_code == 400


def test_create_account_enforces_max_8_cap():
    for i in range(8):
        assert client.post("/api/wow-accounts", json={"label": f"Account {i}"}).status_code == 200
    r = client.post("/api/wow-accounts", json={"label": "One too many"})
    assert r.status_code == 400
    assert len(client.get("/api/wow-accounts").json()["accounts"]) == 8


def test_rename_account_success():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    r = client.patch(f"/api/wow-accounts/{account_id}", json={"label": "  Renamed  "})
    assert r.status_code == 200
    assert r.json()["label"] == "Renamed"


def test_rename_account_not_owned_returns_404():
    # wow_accounts.py's routes only ever declare Depends(current_subscribed_user)
    # (never current_active_user), so that's the override that actually
    # matters here -- swap it to a different (but still "subscribed", so the
    # 402 check itself isn't what's being exercised) user than the one that
    # created the account.
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    other_user = User(id=uuid.uuid4(), email="other@example.com", hashed_password="x",
                      is_active=True, is_superuser=False, is_verified=True,
                      subscription_status="active")
    dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: other_user
    try:
        r = client.patch(f"/api/wow-accounts/{account_id}", json={"label": "Hijacked"})
        assert r.status_code == 404
    finally:
        dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: FAKE_USER


def test_delete_account_also_deletes_its_realms():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    client.post(f"/api/wow-accounts/{account_id}/realms", json={"connected_realm_id": 1403})
    r = client.delete(f"/api/wow-accounts/{account_id}")
    assert r.status_code == 200
    assert client.get("/api/wow-accounts").json()["accounts"] == []


def test_add_realm_success():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    r = client.post(f"/api/wow-accounts/{account_id}/realms", json={"connected_realm_id": 1403})
    assert r.status_code == 200
    assert r.json()["realms"] == [1403]


def test_add_realm_duplicate_rejected():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    client.post(f"/api/wow-accounts/{account_id}/realms", json={"connected_realm_id": 1403})
    r = client.post(f"/api/wow-accounts/{account_id}/realms", json={"connected_realm_id": 1403})
    assert r.status_code == 400
    assert client.get("/api/wow-accounts").json()["accounts"][0]["realms"] == [1403]


def test_add_realm_enforces_max_50_cap():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    for realm_id in range(1, 51):
        assert client.post(f"/api/wow-accounts/{account_id}/realms",
                           json={"connected_realm_id": realm_id}).status_code == 200
    r = client.post(f"/api/wow-accounts/{account_id}/realms", json={"connected_realm_id": 999})
    assert r.status_code == 400
    assert len(client.get("/api/wow-accounts").json()["accounts"][0]["realms"]) == 50


def test_remove_realm_success():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    client.post(f"/api/wow-accounts/{account_id}/realms", json={"connected_realm_id": 1403})
    r = client.delete(f"/api/wow-accounts/{account_id}/realms/1403")
    assert r.status_code == 200
    assert r.json()["realms"] == []


def test_remove_realm_not_found_returns_404():
    account_id = client.post("/api/wow-accounts", json={"label": "Main"}).json()["id"]
    r = client.delete(f"/api/wow-accounts/{account_id}/realms/1403")
    assert r.status_code == 404


def test_wow_accounts_requires_active_subscription():
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: NON_SUBSCRIBED_USER
    dashboard.app.dependency_overrides.pop(auth.current_subscribed_user, None)  # let the real check run
    try:
        r = client.get("/api/wow-accounts")
        assert r.status_code == 402
    finally:
        dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
        dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: FAKE_USER


def test_wow_accounts_requires_login():
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)
    dashboard.app.dependency_overrides.pop(auth.current_subscribed_user, None)
    try:
        r = client.get("/api/wow-accounts")
        assert r.status_code == 401
    finally:
        dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
        dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: FAKE_USER


async def _wow_account_test_user_db(tmp_path, db_name="wow_accounts_atomic.db"):
    """A real SQLite-backed User row for exercising _insert_account_atomic/
    _insert_realm_atomic's actual DB-level atomicity directly -- FAKE_USER
    (used by every HTTP-level test above) is never persisted to a real row.
    Returns (engine, session_factory, user_id); caller disposes the engine."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / db_name}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(email="atomic@example.com", hashed_password="x", is_active=True,
                   is_superuser=False, is_verified=True, subscription_status="active")
        session.add(user)
        await session.commit()
        user_id = user.id
    return engine, session_factory, user_id


def test_create_account_concurrent_requests_only_eight_win(tmp_path):
    """Regression test for the atomic-insert cap: seeds 7 accounts, then
    exercises the 8th/9th insert via two independent sessions that both
    predate either commit -- if _insert_account_atomic's cap check were a
    separate Python-side SELECT COUNT(*) instead of one atomic statement,
    both could observe count == 7 and both would succeed, landing at 9."""
    async def run():
        engine, session_factory, user_id = await _wow_account_test_user_db(tmp_path)
        async with session_factory() as seed_session:
            for i in range(7):
                assert await wow_accounts._insert_account_atomic(user_id, f"Seed {i}", seed_session)
            await seed_session.commit()

        async with session_factory() as session_a, session_factory() as session_b:
            inserted_a = await wow_accounts._insert_account_atomic(user_id, "A", session_a)
            await session_a.commit()
            inserted_b = await wow_accounts._insert_account_atomic(user_id, "B", session_b)
            await session_b.commit()
            assert inserted_a is True   # 8th account -- fills the cap exactly
            assert inserted_b is False  # 9th -- correctly rejected

        async with session_factory() as check_session:
            count = (await check_session.execute(
                select(func.count()).select_from(WowAccount).where(WowAccount.owner_id == user_id)
            )).scalar_one()
            assert count == 8
        await engine.dispose()
    asyncio.run(run())


def test_add_realm_concurrent_duplicate_only_one_wins(tmp_path):
    """Regression test proving WowAccountRealm's UniqueConstraint (not a
    Python-side pre-check) is the real guard against a double-add race:
    two independent sessions both attempt to add the exact same realm to
    the same account; the loser must raise IntegrityError, and exactly one
    row survives."""
    async def run():
        engine, session_factory, user_id = await _wow_account_test_user_db(tmp_path)
        async with session_factory() as session:
            assert await wow_accounts._insert_account_atomic(user_id, "Main", session)
            await session.commit()
            account_id = (await session.execute(
                select(WowAccount.id).where(WowAccount.owner_id == user_id)
            )).scalar_one()

        async with session_factory() as session_a, session_factory() as session_b:
            assert await wow_accounts._insert_realm_atomic(account_id, 1403, session_a)
            await session_a.commit()
            with pytest.raises(Exception):  # sqlalchemy.exc.IntegrityError
                await wow_accounts._insert_realm_atomic(account_id, 1403, session_b)
                await session_b.commit()

        async with session_factory() as check_session:
            count = (await check_session.execute(
                select(func.count()).select_from(WowAccountRealm)
                .where(WowAccountRealm.wow_account_id == account_id, WowAccountRealm.connected_realm_id == 1403)
            )).scalar_one()
            assert count == 1
        await engine.dispose()
    asyncio.run(run())
