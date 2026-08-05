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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import appearance
import auth
import blizz
import dashboard
import db
import item_names
import snipe_check
import tsm
import watchlist
from db import Base, User, WatchlistItem, WowAccount, WowAccountRealm, get_async_session
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
    # check_triggers() opens its own session via db.isolated_session()
    # directly (it doesn't run inside a request at all) -- same precedent
    # as test_auth.py/test_dashboard.py's direct-session call sites.
    # Reuses this fixture's own engine/session_factory rather than
    # db.isolated_session()'s real production behavior of creating and
    # disposing a brand-new engine per call -- that dance exists only to
    # dodge asyncpg's loop-binding, which SQLite/aiosqlite doesn't have.
    @asynccontextmanager
    async def _fake_isolated_session():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(db, "isolated_session", _fake_isolated_session)
    # Some tests also open a direct db.sessionmaker() session outside any
    # request (e.g. to mutate state between two check_triggers() calls) --
    # keep that seam patched too, same as before this function existed.
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
    # check_triggers() now also runs the standing rule scan, which reads the
    # TSM sale-rate and appearance caches. Point both at the tmp dir so no
    # test can read the developer's real data/ files: without this, an
    # unrelated test's assertion on how many Discord posts happened would
    # depend on whether a real TSM refresh had happened to add the fixture's
    # item id with a high enough sale average. Same hermeticity principle as
    # conftest.py's hard fail on any test reaching a real database engine.
    monkeypatch.setattr(tsm, "CACHE_PATH", tmp_path / "tsm_sale_rates.json")
    monkeypatch.setattr(appearance, "CACHE_PATH", tmp_path / "appearances.json")
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


def embed_of(payload):
    """The single embed from a webhook payload. Notifications moved from a
    run-on `content` string to a Discord embed 2026-08-05 (human request
    with a real screenshot) -- these helpers keep the assertions reading in
    terms of what a user actually sees rather than payload plumbing."""
    return payload["embeds"][0]


def embed_field(payload, name):
    for f in embed_of(payload)["fields"]:
        if f["name"] == name:
            return f["value"]
    return None


def embed_text(payload):
    """Every rendered string in the embed, joined -- for assertions that
    only care that a fact is present somewhere the reader can see it."""
    e = embed_of(payload)
    parts = [e.get("title", "")] + [f'{f["name"]} {f["value"]}' for f in e["fields"]]
    return " | ".join(parts)


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


def test_add_item_requires_trigger_price():
    r = client.post("/api/watchlist", json={"item_id": 111})
    assert r.status_code == 400


def test_add_item_rejects_zero_trigger_price():
    r = client.post("/api/watchlist", json={"item_id": 111, "trigger_price_g": 0})
    assert r.status_code == 400


def test_add_item_rejects_invalid_item_id():
    r = client.post("/api/watchlist", json={"item_id": 0, "trigger_price_g": 10})
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
    added = client.post("/api/watchlist", json={"item_id": 111, "trigger_price_g": 10}).json()
    r = client.delete(f"/api/watchlist/{added['id']}")
    assert r.status_code == 200
    assert client.get("/api/watchlist").json()["items"] == []


def test_add_item_enforces_cap(monkeypatch):
    monkeypatch.setattr(watchlist, "MAX_WATCHLIST_ITEMS_PER_USER", 2)
    assert client.post("/api/watchlist", json={"item_id": 1, "trigger_price_g": 10}).status_code == 200
    assert client.post("/api/watchlist", json={"item_id": 2, "trigger_price_g": 10}).status_code == 200
    r = client.post("/api/watchlist", json={"item_id": 3, "trigger_price_g": 10})
    assert r.status_code == 400


# --------------------------------------------------------- batch update ----
# add_item() requires a trigger price at creation (2026-08-02, human
# request), but the batch/single update endpoints still allow setting or
# clearing one later (e.g. a TSM-imported item, which starts with none) --
# these tests create with a placeholder initial price where the point of
# the test is the *update*, not the starting state.

def test_batch_update_sets_multiple_trigger_prices_in_one_request():
    a = client.post("/api/watchlist", json={"item_id": 1, "trigger_price_g": 1}).json()
    b = client.post("/api/watchlist", json={"item_id": 2, "trigger_price_g": 1}).json()
    r = client.patch("/api/watchlist/batch", json={"items": [
        {"id": a["id"], "trigger_price_g": 10},
        {"id": b["id"], "trigger_price_g": 25.5},
    ]})
    assert r.status_code == 200
    assert r.json() == {"updated": 2, "skipped": 0}
    items = {i["id"]: i["trigger_price_g"] for i in client.get("/api/watchlist").json()["items"]}
    assert items[a["id"]] == 10
    assert items[b["id"]] == 25.5


def test_batch_update_can_clear_a_trigger_price():
    a = client.post("/api/watchlist", json={"item_id": 1, "trigger_price_g": 10}).json()
    r = client.patch("/api/watchlist/batch", json={"items": [{"id": a["id"], "trigger_price_g": None}]})
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "skipped": 0}
    assert client.get("/api/watchlist").json()["items"][0]["trigger_price_g"] is None


def test_batch_update_skips_unowned_and_missing_ids():
    a = client.post("/api/watchlist", json={"item_id": 1, "trigger_price_g": 1}).json()
    r = client.patch("/api/watchlist/batch", json={"items": [
        {"id": a["id"], "trigger_price_g": 5},
        {"id": 999999, "trigger_price_g": 5},
    ]})
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "skipped": 1}


def test_batch_update_skips_negative_prices_without_failing_whole_batch():
    a = client.post("/api/watchlist", json={"item_id": 1, "trigger_price_g": 1}).json()
    b = client.post("/api/watchlist", json={"item_id": 2, "trigger_price_g": 1}).json()
    r = client.patch("/api/watchlist/batch", json={"items": [
        {"id": a["id"], "trigger_price_g": -5},
        {"id": b["id"], "trigger_price_g": 5},
    ]})
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "skipped": 1}
    items = {i["id"]: i["trigger_price_g"] for i in client.get("/api/watchlist").json()["items"]}
    assert items[a["id"]] == 1  # unchanged, the negative edit was skipped
    assert items[b["id"]] == 5


def test_batch_update_empty_list_is_a_no_op():
    r = client.patch("/api/watchlist/batch", json={"items": []})
    assert r.status_code == 200
    assert r.json() == {"updated": 0, "skipped": 0}


def test_batch_update_cannot_touch_another_users_items():
    a = client.post("/api/watchlist", json={"item_id": 1, "trigger_price_g": 1}).json()
    other_user = User(id=uuid.uuid4(), email="other-batch@example.com", hashed_password="x",
                      is_active=True, is_superuser=False, is_verified=True,
                      subscription_status="active")
    dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: other_user
    r = client.patch("/api/watchlist/batch", json={"items": [{"id": a["id"], "trigger_price_g": 5}]})
    assert r.status_code == 200
    assert r.json() == {"updated": 0, "skipped": 1}


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
    added = client.post("/api/watchlist", json={"item_id": 111, "trigger_price_g": 10}).json()
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

async def _make_real_user(session_factory, discord_webhook_url=None,
                          default_sniper_list_enabled=True):
    async with session_factory() as session:
        user = User(email=f"real-{uuid.uuid4()}@example.com", hashed_password="x",
                   is_active=True, is_superuser=False, is_verified=True,
                   subscription_status="active", discord_webhook_url=discord_webhook_url,
                   default_sniper_list_enabled=default_sniper_list_enabled)
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

    def _factory(discord_webhook_url=None, default_sniper_list_enabled=True):
        user = asyncio.run(_make_real_user(session_factory, discord_webhook_url,
                                           default_sniper_list_enabled))
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
    assert "5.00g" in embed_field(posted[0][1], "Price")


async def _add_wow_account_realm(session_factory, owner_id, label, cr_id, realm_name=None):
    async with session_factory() as session:
        account = WowAccount(owner_id=owner_id, label=label)
        session.add(account)
        await session.flush()
        session.add(WowAccountRealm(wow_account_id=account.id, connected_realm_id=cr_id, realm_name=realm_name))
        await session.commit()


def test_check_triggers_notification_names_the_registered_wow_account(listings_dir, real_user, monkeypatch,
                                                                      bypass_get_async_session):
    user = real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})
    asyncio.run(_add_wow_account_realm(bypass_get_async_session, user.id, "Main", 99))

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    watchlist.check_triggers()
    assert embed_field(posted[0][1], "Log in on") == "Main"


def test_check_triggers_notification_names_every_matching_wow_account(listings_dir, real_user, monkeypatch,
                                                                      bypass_get_async_session):
    """A realm can legitimately be registered on more than one of the
    user's own WoW accounts -- no uniqueness constraint across accounts,
    only within one (see db.WowAccountRealm)."""
    user = real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})
    asyncio.run(_add_wow_account_realm(bypass_get_async_session, user.id, "Main", 99))
    asyncio.run(_add_wow_account_realm(bypass_get_async_session, user.id, "Alt", 99))

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    watchlist.check_triggers()
    assert embed_field(posted[0][1], "Log in on") == "Main, Alt"


def test_check_triggers_notification_shows_the_specific_realm_name_registered(listings_dir, real_user, monkeypatch,
                                                                              bypass_get_async_session):
    """WowAccountRealm.realm_name (added 2026-08-02) carries the specific
    member name the user picked at registration -- a connected realm can
    bundle several names under one id, so this is what lets the message
    say exactly which one this account is on."""
    user = real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})
    asyncio.run(_add_wow_account_realm(bypass_get_async_session, user.id, "Main", 99, realm_name="Zul'jin"))

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    watchlist.check_triggers()
    assert embed_field(posted[0][1], "Log in on") == "Main (Zul'jin)"


def test_check_triggers_notification_omits_account_note_when_none_registered(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    watchlist.check_triggers()
    assert embed_field(posted[0][1], "Log in on") is None


def test_check_triggers_notification_lists_every_connected_realm_member_name(listings_dir, real_user, monkeypatch):
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})
    monkeypatch.setattr(blizz, "connected_realm_realms",
                        lambda cr_id: [{"name": "Zul'jin", "slug": "zuljin", "category": "English"},
                                      {"name": "Sanguino", "slug": "sanguino", "category": "Spanish"}])

    pq.write_table(pa.Table.from_pylist(
        [listing_row(cr=99, item_id=555, buyout=50_000, auction_id=1)],
        schema=LISTING_SCHEMA,
    ), listings_dir / "99.parquet")

    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append((url, json)))

    watchlist.check_triggers()
    realm = embed_field(posted[0][1], "Realm")
    assert "Zul'jin / Sanguino" in realm
    assert "(connected realm)" in realm


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
    # add_item() requires a trigger price at creation (2026-08-02) -- clear
    # it via batch update to reach the "no trigger set" state this test
    # needs, same as a TSM-imported item (which starts with none) would be.
    added = client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 1}).json()
    client.patch("/api/watchlist/batch", json={"items": [{"id": added["id"], "trigger_price_g": None}]})

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


# ------------------------------------------------- standing rule scan ----
# The experimental "all items under 100g whose TSM sale avg is over 3000g"
# rule (2026-08-05). Sell-realm-free by the human's explicit choice, so the
# only two heuristics it can apply are sus_item and the sniper filter's
# cluster comparison -- see watchlist.py's RULE_* comment block.

G = 10_000  # copper per gold, spelled out so the vectors below read in gold


class FakeSaleRates:
    """Stands in for tsm.SaleRateCache. `avgs` is {item_id: gold}; an item
    absent from it has no TSM average at all, which the rule treats as
    "ignore" rather than as a pass."""
    def __init__(self, avgs):
        self._avgs = avgs

    def get(self, item_id):
        if item_id not in self._avgs:
            return None
        return {"sale_rate": 0.1, "sold_per_day": 1.0,
                "avg_sale_price": self._avgs[item_id] * G}


class FakeAppearances:
    def __init__(self, sources):
        self._sources = sources

    def source_count(self, item_id):
        return self._sources.get(item_id)


class FakeNames:
    def __init__(self, inventory_types=None, base_levels=None,
                 item_classes=None, item_subclasses=None,
                 qualities=None, purchase_prices=None):
        self._inv = inventory_types or {}
        self._base = base_levels or {}
        self._class = item_classes or {}
        self._subclass = item_subclasses or {}
        # Default UNCOMMON / no vendor price: the grey-white and vendor
        # rules would otherwise drop every fixture item, so a test that
        # is not about those two stays about what it says it is.
        self._quality = qualities or {}
        self._pp = purchase_prices or {}

    def quality(self, item_id, pet_species_id=None, pet_quality_id=None):
        return self._quality.get(item_id, "UNCOMMON")

    def purchase_price(self, item_id):
        return self._pp.get(item_id, 0)

    def item_class(self, item_id):
        return self._class.get(item_id)

    def item_subclass(self, item_id):
        return self._subclass.get(item_id)

    def get(self, item_id, pet_species_id=None):
        return f"Item {item_id}"

    def inventory_type(self, item_id):
        return self._inv.get(item_id)

    def base_level(self, item_id):
        return self._base.get(item_id)

    def save(self):
        pass


@pytest.fixture
def rule_caches(monkeypatch):
    """Stubs the three caches _rule_scan() reads. Returns a setter so each
    test states only the data it cares about."""
    def _configure(avgs=None, sources=None, inventory_types=None, base_levels=None,
                   item_classes=None, item_subclasses=None, qualities=None,
                   purchase_prices=None):
        monkeypatch.setattr(watchlist.tsm, "SaleRateCache",
                            lambda: FakeSaleRates(avgs or {}))
        monkeypatch.setattr(watchlist, "AppearanceCache",
                            lambda: FakeAppearances(sources or {}))
        monkeypatch.setattr(watchlist, "NameCache",
                            lambda: FakeNames(inventory_types, base_levels,
                                              item_classes, item_subclasses,
                                              qualities, purchase_prices))
    return _configure


def write_listings(listings_dir, rows):
    """One parquet per realm, matching how scan_region.py actually writes
    them -- a single combined file would let a bug that ignores cr_id
    still pass."""
    by_realm = {}
    for r in rows:
        by_realm.setdefault(r["cr_id"], []).append(r)
    for cr, realm_rows in by_realm.items():
        pq.write_table(pa.Table.from_pylist(realm_rows, schema=LISTING_SCHEMA),
                       listings_dir / f"{cr}.parquet")


def test_rule_candidates_returns_every_item_regardless_of_price(listings_dir):
    """The buy ceiling became per-item when it started depending on the
    item's TSM sale average, so it deliberately no longer lives in this
    query -- _rule_scan() applies it after the TSM lookup instead."""
    write_listings(listings_dir, [
        listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1),
        listing_row(cr=2, item_id=222, buyout=150_000 * G, auction_id=2),
    ])
    got = {c["item_id"] for c in watchlist._rule_candidates()}
    assert got == {111, 222}


def test_rule_candidates_uses_unit_price_not_stack_price(listings_dir):
    """buyout is the whole-stack price. A 5-stack at 250g total is 50g/unit
    and must qualify -- the copper/unit-price distinction CLAUDE.md calls
    out as a repeat source of real bugs."""
    row = listing_row(cr=1, item_id=111, buyout=250 * G, auction_id=1)
    row["quantity"] = 5
    write_listings(listings_dir, [row])
    cands = watchlist._rule_candidates()
    assert len(cands) == 1
    assert cands[0]["buy_copper"] == 50 * G


def test_rule_candidates_cluster_uses_the_five_cheapest_other_realms(listings_dir):
    """Real vector, measured live 2026-08-05: item 204925's per-realm floors
    across EU. Cheapest realm 8,496g, next five 20,000/29,701/29,999/
    50,000/59,999 -> cluster median 29,999g."""
    floors_g = [8_496, 20_000, 29_701, 29_999, 50_000, 59_999, 65_000, 90_001]
    write_listings(listings_dir, [
        listing_row(cr=i + 1, item_id=204925, buyout=f * G, auction_id=i + 1)
        for i, f in enumerate(floors_g)
    ])
    cands = watchlist._rule_candidates()
    assert len(cands) == 1
    c = cands[0]
    assert c["buy_copper"] == 8_496 * G
    assert c["cluster_realms"] == 5
    assert c["cluster_median"] == 29_999 * G
    # 29,999 > 8,496 * 1.7 (= 14,443) -> a genuine outlier, not flagged.
    assert watchlist._rule_cluster_suspect(c) is False


def test_rule_candidates_realm_floor_ignores_a_realms_own_duplicate_listings(listings_dir):
    """One realm spamming copies must not fill the cluster -- the cluster is
    over per-realm floors, mirroring snipe_check's region_realm_floor."""
    rows = [listing_row(cr=1, item_id=111, buyout=10 * G, auction_id=1)]
    rows += [listing_row(cr=2, item_id=111, buyout=(11 + i) * G, auction_id=10 + i)
             for i in range(6)]
    write_listings(listings_dir, rows)
    c = watchlist._rule_candidates()[0]
    assert c["cluster_realms"] == 1  # realm 2 contributes exactly one floor
    assert watchlist._rule_cluster_suspect(c) is False  # below RULE_CLUSTER_MIN_REALMS


def test_rule_cluster_suspect_flags_a_corroborated_price():
    """Cluster median within RULE_CLUSTER_CLOSE_MULTIPLE of the buy price:
    several realms independently agree this is just what the item costs."""
    cand = {"buy_copper": 100 * G, "cluster_median": 150 * G, "cluster_realms": 5}
    assert watchlist._rule_cluster_suspect(cand) is True


def test_rule_cluster_suspect_boundary_is_inclusive():
    exactly = {"buy_copper": 100 * G,
               "cluster_median": 100 * G * watchlist.RULE_CLUSTER_CLOSE_MULTIPLE,
               "cluster_realms": 5}
    assert watchlist._rule_cluster_suspect(exactly) is True
    just_over = dict(exactly, cluster_median=exactly["cluster_median"] + 1)
    assert watchlist._rule_cluster_suspect(just_over) is False


def test_rule_cluster_suspect_needs_enough_realms_to_judge():
    """Too few other realms is "not enough data", which must resolve to
    not-flagged -- the same "unknown isn't a claim" convention the original
    sniper_filter_suspect uses, not an assumed pass."""
    thin = {"buy_copper": 100 * G, "cluster_median": 100 * G,
            "cluster_realms": watchlist.RULE_CLUSTER_MIN_REALMS - 1}
    assert watchlist._rule_cluster_suspect(thin) is False


def test_rule_scan_skips_items_with_no_tsm_sale_average(listings_dir, rule_caches):
    """Human's call 2026-08-05: an item TSM has no average for is ignored,
    not passed through."""
    rule_caches(avgs={})  # TSM knows nothing about item 111
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_ignores_items_under_the_minimum_sale_average(listings_dir, rule_caches):
    """Under 2,000g sale average the item is ignored **however cheap it is**.
    This is the clause that stops the rule flooding: an intermediate version
    that capped these at a flat 100g instead of rejecting them produced
    6,580 hits against the live sweep, almost all sub-1g trade goods."""
    rule_caches(avgs={111: 1_999})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=1, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_minimum_sale_average_boundary_is_inclusive(listings_dir, rule_caches):
    rule_caches(avgs={111: 2_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=199 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [111]


def test_rule_scan_high_sale_avg_items_use_the_ten_percent_cap(listings_dir, rule_caches):
    """Human's spec: "if item is over 5k sellavg, it can cost up to 500"."""
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=499 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [111]


def test_rule_scan_high_sale_avg_item_over_ten_percent_is_dropped(listings_dir, rule_caches):
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=501 * G, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_ceiling_scales_with_the_sale_average(listings_dir, rule_caches):
    """A 50,000g item qualifies up to 5,000g -- the whole point of making the
    ceiling proportional, since the old flat 100g hid exactly these."""
    rule_caches(avgs={111: 50_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=4_999 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [111]


def test_rule_max_buy_is_a_flat_ten_percent():
    for avg_g, cap_g in ((2_000, 200), (5_000, 500), (11_111, 1_111.1), (50_000, 5_000)):
        assert watchlist._rule_max_buy_copper(avg_g * G) == pytest.approx(cap_g * G)


def test_the_real_item_that_missed_the_old_flat_ceiling_now_qualifies(listings_dir, rule_caches):
    """Item 29726 "Pattern: Hood of Primal Life", real live figures 2026-08-05
    (human found it by hand and asked why it wasn't sent): TSM avg 11,111g,
    cheapest realm 100g, next realms 9,000g / 10,001g / 16,098g / 16,100g /
    16,100g. It missed the old flat 100g ceiling by a single copper -- 100g
    is not < 100g -- and clears the 10% one (1,111g) comfortably."""
    rule_caches(avgs={29726: 11_111})
    floors_g = [100, 9_000, 10_001, 16_098, 16_100, 16_100]
    write_listings(listings_dir, [
        listing_row(cr=i + 1, item_id=29726, buyout=f * G, auction_id=i + 1)
        for i, f in enumerate(floors_g)
    ])
    hits = watchlist._rule_scan()
    assert [h["item_id"] for h in hits] == [29726]
    assert hits[0]["buy_copper"] == 100 * G


def test_rule_scan_keeps_a_cheap_high_value_item(listings_dir, rule_caches):
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    hits = watchlist._rule_scan()
    assert [h["item_id"] for h in hits] == [111]
    assert hits[0]["region_sale_avg_copper"] == 5_000 * G


def test_rule_scan_drops_a_cluster_flagged_item(listings_dir, rule_caches):
    """Flagged items must never be sent -- a buy price several other realms
    corroborate never reaches the notification step."""
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [
        listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1),
        listing_row(cr=2, item_id=111, buyout=55 * G, auction_id=2),
        listing_row(cr=3, item_id=111, buyout=60 * G, auction_id=3),
        listing_row(cr=4, item_id=111, buyout=65 * G, auction_id=4),
    ])
    assert watchlist._rule_scan() == []


def test_rule_scan_drops_a_sus_item(listings_dir, rule_caches):
    """snipe_check.is_sus_item's legacy-jewelry rule, applied unchanged."""
    rule_caches(avgs={111: 5_000},
                inventory_types={111: "FINGER"},
                base_levels={111: snipe_check.LEGACY_JEWELRY_ILVL_MAX})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_drops_non_unique_transmog(listings_dir, rule_caches):
    rule_caches(avgs={111: 5_000}, sources={111: 4}, inventory_types={111: "SHOULDER"})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_keeps_unique_transmog(listings_dir, rule_caches):
    rule_caches(avgs={111: 5_000}, sources={111: 1}, inventory_types={111: "SHOULDER"})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [111]


def test_rule_scan_keeps_a_non_transmog_item_without_applying_the_unique_test(
        listings_dir, rule_caches):
    """An item with no appearance at all (mount, recipe, caged pet) is simply
    not transmog, so the uniqueness test does not apply to it. This is the
    opposite disposition from snipe_check._filter_by_appearance(), which
    answers a different question -- see _rule_scan()'s comment."""
    rule_caches(avgs={111: 5_000}, sources={})  # source_count -> None
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [111]


def test_rule_scan_drops_profession_tools_entirely(listings_dir, rule_caches):
    """Human's call 2026-08-05, from a real delivered message ("Burnt Rolling
    Pin"): profession tools/gear "should never be sent/showed". They now
    fail is_sus_item() outright, so the earlier behavior -- passing because
    they aren't transmog and so escape the uniqueness test -- is gone."""
    rule_caches(avgs={111: 5_000}, sources={111: 7},
                inventory_types={111: "PROFESSION_TOOL"})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_drops_blizzard_junk_class_items(listings_dir, rule_caches):
    """Human's call 2026-08-05, from a real delivered message ("Undelivered
    Love Letter", item 67386): junk "never". Uses Blizzard's own
    class 15 / subclass 0 = Junk classification, confirmed live."""
    rule_caches(avgs={67386: 5_000}, inventory_types={67386: "NON_EQUIP"},
                item_classes={67386: 15}, item_subclasses={67386: 0})
    write_listings(listings_dir, [listing_row(cr=1, item_id=67386, buyout=50 * G, auction_id=1)])
    assert watchlist._rule_scan() == []


def test_rule_scan_keeps_mounts_which_share_junks_item_class(listings_dir, rule_caches):
    """Mounts are class 15 subclass 5 -- the Junk rule is subclass-specific
    precisely so a class-only check can't swallow the category the human
    explicitly asked to keep ("we still want mounts/pets/recipes")."""
    rule_caches(avgs={111: 5_000}, inventory_types={111: "NON_EQUIP"},
                item_classes={111: 15}, item_subclasses={111: 5})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [111]


def test_rule_scan_keeps_recipes(listings_dir, rule_caches):
    """Recipes are class 9, untouched by the Junk rule -- the four
    recipe/technique/pattern/schematic items in the same real Discord batch
    were not objected to."""
    rule_caches(avgs={11168: 5_000}, inventory_types={11168: "NON_EQUIP"},
                item_classes={11168: 9}, item_subclasses={11168: 8})
    write_listings(listings_dir, [listing_row(cr=1, item_id=11168, buyout=50 * G, auction_id=1)])
    assert [h["item_id"] for h in watchlist._rule_scan()] == [11168]


def test_rule_scan_matches_caged_pets_by_species(listings_dir, rule_caches):
    """pet_species_id is NULL for every non-pet row, so the cluster join has
    to use IS NOT DISTINCT FROM -- a plain equality join silently drops
    every ordinary item."""
    rule_caches(avgs={82800: 5_000})
    write_listings(listings_dir, [
        listing_row(cr=1, item_id=82800, buyout=50 * G, auction_id=1, pet_species_id=42),
    ])
    hits = watchlist._rule_scan()
    assert len(hits) == 1
    assert hits[0]["pet_species_id"] == 42


# ---- delivery: who gets told, how often, and how many at once ----

async def _make_superuser(session_factory, discord_webhook_url):
    async with session_factory() as session:
        user = User(email=f"su-{uuid.uuid4()}@example.com", hashed_password="x",
                    is_active=True, is_superuser=True, is_verified=True,
                    subscription_status="active",
                    discord_webhook_url=discord_webhook_url,
                    # Explicit: the column defaults to off since 2026-08-05,
                    # so a rule-delivery test has to opt in like a real user.
                    default_sniper_list_enabled=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
def superuser(bypass_get_async_session):
    def _factory(discord_webhook_url="https://discord.com/api/webhooks/1/su"):
        return asyncio.run(_make_superuser(bypass_get_async_session, discord_webhook_url))
    return _factory


def test_rule_notifies_a_superuser(listings_dir, rule_caches, superuser, monkeypatch,
                                   stub_realm_lookup):
    superuser()
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["rule_notified"] == 1
    assert len(posted) == 1
    embed = embed_of(posted[0])
    assert embed["title"] == "1 snipe"
    # One line per find, carrying only what the human asked for: name (as
    # the undermine link), buy price, sale average, realm.
    assert embed["description"] == (
        "**[Item 111](https://undermine.exchange/#eu-realm-1/111)** · "
        "**50g** · avg 5,000g · Realm 1")


def test_rule_notifies_any_active_subscriber(listings_dir, rule_caches, real_user,
                                             monkeypatch, stub_realm_lookup):
    """Human's call 2026-08-05: "Every sub should be able to use it" -- an
    ordinary subscribed (non-superuser) account is a recipient."""
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["rule_recipients"] == 1
    assert result["rule_notified"] == 1
    assert len(posted) == 1


def test_rule_does_not_notify_an_unsubscribed_account(listings_dir, rule_caches,
                                                      bypass_get_async_session,
                                                      monkeypatch, stub_realm_lookup):
    """Premium-only, same audience as every other Watchlist route. Gated
    through auth.has_active_subscription() rather than a re-expressed SQL
    predicate, so this also pins that the helper is what decides."""
    async def _make():
        async with bypass_get_async_session() as session:
            session.add(User(email=f"lapsed-{uuid.uuid4()}@example.com", hashed_password="x",
                             is_active=True, is_superuser=False, is_verified=True,
                             subscription_status="canceled",
                             discord_webhook_url="https://discord.com/api/webhooks/1/x"))
            await session.commit()
    asyncio.run(_make())
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["rule_recipients"] == 0
    assert result["rule_notified"] == 0
    assert posted == []


def test_rule_respects_the_cooldown_across_cycles(listings_dir, rule_caches, superuser,
                                                  monkeypatch, stub_realm_lookup):
    """The cooldown lives in a state file, not on a DB row, so it has to
    survive a second call within the window."""
    superuser()
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    assert watchlist.check_triggers()["rule_notified"] == 1
    assert watchlist.check_triggers()["rule_notified"] == 0
    assert len(posted) == 1


def test_rule_cooldown_state_is_written_under_the_patched_data_dir(listings_dir, rule_caches,
                                                                   superuser, monkeypatch,
                                                                   stub_realm_lookup):
    """Hermeticity: the state file must land in the test's tmp_path, never
    in the developer's real data/ directory."""
    superuser()
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    monkeypatch.setattr(watchlist.requests, "post", lambda url, json, timeout: None)

    watchlist.check_triggers()
    state_path = watchlist._rule_state_path()
    assert state_path.parent.parent == listings_dir.parent
    assert state_path.exists()


def test_rule_caps_notifications_per_cycle(listings_dir, rule_caches, superuser,
                                           monkeypatch, stub_realm_lookup):
    """A first run against a full sweep must not fire hundreds of messages.
    Over-cap hits are not marked notified, so they come back next cycle."""
    monkeypatch.setattr(watchlist, "RULE_MAX_NOTIFICATIONS_PER_CYCLE", 2)
    superuser()
    item_ids = [111, 222, 333, 444]
    rule_caches(avgs={i: 5_000 for i in item_ids})
    write_listings(listings_dir, [
        listing_row(cr=1, item_id=i, buyout=50 * G, auction_id=n)
        for n, i in enumerate(item_ids)
    ])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["rule_hits"] == 4
    assert result["rule_notified"] == 2
    assert result["rule_suppressed_by_cap"] == 2
    # One message, not one per find (2026-08-05) -- the cap now bounds how
    # many *lines* it carries, not how many messages get sent.
    assert len(posted) == 1
    assert embed_of(posted[0])["title"] == "2 snipes"
    assert len(embed_of(posted[0])["description"].splitlines()) == 2


def test_rule_failure_does_not_break_the_per_item_trigger_path(listings_dir, real_user,
                                                               monkeypatch):
    """The experimental rule runs in its own try/except -- a crash in it must
    leave the established per-item notification path working."""
    # A recipient has to exist, otherwise _check_rule_async() short-circuits
    # before _rule_scan() and the crash never happens -- the test would pass
    # while exercising nothing. real_user is subscribed, so it is one.
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a")
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})
    write_listings(listings_dir, [listing_row(cr=99, item_id=555, buyout=5 * G, auction_id=1)])

    def _boom():
        raise RuntimeError("rule exploded")
    monkeypatch.setattr(watchlist, "_rule_scan", _boom)
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["notified"] == 1      # per-item path unaffected
    assert result.get("rule_error") is True
    assert len(posted) == 1


# ---- "Default sniper list" per-user toggle (2026-08-05) ----

def test_default_sniper_list_is_disabled_by_default(bypass_get_async_session):
    """Defaults OFF (changed 2026-08-05, human request). It shipped
    defaulting on so an already-live feature would not vanish for accounts
    that had it; as a standing opt-in it should not enrol new accounts into
    unsolicited Discord messages.

    Asserted against a **persisted** row, not the module-level FAKE_USER --
    SQLAlchemy applies a column default on INSERT, so an object merely
    constructed in Python still has None there and would prove nothing."""
    async def _make():
        async with bypass_get_async_session() as session:
            user = User(email=f"dflt-{uuid.uuid4()}@example.com", hashed_password="x",
                        is_active=True, is_superuser=False, is_verified=True,
                        subscription_status="active")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user.default_sniper_list_enabled
    assert asyncio.run(_make()) is False


def test_default_sniper_list_can_be_toggled_off_and_back_on(real_user):
    user = real_user()
    r = client.patch("/api/watchlist/default-sniper-list", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["default_sniper_list_enabled"] is False
    assert client.get("/api/watchlist").json()["default_sniper_list_enabled"] is False

    r = client.patch("/api/watchlist/default-sniper-list", json={"enabled": True})
    assert r.status_code == 200
    assert client.get("/api/watchlist").json()["default_sniper_list_enabled"] is True
    assert user.default_sniper_list_enabled is True


def test_default_sniper_list_route_is_not_swallowed_by_the_item_id_route():
    """Route-order regression guard, the same one /discord-webhook needed:
    a "/{item_id}: int" pattern registered first would turn this path into a
    422 int-parse failure instead of reaching the handler."""
    r = client.patch("/api/watchlist/default-sniper-list", json={"enabled": True})
    assert r.status_code == 200


def test_rule_skips_a_subscriber_who_turned_the_list_off(listings_dir, rule_caches,
                                                         real_user, monkeypatch,
                                                         stub_realm_lookup):
    """The toggle has to gate delivery, not just render in the UI."""
    # Seeded at creation, not via the PATCH route -- see real_user()'s
    # docstring: in this bypassed-session setup the route mutates a detached
    # object, so check_triggers() would still read the old value from the DB
    # and the test would pass for the wrong reason.
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a",
              default_sniper_list_enabled=False)
    rule_caches(avgs={111: 5_000})
    write_listings(listings_dir, [listing_row(cr=1, item_id=111, buyout=50 * G, auction_id=1)])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["rule_recipients"] == 0
    assert result["rule_notified"] == 0
    assert posted == []


def test_turning_the_list_off_leaves_per_item_triggers_working(listings_dir, real_user,
                                                                monkeypatch, stub_realm_lookup):
    """The toggle is scoped to the standing rule only -- a user must be able
    to keep their own watched items firing while turning the rule off."""
    real_user(discord_webhook_url="https://discord.com/api/webhooks/1/a",
              default_sniper_list_enabled=False)
    client.post("/api/watchlist", json={"item_id": 555, "trigger_price_g": 10})
    write_listings(listings_dir, [listing_row(cr=99, item_id=555, buyout=5 * G, auction_id=1)])
    posted = []
    monkeypatch.setattr(watchlist.requests, "post",
                        lambda url, json, timeout: posted.append(json))

    result = watchlist.check_triggers()
    assert result["notified"] == 1        # per-item path still fires
    assert result["rule_notified"] == 0   # standing rule does not
    assert len(posted) == 1
