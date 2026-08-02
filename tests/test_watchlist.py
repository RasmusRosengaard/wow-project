"""Tests for watchlist.py: CRUD routes, TSM group import, and the
check_triggers() background function collect_all.py calls each cycle.

Mirrors test_wow_accounts.py's dependency-override + real-SQLite-engine
style. check_triggers() runs outside any FastAPI request (it's called
directly by collect_all.py), so its tests monkeypatch db.sessionmaker
itself, same precedent as test_auth.py/test_dashboard.py's direct-session
call sites.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import auth
import blizz
import dashboard
import db
import item_names
import watchlist
from db import Base, User, WatchlistItem, get_async_session
from scan_region import LISTING_SCHEMA
from tests.test_tsm_import import REAL_SAMPLE

client = TestClient(dashboard.app)

FAKE_USER = User(id=uuid.uuid4(), email="watchlist@example.com", hashed_password="x",
                 is_active=True, is_superuser=False, is_verified=True,
                 subscription_status="active", discord_webhook_url=None)


@pytest.fixture(autouse=True)
def bypass_auth():
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
    dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: FAKE_USER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)
    dashboard.app.dependency_overrides.pop(auth.current_subscribed_user, None)


@pytest.fixture(autouse=True)
def bypass_get_async_session(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_watchlist.db'}"
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
    # check_triggers() opens its own session via db.sessionmaker() directly
    # (it doesn't run inside a request at all) -- same precedent as
    # test_auth.py/test_dashboard.py's direct-session call sites.
    monkeypatch.setattr(db, "sessionmaker", lambda: session_factory)
    yield session_factory
    dashboard.app.dependency_overrides.pop(get_async_session, None)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def isolate_item_names_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(item_names, "CACHE_PATH", tmp_path / "item_names_test_cache.json")
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": f"Item {item_id}", "quality": "EPIC", "level": None,
                                          "inventory_type": None, "item_class": 4, "item_subclass": 2})
    monkeypatch.setattr(item_names.NameCache, "icon", lambda self, item_id, pet_species_id=None: None)


@pytest.fixture(autouse=True)
def stub_realm_lookup(monkeypatch):
    monkeypatch.setattr(blizz, "connected_realm_realms",
                        lambda cr_id: [{"name": f"Realm {cr_id}", "slug": f"realm-{cr_id}", "category": "English"}])


@pytest.fixture
def listings_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "DATA", tmp_path)
    d = tmp_path / "listings"
    d.mkdir(parents=True)
    return d


def listing_row(cr, item_id, buyout, auction_id, pet_species_id=None):
    return {
        "cr_id": cr, "fetched_ts": 1000, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": "", "pet_species_id": pet_species_id, "pet_quality_id": None,
        "pet_level": None, "buyout": buyout, "bid": None, "quantity": 1,
        "time_left": "VERY_LONG",
    }


# ---------------------------------------------------------------- CRUD ----

def test_list_watchlist_empty_by_default():
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["discord_webhook_url"] is None


def test_add_item_by_id():
    r = client.post("/api/watchlist", json={"item_id": 12345, "trigger_price_g": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == 12345
    assert body["trigger_price_g"] == 50
    assert body["pet_species_id"] is None


def test_add_item_without_trigger_price_is_allowed():
    r = client.post("/api/watchlist", json={"item_id": 111})
    assert r.status_code == 200
    assert r.json()["trigger_price_g"] is None


def test_add_item_rejects_invalid_item_id():
    r = client.post("/api/watchlist", json={"item_id": 0})
    assert r.status_code == 400


def test_add_item_rejects_negative_trigger_price():
    r = client.post("/api/watchlist", json={"item_id": 111, "trigger_price_g": -5})
    assert r.status_code == 400


def test_update_item_trigger_price():
    added = client.post("/api/watchlist", json={"item_id": 111, "trigger_price_g": 10}).json()
    r = client.patch(f"/api/watchlist/{added['id']}", json={"trigger_price_g": 25})
    assert r.status_code == 200
    assert r.json()["trigger_price_g"] == 25


def test_update_item_can_clear_trigger_price():
    added = client.post("/api/watchlist", json={"item_id": 111, "trigger_price_g": 10}).json()
    r = client.patch(f"/api/watchlist/{added['id']}", json={"trigger_price_g": None})
    assert r.status_code == 200
    assert r.json()["trigger_price_g"] is None


def test_update_missing_item_404s():
    r = client.patch("/api/watchlist/999999", json={"trigger_price_g": 5})
    assert r.status_code == 404


def test_delete_item():
    added = client.post("/api/watchlist", json={"item_id": 111}).json()
    r = client.delete(f"/api/watchlist/{added['id']}")
    assert r.status_code == 200
    assert client.get("/api/watchlist").json()["items"] == []


def test_add_item_enforces_cap(monkeypatch):
    monkeypatch.setattr(watchlist, "MAX_WATCHLIST_ITEMS_PER_USER", 2)
    assert client.post("/api/watchlist", json={"item_id": 1}).status_code == 200
    assert client.post("/api/watchlist", json={"item_id": 2}).status_code == 200
    r = client.post("/api/watchlist", json={"item_id": 3})
    assert r.status_code == 400


# ---------------------------------------------------------- webhook URL ----

def test_update_discord_webhook_accepts_valid_url():
    url = "https://discord.com/api/webhooks/123/abc"
    r = client.patch("/api/watchlist/discord-webhook", json={"discord_webhook_url": url})
    assert r.status_code == 200
    assert r.json()["discord_webhook_url"] == url


def test_update_discord_webhook_rejects_non_discord_url():
    r = client.patch("/api/watchlist/discord-webhook", json={"discord_webhook_url": "https://evil.example.com/x"})
    assert r.status_code == 400


def test_update_discord_webhook_can_be_cleared():
    client.patch("/api/watchlist/discord-webhook", json={"discord_webhook_url": "https://discord.com/api/webhooks/1/a"})
    r = client.patch("/api/watchlist/discord-webhook", json={"discord_webhook_url": None})
    assert r.status_code == 200
    assert r.json()["discord_webhook_url"] is None


# ------------------------------------------------------------ TSM import ----

def test_import_tsm_group_real_sample():
    """REAL_SAMPLE is the actual user-provided TSM export string (see
    tests/test_tsm_import.py) -- a real 300-item group, not synthetic."""
    r = client.post("/api/watchlist/import-tsm", json={"export": REAL_SAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["group_name"] == "Player Housing - Decor || Craft"
    assert body["total_in_export"] == 300
    assert body["imported"] == 300
    assert body["skipped_existing"] == 0

    listed = client.get("/api/watchlist").json()["items"]
    assert len(listed) == 300
    assert all(item["trigger_price_g"] is None for item in listed)  # no price data in a TSM export
    assert all(item["label"] for item in listed)  # group path carried through as label


def test_import_tsm_group_twice_skips_duplicates():
    client.post("/api/watchlist/import-tsm", json={"export": REAL_SAMPLE})
    r = client.post("/api/watchlist/import-tsm", json={"export": REAL_SAMPLE})
    assert r.status_code == 200
    assert r.json()["imported"] == 0
    assert r.json()["skipped_existing"] == 300


def test_import_tsm_group_rejects_garbage():
    r = client.post("/api/watchlist/import-tsm", json={"export": "not a real export"})
    assert r.status_code == 400


def test_import_tsm_group_respects_cap(monkeypatch):
    monkeypatch.setattr(watchlist, "MAX_WATCHLIST_ITEMS_PER_USER", 10)
    r = client.post("/api/watchlist/import-tsm", json={"export": REAL_SAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 10
    assert body["skipped_cap"] == 290


# ------------------------------------------------------------- ownership ----

def test_cannot_touch_another_users_item():
    other_user_id = uuid.uuid4()
    # add via the real user, then swap the authenticated user and confirm
    # the row is invisible to them (404, not leaked)
    added = client.post("/api/watchlist", json={"item_id": 111}).json()
    other_user = User(id=other_user_id, email="other@example.com", hashed_password="x",
                      is_active=True, is_superuser=False, is_verified=True,
                      subscription_status="active")
    dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: other_user
    r = client.delete(f"/api/watchlist/{added['id']}")
    assert r.status_code == 404


# -------------------------------------------------------- check_triggers ----
#
# check_triggers() does a real WatchlistItem-JOIN-User query in its own,
# separate DB session (see watchlist._check_triggers_async) -- unlike the
# HTTP-layer CRUD tests above, this needs an *actually persisted* User row,
# not the bare in-memory FAKE_USER object bypass_auth injects via
# dependency_overrides (that object was never inserted into the test DB at
# all, and mutating its attributes through a request doesn't reliably
# persist across a *different* session either -- same caveat
# test_dashboard.py's own _setup_free_tier_db fixture documents for exactly
# this reason). So these tests build a real, committed User row directly
# and point current_subscribed_user at it instead of FAKE_USER.

async def _make_real_user(session_factory, discord_webhook_url=None):
    async with session_factory() as session:
        user = User(email=f"real-{uuid.uuid4()}@example.com", hashed_password="x",
                   is_active=True, is_superuser=False, is_verified=True,
                   subscription_status="active", discord_webhook_url=discord_webhook_url)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
def real_user(bypass_get_async_session):
    """Persists a real User row and points current_subscribed_user at it for
    the duration of the test. Call real_user(discord_webhook_url=...) to
    set the webhook up front (there's no reliable way to set it later via
    the HTTP PATCH route in this bypassed-session test setup -- see the
    section docstring above)."""
    session_factory = bypass_get_async_session

    def _factory(discord_webhook_url=None):
        user = asyncio.run(_make_real_user(session_factory, discord_webhook_url))
        dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: user
        return user
    return _factory


def test_check_triggers_notifies_when_price_clears_trigger(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],  # 5g, under the 10g trigger
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    result = watchlist.check_triggers()
    assert result["watched"] == 1
    assert result["notified"] == 1
    assert len(posted) == 1
    assert "5.00g" in posted[0][1]["content"] or "5.0" in posted[0][1]["content"]


def test_check_triggers_does_not_notify_above_trigger(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 1})

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],  # 5g, above the 1g trigger
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    result = watchlist.check_triggers()
    assert result["notified"] == 0
    assert posted == []


def test_check_triggers_skips_items_with_no_trigger_set(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555})  # no trigger_price_g

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=1, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    result = watchlist.check_triggers()
    assert result["watched"] == 0
    assert result["notified"] == 0
    assert posted == []


def test_check_triggers_respects_cooldown(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    first = watchlist.check_triggers()
    assert first["notified"] == 1
    second = watchlist.check_triggers()
    assert second["notified"] == 0  # still under cooldown, same still-cheap listing
    assert len(posted) == 1


def test_check_triggers_renotifies_after_cooldown_expires(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    added = client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10}).json()

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))
    monkeypatch.setattr(watchlist, "NOTIFY_COOLDOWN_SECONDS", 0)

    watchlist.check_triggers()

    async def _age_last_notified():
        async with db.sessionmaker()() as session:
            item = (await session.execute(
                select(WatchlistItem).where(WatchlistItem.id == added["id"])
            )).scalar_one()
            item.last_notified_at = datetime.now(timezone.utc) - timedelta(days=1)
            await session.commit()
    asyncio.run(_age_last_notified())

    second = watchlist.check_triggers()
    assert second["notified"] == 1
    assert len(posted) == 2


def test_check_triggers_no_webhook_still_tracks_silently(listings_dir, real_user, monkeypatch):
    """No discord_webhook_url set -- item is still watched/matched but
    nothing is ever POSTed."""
    real_user(discord_webhook_url=None)
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    result = watchlist.check_triggers()
    assert result["notified"] == 1  # counted as "notified" (cooldown updated) even with no delivery
    assert posted == []
