"""Tests for admin.py: the superuser-only activity tracker and visitor
history (extracted from dashboard.py 2026-08-04, when the tracker gained a
persisted table and its own page).

Mirrors test_dashboard.py's fixture style -- throwaway per-test SQLite via
dependency_overrides + a db.sessionmaker monkeypatch, auth bypassed through
dependency_overrides -- rather than inventing a second convention.

The two layers are tested separately, because that separation is the design:
track_activity() must only touch memory, and flush_visitors() is what turns
memory into rows.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import admin
import auth
import dashboard
import db
from db import Base, User, VisitorIP, get_async_session

client = TestClient(dashboard.app)

PLAIN_USER = User(email="plain@example.com", hashed_password="x",
                  is_active=True, is_superuser=False, is_verified=True,
                  subscription_status="active")
SUPERUSER = User(email="root@example.com", hashed_password="x",
                 is_active=True, is_superuser=True, is_verified=True,
                 subscription_status="active")


@pytest.fixture(autouse=True)
def session_factory(tmp_path, monkeypatch):
    """Throwaway SQLite for one test, wired into both seams admin.py can
    reach the database through: Depends(get_async_session) for the routes,
    and db.sessionmaker() for _visitor_flush_loop().

    Autouse, matching test_dashboard.py's bypass_get_async_session: even the
    tests that only assert on the in-memory buffers get there by making a
    real request through the app, and FastAPI resolves every declared
    dependency of the route it hits (/api/me takes a session) regardless of
    whether this test's assertions care. Without it those tests trip
    tests/conftest.py's no-real-database guard."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test_admin.db'}")

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create_tables())

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_async_session():
        async with factory() as session:
            yield session

    dashboard.app.dependency_overrides[get_async_session] = override_get_async_session
    monkeypatch.setattr(db, "sessionmaker", lambda: factory)
    yield factory
    dashboard.app.dependency_overrides.pop(get_async_session, None)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def reset_buffers(monkeypatch):
    """Both module-level dicts are process-global; reset per test so one
    test's TestClient traffic can't leak into another's assertions."""
    monkeypatch.setattr(admin, "_recent_activity", {})
    monkeypatch.setattr(admin, "_pending_hits", {})


@pytest.fixture
def as_superuser():
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: SUPERUSER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)


@pytest.fixture
def as_plain_user():
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: PLAIN_USER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)


def _flush(factory):
    async def _run():
        async with factory() as session:
            return await admin.flush_visitors(session)
    return asyncio.run(_run())


def _rows(factory):
    async def _run():
        async with factory() as session:
            return (await session.execute(
                select(VisitorIP).order_by(VisitorIP.ip))).scalars().all()
    return asyncio.run(_run())


def _seed(factory, ip, *, last_seen, first_seen=None, hit_count=1):
    async def _run():
        async with factory() as session:
            session.add(VisitorIP(ip=ip, first_seen=first_seen or last_seen,
                                  last_seen=last_seen, hit_count=hit_count))
            await session.commit()
    asyncio.run(_run())


# --- the request path: memory only ------------------------------------


def test_track_activity_records_api_hits_by_ip():
    """Keyed by X-Forwarded-For's first entry (the real client, not
    Railway's internal proxy hop -- see _client_ip()'s own comment)."""
    client.get("/api/me", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})
    assert "203.0.113.5" in admin._recent_activity
    assert admin._pending_hits["203.0.113.5"] == 1


def test_track_activity_ignores_non_api_paths():
    client.get("/pricing", headers={"X-Forwarded-For": "203.0.113.9"})
    assert "203.0.113.9" not in admin._recent_activity
    assert "203.0.113.9" not in admin._pending_hits


def test_track_activity_accumulates_repeat_hits():
    for _ in range(3):
        client.get("/api/me", headers={"X-Forwarded-For": "203.0.113.7"})
    assert admin._pending_hits["203.0.113.7"] == 3


def test_track_activity_writes_nothing_to_the_database(session_factory):
    """The whole point of the two-layer design: a request must not touch
    Postgres. db.engine()'s comment documents a real outage from connection
    pressure, and the dashboard auto-refreshes."""
    client.get("/api/me", headers={"X-Forwarded-For": "203.0.113.11"})
    assert _rows(session_factory) == []


def test_client_ip_truncates_overlong_header():
    """X-Forwarded-For is attacker-controlled and VisitorIP.ip is a
    String(45) primary key -- an over-length value must be cut down here
    rather than blowing up the flush."""
    long_ip = "a" * 200

    class _Req:
        headers = {"x-forwarded-for": long_ip}
        client = None

    assert len(admin._client_ip(_Req())) == admin.MAX_IP_LEN


def test_activity_dict_is_pruned_past_the_threshold(monkeypatch):
    """The buffer must not grow unboundedly over a long-running process."""
    monkeypatch.setattr(admin, "_ACTIVITY_PRUNE_THRESHOLD", 3)
    stale = time.time() - admin.ACTIVE_WINDOW_SECONDS - 60
    monkeypatch.setattr(admin, "_recent_activity",
                        {f"198.51.100.{i}": stale for i in range(5)})
    client.get("/api/me", headers={"X-Forwarded-For": "203.0.113.42"})
    assert "198.51.100.0" not in admin._recent_activity
    assert "203.0.113.42" in admin._recent_activity


# --- the flush: memory -> rows ----------------------------------------


def test_flush_inserts_a_new_visitor(session_factory):
    admin._pending_hits.update({"203.0.113.5": 4})
    assert _flush(session_factory) == 1
    (row,) = _rows(session_factory)
    assert (row.ip, row.hit_count) == ("203.0.113.5", 4)
    assert row.first_seen is not None and row.last_seen is not None


def test_flush_accumulates_into_an_existing_visitor(session_factory):
    """hit_count is a lifetime total across flushes -- it must survive the
    redeploys that reset the in-memory dict, so the second flush adds rather
    than overwrites."""
    admin._pending_hits.update({"203.0.113.5": 4})
    _flush(session_factory)
    admin._pending_hits.update({"203.0.113.5": 3})
    _flush(session_factory)
    (row,) = _rows(session_factory)
    assert row.hit_count == 7


def test_flush_preserves_first_seen_but_advances_last_seen(session_factory):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    _seed(session_factory, "203.0.113.5", last_seen=old, hit_count=1)
    admin._pending_hits.update({"203.0.113.5": 1})
    _flush(session_factory)
    (row,) = _rows(session_factory)
    # SQLite returns naive datetimes; compare on the wire-independent parts.
    assert row.first_seen.replace(tzinfo=None) == old.replace(tzinfo=None)
    assert row.last_seen.replace(tzinfo=None) > old.replace(tzinfo=None)


def test_flush_drains_the_buffer(session_factory):
    admin._pending_hits.update({"203.0.113.5": 2})
    _flush(session_factory)
    assert admin._pending_hits == {}
    # A second flush with nothing pending must be a no-op, not a double-count.
    assert _flush(session_factory) == 0
    (row,) = _rows(session_factory)
    assert row.hit_count == 2


def test_flush_with_nothing_pending_writes_nothing(session_factory):
    assert _flush(session_factory) == 0
    assert _rows(session_factory) == []


# --- the endpoints ----------------------------------------------------


def test_active_users_requires_superuser(as_plain_user, session_factory):
    """A logged-in non-superuser gets 403 -- current_active_user already
    proved they're logged in, this is the stricter check on top."""
    assert client.get("/api/admin/active-users").status_code == 403


def test_visitors_requires_superuser(as_plain_user, session_factory):
    assert client.get("/api/admin/visitors").status_code == 403


def test_active_users_lists_a_recent_visitor(as_superuser, session_factory):
    _seed(session_factory, "203.0.113.5", last_seen=datetime.now(timezone.utc), hit_count=9)
    r = client.get("/api/admin/active-users")
    assert r.status_code == 200
    body = r.json()
    assert body["window_seconds"] == admin.ACTIVE_WINDOW_SECONDS
    entry = next(e for e in body["ips"] if e["ip"] == "203.0.113.5")
    assert entry["hit_count"] == 9
    assert entry["last_seen_seconds_ago"] < 60


def test_active_users_excludes_stale_visitors(as_superuser, session_factory):
    """An IP last seen outside ACTIVE_WINDOW_SECONDS is history, not
    "currently on the site"."""
    stale = datetime.now(timezone.utc) - timedelta(seconds=admin.ACTIVE_WINDOW_SECONDS + 60)
    _seed(session_factory, "203.0.113.99", last_seen=stale)
    body = client.get("/api/admin/active-users").json()
    assert not any(e["ip"] == "203.0.113.99" for e in body["ips"])
    assert body["count"] == 0


def test_active_users_survives_a_restart(as_superuser, session_factory):
    """Reading from the table rather than the in-memory dict is what makes
    the view outlive a redeploy -- the dict is empty here (reset_buffers)
    and the row must still show."""
    assert admin._recent_activity == {}
    _seed(session_factory, "203.0.113.5", last_seen=datetime.now(timezone.utc))
    assert client.get("/api/admin/active-users").json()["count"] == 1


def test_visitors_returns_full_history_newest_first(as_superuser, session_factory):
    now = datetime.now(timezone.utc)
    _seed(session_factory, "203.0.113.1", last_seen=now - timedelta(days=3))
    _seed(session_factory, "203.0.113.2", last_seen=now)
    body = client.get("/api/admin/visitors").json()
    assert [v["ip"] for v in body["visitors"]] == ["203.0.113.2", "203.0.113.1"]
    assert body["count"] == 2


def test_visitors_flags_which_entries_are_currently_active(as_superuser, session_factory):
    now = datetime.now(timezone.utc)
    _seed(session_factory, "203.0.113.1", last_seen=now)
    _seed(session_factory, "203.0.113.2",
          last_seen=now - timedelta(seconds=admin.ACTIVE_WINDOW_SECONDS + 60))
    by_ip = {v["ip"]: v for v in client.get("/api/admin/visitors").json()["visitors"]}
    assert by_ip["203.0.113.1"]["is_active"] is True
    assert by_ip["203.0.113.2"]["is_active"] is False


def test_visitors_is_capped(as_superuser, session_factory, monkeypatch):
    """The page reports truncation to the reader, so the cap has to be real
    and reported honestly in `limit`."""
    monkeypatch.setattr(admin, "VISITOR_HISTORY_LIMIT", 2)
    now = datetime.now(timezone.utc)
    for i in range(4):
        _seed(session_factory, f"203.0.113.{i}", last_seen=now - timedelta(minutes=i))
    body = client.get("/api/admin/visitors").json()
    assert body["count"] == 2 and body["limit"] == 2
    # Newest kept, oldest dropped.
    assert [v["ip"] for v in body["visitors"]] == ["203.0.113.0", "203.0.113.1"]


# --- signups -----------------------------------------------------------


def _seed_user(factory, email, *, created_at=None, nickname=None,
               subscription_status=None, is_superuser=False, is_verified=True,
               is_active=True):
    """created_at=None seeds a *legacy* row -- one that predates the column.

    It needs a follow-up UPDATE because SQLAlchemy applies a column default
    whenever the attribute is None at flush time, so an explicit
    `created_at=None` on the model still comes back stamped with now(). That
    is correct behaviour for real registrations (which must always get a
    date), and it means the only rows that can legitimately be NULL are the
    ones the migration added the column to -- exactly what this reproduces."""
    async def _run():
        async with factory() as session:
            session.add(User(email=email, hashed_password="x", created_at=created_at,
                             nickname=nickname, subscription_status=subscription_status,
                             is_superuser=is_superuser, is_verified=is_verified,
                             is_active=is_active))
            await session.commit()
            if created_at is None:
                await session.execute(
                    update(User).where(User.email == email).values(created_at=None))
                await session.commit()
    asyncio.run(_run())


def test_signups_requires_superuser(as_plain_user):
    assert client.get("/api/admin/signups").status_code == 403


def test_signups_lists_accounts_with_email(as_superuser, session_factory):
    """The literal ask: "new users actual signups also with mail"."""
    _seed_user(session_factory, "new@example.com",
               created_at=datetime.now(timezone.utc), nickname="Sniper")
    body = client.get("/api/admin/signups").json()
    entry = next(e for e in body["signups"] if e["email"] == "new@example.com")
    assert entry["nickname"] == "Sniper"
    assert entry["signed_up_seconds_ago"] < 60
    assert body["total"] == 1


def test_signups_orders_newest_first_with_undated_last(as_superuser, session_factory):
    """created_at DESC NULLS LAST -- Postgres defaults to NULLS FIRST on
    DESC, which would bury real recent signups under legacy accounts."""
    now = datetime.now(timezone.utc)
    _seed_user(session_factory, "old@example.com", created_at=now - timedelta(days=10))
    _seed_user(session_factory, "legacy@example.com", created_at=None)
    _seed_user(session_factory, "newest@example.com", created_at=now)
    emails = [e["email"] for e in client.get("/api/admin/signups").json()["signups"]]
    assert emails == ["newest@example.com", "old@example.com", "legacy@example.com"]


def test_signups_reports_missing_created_at_as_null(as_superuser, session_factory):
    """An account predating the column has no recoverable signup date; it
    must come back null rather than being invented as "now"."""
    _seed_user(session_factory, "legacy@example.com", created_at=None)
    (entry,) = client.get("/api/admin/signups").json()["signups"]
    assert entry["created_at"] is None
    assert entry["signed_up_seconds_ago"] is None


def test_signups_never_exposes_secrets(as_superuser, session_factory):
    """The User row carries hashed_password and Stripe ids -- the response is
    an explicit allowlist, so a future column can't leak by default."""
    _seed_user(session_factory, "new@example.com", created_at=datetime.now(timezone.utc))
    (entry,) = client.get("/api/admin/signups").json()["signups"]
    assert set(entry) == {"email", "nickname", "created_at", "signed_up_seconds_ago",
                          "subscription_status", "is_verified", "is_active", "is_superuser"}


def test_signups_surfaces_subscription_and_flags(as_superuser, session_factory):
    _seed_user(session_factory, "sub@example.com", created_at=datetime.now(timezone.utc),
               subscription_status="active", is_superuser=True, is_verified=False)
    (entry,) = client.get("/api/admin/signups").json()["signups"]
    assert entry["subscription_status"] == "active"
    assert entry["is_superuser"] is True and entry["is_verified"] is False


def test_signups_total_counts_beyond_the_page_limit(as_superuser, session_factory,
                                                    monkeypatch):
    """`total` must be the real account count, not the truncated page length
    -- the stat tile reads it."""
    monkeypatch.setattr(admin, "SIGNUP_LIST_LIMIT", 2)
    now = datetime.now(timezone.utc)
    for i in range(4):
        _seed_user(session_factory, f"u{i}@example.com", created_at=now - timedelta(hours=i))
    body = client.get("/api/admin/signups").json()
    assert body["count"] == 2 and body["total"] == 4


def test_new_user_gets_created_at_automatically(session_factory):
    """db.User.created_at's default must apply on insert -- fastapi-users'
    UserManager doesn't set it, so registration relies entirely on this."""
    async def _run():
        async with session_factory() as session:
            session.add(User(email="auto@example.com", hashed_password="x"))
            await session.commit()
            return (await session.execute(
                select(User).where(User.email == "auto@example.com"))).scalar_one()
    assert asyncio.run(_run()).created_at is not None


def test_admin_page_is_served():
    r = client.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
