"""Tests for dashboard.py: the FastAPI read-only web layer over
snipe_check.find_snipes(). Mirrors test_snipe_check.py's synthetic-pipeline
fixture style -- real duckdb/pyarrow, no mocking, only the HTTP/JSON
boundary is new relative to the existing test conventions."""
import asyncio
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import analyze
import appearance
import auth
import blizz
import dashboard
import diff_snapshots
import item_names
import snipe_check
from db import Base, User, get_async_session
from fetch_snapshot import SCHEMA
from scan_region import LISTING_SCHEMA

SELL_CR = 9999
BUY_CR_A = 1111
T0, T1 = 1_700_000_000, 1_700_003_600

client = TestClient(dashboard.app)

FAKE_USER = User(email="test@example.com", hashed_password="x",
                 is_active=True, is_superuser=False, is_verified=True,
                 subscription_status="active")


@pytest.fixture(autouse=True)
def bypass_auth():
    """These tests exercise snipe_check/dashboard business logic, not auth
    or billing (see test_auth.py/test_billing.py for those) -- override
    FastAPI's dependency injection to skip real login, the standard FastAPI
    testing pattern, rather than requiring every test here to register+log
    in a real, actually-subscribed user."""
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
    dashboard.app.dependency_overrides[auth.current_subscribed_user] = lambda: FAKE_USER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)
    dashboard.app.dependency_overrides.pop(auth.current_subscribed_user, None)


@pytest.fixture(autouse=True)
def bypass_get_async_session(tmp_path):
    """/api/snipes now also depends on get_async_session directly (for the
    free-tier realm lock, see dashboard._enforce_realm_lock) -- FastAPI
    resolves every declared dependency regardless of whether a given
    request's code path actually uses it, so even a subscribed FAKE_USER
    request (which never reaches the lock's write branch) still needs this
    to resolve. Without an override it falls through to the real
    get_async_session, which requires DATABASE_URL -- absent in CI, and
    this suite has no business touching a real Postgres anyway. Same
    throwaway-per-test-SQLite pattern test_auth.py's `client` fixture uses,
    just for dashboard.py's TestClient(app) module-level instance instead of
    a per-test one."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test_dashboard.db'}"
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
    yield
    dashboard.app.dependency_overrides.pop(get_async_session, None)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def stub_realm_info(monkeypatch):
    """dashboard._realm_info() calls the live Blizzard API -- stub it and
    reset the in-process cache so tests stay offline and deterministic."""
    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms",
                        lambda cr_id: [{"name": f"Realm {cr_id}", "slug": f"realm-{cr_id}", "category": "English"}])


@pytest.fixture(autouse=True)
def isolate_item_names_cache(tmp_path, monkeypatch):
    """dashboard.py's `names=true` path instantiates a real item_names.NameCache,
    which reads/writes CACHE_PATH (data/item_names.json) unless redirected --
    autouse so no test can accidentally read from or write into the real,
    gitignored project cache."""
    monkeypatch.setattr(item_names, "CACHE_PATH", tmp_path / "item_names_test_cache.json")


@pytest.fixture(autouse=True)
def default_item_class_stub(monkeypatch):
    """snipe_check.find_snipes()'s class_quotas (added 2026-07-27) resolves
    item_class for every row regardless of names=true -- a row whose class
    can't be resolved falls into no quota bucket at all (see
    snipe_check._class_bucket()) and gets silently excluded from results.
    Without this default, every test's fixture item would vanish from
    /api/snipes' response the instant class_quotas became unconditional,
    since the real _fetch_item_details() hits the live Blizzard API (no
    token in CI) and returns None. Armor (class 4) is a stable, always-
    quota'd bucket at every tier -- a test that cares about a specific
    item_class calls stub_item_details() itself later in the test body,
    which overrides this."""
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": None, "quality": None, "level": None,
                                          "inventory_type": None, "item_class": 4, "item_subclass": 2})


@pytest.fixture(autouse=True)
def isolate_appearance_cache(tmp_path, monkeypatch):
    """find_snipes() instantiates a real appearance.AppearanceCache, which
    reads CACHE_PATH (data/appearances.json) unless redirected -- same
    isolation reasoning as isolate_item_names_cache above: no test should
    depend on whatever the real, gitignored local cache happens to contain."""
    monkeypatch.setattr(appearance, "CACHE_PATH", tmp_path / "appearances_test_cache.json")


def stub_item_details(monkeypatch, name="Stub Item", quality="EPIC", level=600,
                      icon="https://example/icon.jpg", item_class=4, item_subclass=2,
                      inventory_type=None):
    """item_names.NameCache hits the live Blizzard API -- stub the network
    edge so names=true tests stay offline and deterministic. item_class/
    item_subclass default to Armor (a stable, always-quota'd class_quotas
    bucket, see default_item_class_stub above) rather than None -- a test
    that doesn't care about item_class shouldn't have its row silently
    excluded by class-quota bucketing; a test that does care passes its own
    values explicitly (e.g. test_api_snipes_carries_item_class_when_names_resolved)."""
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": name, "quality": quality, "level": level,
                                         "item_class": item_class, "item_subclass": item_subclass,
                                         "inventory_type": inventory_type})
    monkeypatch.setattr(item_names, "_fetch_icon", lambda path: icon)


def snap_row(auction_id, ts, item_id=101, buyout=20_000, quantity=1,
             time_left="VERY_LONG", bonus_key="", pet_species_id=None, pet_quality_id=None):
    return {
        "snapshot_ts": ts, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": pet_species_id,
        "pet_quality_id": pet_quality_id,
        "pet_level": None, "buyout": buyout, "bid": None,
        "quantity": quantity, "time_left": time_left,
    }


def listing_row(cr, item_id, buyout, auction_id, bonus_key="",
                 pet_species_id=None, pet_quality_id=None):
    return {
        "cr_id": cr, "fetched_ts": T1, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": pet_species_id,
        "pet_quality_id": pet_quality_id,
        "pet_level": None, "buyout": buyout, "bid": None, "quantity": 1,
        "time_left": "VERY_LONG",
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    # item 101's sell price is its current live listing: 20_000 copper = 2g
    # (pricing model changed 2026-07-25 -- sell price is the sell realm's
    # current cheapest listing, not an inferred sold-price percentile).
    prev = [snap_row(3, T0, item_id=103)]  # survives -> no event
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(10, T1, item_id=101, buyout=20_000),  # current sell-realm listing: 2g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100),  # cheap -> snipe
        listing_row(BUY_CR_A, item_id=101, buyout=25_000, auction_id=101),  # pricier -> not a snipe
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / f"{SELL_CR}.json").write_text('{"last_modified": "Thu, 23 Jul 2026 11:20:39 GMT"}')
    return tmp_path


def run_diff(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["diff_snapshots.py", "--cr-id", str(SELL_CR)])
    diff_snapshots.main()


def test_api_snipes_returns_rows_and_caveat(data_dir, monkeypatch):
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3})
    assert r.status_code == 200
    body = r.json()
    assert body["caveat"] == snipe_check.CAVEAT
    assert body["count"] == 1
    assert body["rows"][0]["item_id"] == 101
    assert body["rows"][0]["buy_realm"] == BUY_CR_A
    assert body["rows"][0]["buy_copper"] == 10_000
    assert body["rows"][0]["buy_realm_name"] == f"Realm {BUY_CR_A}"
    assert body["rows"][0]["buy_realm_category"] == "English"
    assert body["region"] == blizz.REGION
    assert body["sell_realm_slug"] == f"realm-{SELL_CR}"


def test_api_snipes_rows_carry_pet_identity_without_names(data_dir, monkeypatch):
    """pet_species_id/pet_quality_id (replacing market_key, 2026-07-26 --
    see snipe_check.find_snipes()'s docstring) must ride along even without
    names=true -- it's what dashboard.html groups rows by now, unrelated to
    the names/icon/quality resolution names=true gates. Both None here
    (item 101, non-pet) -- test_api_snipes_pet_variant_unaffected_by_ilvl_parsing
    covers the pet case."""
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3})
    row = r.json()["rows"][0]
    assert "pet_species_id" in row and row["pet_species_id"] is None
    assert "pet_quality_id" in row and row["pet_quality_id"] is None


def _user(is_superuser=False, subscription_status=None):
    return User(email="tier@example.com", hashed_password="x", is_active=True,
               is_superuser=is_superuser, is_verified=True, subscription_status=subscription_status)


def test_snipe_cap_by_tier():
    """dashboard._snipe_cap(): the free tier (2026-07-25) exists so a
    logged-in-but-unsubscribed user can preview real data, just capped much
    lower than a paying subscriber; superuser is generous founder/admin
    headroom, not a real subscription concept."""
    assert dashboard._snipe_cap(_user()) == 250
    assert dashboard._snipe_cap(_user(subscription_status="past_due")) == 250
    assert dashboard._snipe_cap(_user(subscription_status="active")) == 2000
    assert dashboard._snipe_cap(_user(is_superuser=True)) == 10000
    # Superuser wins even with no/expired subscription -- matches
    # auth.has_active_subscription's existing "is_superuser OR active" logic.
    assert dashboard._snipe_cap(_user(is_superuser=True, subscription_status=None)) == 10000


def test_class_quotas_by_tier_sum_to_the_tier_cap():
    """dashboard._class_quotas() (added 2026-07-27, human-specified numbers):
    each tier's per-bucket quotas must sum to exactly that tier's own
    SNIPE_TIER_CAPS ceiling, not more (would silently over-fetch past what
    the tier is supposed to see) or less (would waste part of the budget on
    nothing). Free tier deliberately has no quest/profession/container
    entries at all -- a human product decision (see FREE_CLASS_QUOTAS'
    module comment), not a gap to fill in."""
    assert dashboard._class_quotas(_user()) == dashboard.FREE_CLASS_QUOTAS
    assert sum(dashboard.FREE_CLASS_QUOTAS.values()) == dashboard.SNIPE_TIER_CAPS["free"]
    assert "quest" not in dashboard.FREE_CLASS_QUOTAS
    assert "profession" not in dashboard.FREE_CLASS_QUOTAS
    assert "container" not in dashboard.FREE_CLASS_QUOTAS

    assert dashboard._class_quotas(_user(subscription_status="active")) == dashboard.SUBSCRIBED_CLASS_QUOTAS
    assert sum(dashboard.SUBSCRIBED_CLASS_QUOTAS.values()) == dashboard.SNIPE_TIER_CAPS["subscribed"]

    assert dashboard._class_quotas(_user(is_superuser=True)) == dashboard.SUPERUSER_CLASS_QUOTAS
    assert sum(dashboard.SUPERUSER_CLASS_QUOTAS.values()) == dashboard.SNIPE_TIER_CAPS["superuser"]


async def _free_tier_user_db(tmp_path, db_name="realm_lock.db"):
    """A real SQLite-backed User row for testing _enforce_realm_lock's
    actual DB-level atomicity -- FAKE_USER (used by every other test in
    this file, and subscribed anyway) is never persisted to a real row, so
    it can't exercise the `UPDATE ... WHERE locked_sell_realm IS NULL` path
    at all (there'd be no row for it to match). Returns (session_factory,
    user_id); caller is responsible for disposing the engine."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / db_name}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(email="free@example.com", hashed_password="x", is_active=True,
                   is_superuser=False, is_verified=True, subscription_status=None)
        session.add(user)
        await session.commit()
        user_id = user.id
    return engine, session_factory, user_id


def test_enforce_realm_lock_first_query_sets_lock(tmp_path):
    async def run():
        engine, session_factory, user_id = await _free_tier_user_db(tmp_path)
        async with session_factory() as session:
            user = await session.get(User, user_id)
            await dashboard._enforce_realm_lock(user, 100, session)
            assert user.locked_sell_realm == 100
        await engine.dispose()
    asyncio.run(run())


def test_enforce_realm_lock_same_realm_repeat_is_ok(tmp_path):
    async def run():
        engine, session_factory, user_id = await _free_tier_user_db(tmp_path)
        async with session_factory() as session:
            user = await session.get(User, user_id)
            await dashboard._enforce_realm_lock(user, 100, session)
            await dashboard._enforce_realm_lock(user, 100, session)  # no error, same realm
            assert user.locked_sell_realm == 100
        await engine.dispose()
    asyncio.run(run())


def test_enforce_realm_lock_different_realm_raises_403(tmp_path):
    async def run():
        engine, session_factory, user_id = await _free_tier_user_db(tmp_path)
        async with session_factory() as session:
            user = await session.get(User, user_id)
            await dashboard._enforce_realm_lock(user, 100, session)
            with pytest.raises(HTTPException) as exc_info:
                await dashboard._enforce_realm_lock(user, 200, session)
            assert exc_info.value.status_code == 403
        await engine.dispose()
    asyncio.run(run())


def test_enforce_realm_lock_concurrent_requests_only_one_wins(tmp_path):
    """Real regression test for the TOCTOU race (2026-07-31, human report
    during a bug audit): the old read-then-write ORM pattern
    (`if user.locked_sell_realm is None: user.locked_sell_realm = sell;
    commit`) let two concurrent first-ever requests -- for two *different*
    realms -- both observe locked_sell_realm as None (each from its own
    freshly-loaded User object) before either committed, so both proceeded
    to lock in (and query) their own realm, defeating the lock's whole
    purpose. Simulates the race directly with two independent sessions
    against the same real row: both load the user while it's still
    unlocked, one "wins" first; the second, still holding its now-*stale*
    None-valued user object, must still correctly detect the real lock
    (via a re-fetch, not its own stale read) and raise 403."""
    async def run():
        engine, session_factory, user_id = await _free_tier_user_db(tmp_path)
        async with session_factory() as session_a, session_factory() as session_b:
            user_a = await session_a.get(User, user_id)
            user_b = await session_b.get(User, user_id)
            assert user_a.locked_sell_realm is None
            assert user_b.locked_sell_realm is None  # both loaded before either lock is set

            await dashboard._enforce_realm_lock(user_a, 100, session_a)
            assert user_a.locked_sell_realm == 100

            # user_b is still stale (loaded before session_a's commit) -- the
            # old buggy code would read user_b.locked_sell_realm as None here
            # and incorrectly let this "win" too, locking realm 200 as well.
            with pytest.raises(HTTPException) as exc_info:
                await dashboard._enforce_realm_lock(user_b, 200, session_b)
            assert exc_info.value.status_code == 403
            assert user_b.locked_sell_realm == 100  # refreshed to the real value, not left stale
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("is_superuser,subscription_status,expected_cap", [
    (False, None, 250),
    (False, "active", 2000),
    (True, None, 10000),
])
def test_api_snipes_clamps_top_to_tier_cap(data_dir, tmp_path, monkeypatch, is_superuser, subscription_status, expected_cap):
    """The client can request any `top` it likes (dashboard.html always asks
    for its own BATCH_TOP ceiling) -- the server is what actually enforces
    the real per-tier row budget, regardless of what was requested."""
    run_diff(monkeypatch)
    # The free-tier case (only) reaches _enforce_realm_lock's real DB write
    # path -- subscribed/superuser return early via has_active_subscription.
    # That path now needs a genuinely persisted User row and the *same*
    # database as its own session dependency (2026-07-31, see
    # _enforce_realm_lock's docstring) -- a bare in-memory _user() object
    # (fine for every other tier, and for this test's own top-clamping
    # assertion) has no real row behind it, so get_async_session is
    # overridden here too instead of relying on bypass_get_async_session's
    # own test DB, which this test has no handle to add a row into.
    is_free_tier = not is_superuser and subscription_status != "active"
    if is_free_tier:
        engine, session_factory, user_id = asyncio.run(_free_tier_user_db(tmp_path))

        async def override_session():
            async with session_factory() as session:
                yield session

        async def override_user():
            async with session_factory() as session:
                return await session.get(User, user_id)
        dashboard.app.dependency_overrides[get_async_session] = override_session
        dashboard.app.dependency_overrides[auth.current_active_user] = override_user
    else:
        dashboard.app.dependency_overrides[auth.current_active_user] = \
            lambda: _user(is_superuser=is_superuser, subscription_status=subscription_status)
    try:
        captured = {}
        real_find_snipes = snipe_check.find_snipes

        def spy(*args, **kwargs):
            captured["top"] = kwargs.get("top")
            return real_find_snipes(*args, **kwargs)
        monkeypatch.setattr(dashboard.snipe_check, "find_snipes", spy)

        r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                              "top": 999999})
        assert r.status_code == 200
        assert captured["top"] == expected_cap
    finally:
        dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
        if is_free_tier:
            dashboard.app.dependency_overrides.pop(get_async_session, None)


def _write_single_ilvl_fixture(tmp_path, monkeypatch, bk):
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(10, T1, item_id=101, buyout=20_000, bonus_key=bk),  # current sell-realm listing: 2g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100, bonus_key=bk)]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")
    run_diff(monkeypatch)


def test_api_snipes_variant_shows_ilvl_when_plausible(tmp_path, monkeypatch):
    """A modifier-28 value in the same ballpark as the item's own catalog
    level (here 636 vs a base of 600, well within the 5x plausibility
    multiple) should render as "ilvl NNN" -- this is the legitimate case
    (modern BoE gear whose bonus list scales its ilvl)."""
    bk = "b:6652,10844|m:28=636,29=32"
    _write_single_ilvl_fixture(tmp_path, monkeypatch, bk)
    stub_item_details(monkeypatch, level=600)

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["variant"] == "ilvl 636"
    assert row["variant_raw"] == bk


def test_api_snipes_variant_falls_back_when_ilvl_implausible(tmp_path, monkeypatch):
    """Reproduces the reported bug: a classic fixed-stat item (base catalog
    level ~34, like Amani Hex Stick) with a modifier-28 value of 1112 --
    wildly outside any plausible ilvl for that item -- must NOT be labeled
    "ilvl 1112". Falls back to the bonus-count summary instead."""
    bk = "b:6652,10844,12804,13335,13577|m:28=1112,29=36,30=32"
    _write_single_ilvl_fixture(tmp_path, monkeypatch, bk)
    stub_item_details(monkeypatch, level=34)

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["variant"] == "5 bonuses"
    assert "ilvl" not in row["variant"]
    assert row["variant_raw"] == bk


def test_api_snipes_variant_falls_back_when_ilvl_within_ratio_but_absurd(tmp_path, monkeypatch):
    """Reproduces a second, different bug (caught live 2026-07-25): item
    237468 (Nightfall Executioner's Girdle, a modern raid item, base 610)
    showed "ilvl 3031" -- INSIDE the 5x ratio (610*5=3050) but obviously not
    a real item level (every live listing for that item carried modifier 28
    set to 3031 or 2462, nothing else, suggesting it isn't ilvl at all for
    this item). The ratio check alone isn't tight enough for high-base-level
    items; ILVL_ABSOLUTE_MAX must also reject this."""
    bk = "b:1504,6652,10844,12265,12921|m:28=3031"
    _write_single_ilvl_fixture(tmp_path, monkeypatch, bk)
    stub_item_details(monkeypatch, level=610)

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["variant"] == "5 bonuses"
    assert "ilvl" not in row["variant"]
    assert row["variant_raw"] == bk


def test_api_snipes_carries_item_class_when_names_resolved(data_dir, monkeypatch):
    """item_class/item_subclass back the dashboard's client-side item-class
    filter -- must ride along on every row when names=true, same as icon/
    quality_color."""
    run_diff(monkeypatch)
    stub_item_details(monkeypatch, item_class=2, item_subclass=7)  # Weapon/Sword
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["item_class"] == 2
    assert row["item_subclass"] == 7


def test_api_snipes_carries_quality_tier_for_rarity_filter(data_dir, monkeypatch):
    """quality (the tier name, e.g. "EPIC") backs the dashboard's rarity
    filter -- must ride along on every row when names=true, same as
    quality_color (the ring color derived from the same tier)."""
    run_diff(monkeypatch)
    stub_item_details(monkeypatch, quality="EPIC")
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["quality"] == "EPIC"
    assert row["quality_color"] == "#a335ee"


def test_api_snipes_flags_profession_items_for_unique_transmog_toggle(data_dir, monkeypatch):
    """is_profession_item mirrors find_snipes()'s NON_TRANSMOG_INVENTORY_TYPES
    check exactly, so the dashboard's client-side "Unique transmog only"
    toggle can reproduce --max-appearance-sources' exclusion without a
    server round trip."""
    run_diff(monkeypatch)
    stub_item_details(monkeypatch, inventory_type="PROFESSION_TOOL")
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["is_profession_item"] is True


def test_api_snipes_omits_item_class_without_names(data_dir, monkeypatch):
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3})
    row = r.json()["rows"][0]
    assert "item_class" not in row
    assert "item_subclass" not in row
    assert "is_profession_item" not in row


def test_api_snipes_flags_sus_item_for_old_neck_item(data_dir, monkeypatch):
    """sus_item_suspect mirrors snipe_check.is_sus_item() exactly --
    an old NECK item (ilvl well under LEGACY_JEWELRY_ILVL_MAX) must flag
    true, the same live-verified shape as Charm of Potent and Powerful
    Passions (item 27982, ilvl 26, NECK -- see snipe_check.py)."""
    run_diff(monkeypatch)
    stub_item_details(monkeypatch, inventory_type="NECK", level=26)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["sus_item_suspect"] is True


def test_api_snipes_sus_item_false_for_current_tier_jewelry(data_dir, monkeypatch):
    run_diff(monkeypatch)
    stub_item_details(monkeypatch, inventory_type="NECK", level=610)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["sus_item_suspect"] is False


def test_api_snipes_sus_item_false_for_non_jewelry_slot(data_dir, monkeypatch):
    """A low-ilvl item in a non-jewelry slot (e.g. a leveling HEAD item)
    must not be flagged -- the ilvl cutoff alone isn't the whole rule, and
    item 101 (this fixture's default) isn't in CLASS_STARTER_ARMOR_ITEM_IDS
    either."""
    run_diff(monkeypatch)
    stub_item_details(monkeypatch, inventory_type="HEAD", level=26)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["sus_item_suspect"] is False


def test_api_snipes_omits_sus_item_without_names(data_dir, monkeypatch):
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3})
    row = r.json()["rows"][0]
    assert "sus_item_suspect" not in row


def test_api_snipes_flags_sus_item_for_class_starter_armor_item_id(tmp_path, monkeypatch):
    """The item_id itself (not just inventory_type/ilvl) can trigger
    sus_item_suspect via CLASS_STARTER_ARMOR_ITEM_IDS -- uses Paladin's
    Girdle's real id (187726, Plate, WAIST -- confirmed live 2026-07-31, see
    snipe_check.py) in a custom fixture, since the shared data_dir fixture's
    default item_id (101) isn't in that set. WAIST isn't a jewelry slot, so
    this only passes if the id-based path works independently of the
    ilvl+inventory_type jewelry rule."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    STARTER_ITEM = 187726

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103),
            snap_row(1, T1, item_id=STARTER_ITEM, buyout=100_000)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_A, item_id=STARTER_ITEM, buyout=10_000, auction_id=100)],
        schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    stub_item_details(monkeypatch, inventory_type="WAIST", level=1)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "names": True})
    row = r.json()["rows"][0]
    assert row["item_id"] == STARTER_ITEM
    assert row["sus_item_suspect"] is True


def test_api_snipes_variant_falls_back_without_names_resolved(tmp_path, monkeypatch):
    """Without names=true there's no base_level to check the claim against,
    so the smarter ilvl label is skipped entirely rather than shown unverified."""
    bk = "b:6652,10844|m:28=636,29=32"
    _write_single_ilvl_fixture(tmp_path, monkeypatch, bk)

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3})
    row = r.json()["rows"][0]
    assert row["variant"] == "2 bonuses"


def test_api_snipes_pet_variant_unaffected_by_ilvl_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    PET_ITEM = 82800

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(1, T1, item_id=PET_ITEM, buyout=500_000, pet_species_id=2, pet_quality_id=1),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, PET_ITEM, buyout=100_000, auction_id=200,
                             pet_species_id=2, pet_quality_id=1)]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.1})
    row = r.json()["rows"][0]
    assert row["variant"] == "pet:2/1"
    # Backs dashboard.html's groupKey() (2026-07-26) -- without these, every
    # pet species/quality would wrongly collapse into one display group
    # since they all share item_id 82800.
    assert row["pet_species_id"] == 2
    assert row["pet_quality_id"] == 1


def test_api_snipes_caveat_present_even_when_empty(data_dir, monkeypatch):
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.99})
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["caveat"] == snipe_check.CAVEAT


def test_api_snipes_missing_events_returns_400(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    (tmp_path / "listings").mkdir()
    r = client.get("/api/snipes", params={"sell": 424242})
    assert r.status_code == 400


def test_api_snipes_missing_listings_returns_400(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    (events_dir / "424242.parquet").touch()
    r = client.get("/api/snipes", params={"sell": 424242})
    assert r.status_code == 400


def test_api_snipes_rejects_bad_sort(data_dir, monkeypatch):
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "sort": "nonsense"})
    assert r.status_code == 400


def test_api_snipes_respects_min_gold_and_max_gold(data_dir, monkeypatch):
    """The one qualifying listing is 1g (10_000 copper) -- min_gold above
    that or max_gold below it should filter it out."""
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "min_gold": 2})
    assert r.json()["rows"] == []

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "max_gold": 0.5})
    assert r.json()["rows"] == []

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3,
                                          "max_gold": 5})
    assert r.json()["count"] == 1


def test_api_realms_lists_collected_realms(data_dir):
    """A realm shows up from having at least one snapshot alone -- since
    2026-07-25 nothing runs diff_snapshots.py automatically (see
    collect_all.py), so requiring an events file would make this list
    permanently empty."""
    r = client.get("/api/realms")
    assert r.status_code == 200
    realms = r.json()["realms"]
    assert {"id": SELL_CR, "name": f"Realm {SELL_CR}", "slug": f"realm-{SELL_CR}"} in realms


def test_api_realms_empty_when_nothing_collected(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    r = client.get("/api/realms")
    assert r.status_code == 200
    assert r.json()["realms"] == []


def test_api_status_reports_last_modified(data_dir):
    r = client.get("/api/status", params={"sell": SELL_CR})
    assert r.status_code == 200
    body = r.json()
    assert body["last_modified"] == "Thu, 23 Jul 2026 11:20:39 GMT"
    assert body["listings_updated"] is not None
    assert body["has_data"] is True  # a snapshot exists, regardless of events


def test_api_status_handles_unknown_realm(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    r = client.get("/api/status", params={"sell": 424242})
    assert r.status_code == 200
    body = r.json()
    assert body["last_modified"] is None
    assert body["has_data"] is False


def _drop_auth_overrides():
    """The autouse bypass_auth fixture stubs current_active_user/
    current_subscribed_user for every test in this module, which would
    silently hide a missing @app.get(...) auth dependency on a route meant
    to be public. Pop the overrides so these specific tests exercise the
    real (absent) auth requirement -- restored automatically by
    bypass_auth's own teardown, which pop()s with a None default."""
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)
    dashboard.app.dependency_overrides.pop(auth.current_subscribed_user, None)


def test_pricing_page_served_without_auth():
    """Public like /log -- a pricing page a visitor can't see before
    registering would defeat its own purpose."""
    _drop_auth_overrides()
    r = client.get("/pricing", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_index_serves_landing_page():
    """/ became the public marketing landing page (2026-07-26) -- the
    sniper tool itself moved to /snipes, see the test below. Distinguished
    by id="rows" (the real dynamic ledger body), not by <table> presence --
    the landing page legitimately has its own static sample table too."""
    r = client.get("/")
    assert r.status_code == 200
    assert b"Realm Arbitrage" in r.content
    assert b'id="rows"' not in r.content


def test_snipes_serves_dashboard_html():
    r = client.get("/snipes")
    assert r.status_code == 200
    assert b'id="rows"' in r.content
    assert b"Realm Arbitrage" in r.content


def test_html_pages_are_not_heuristically_cached():
    """A real live bug, 2026-07-31: FileResponse sets Last-Modified/ETag but
    no Cache-Control, so a browser can keep serving a page from before the
    latest deploy on a plain reload with no way to know it's stale -- a user
    kept seeing a bug fixed server-side because their browser silently reused
    a cached dashboard.html from before that fix shipped. no_cache_html()
    forces revalidation on every load (a cheap 304 when unchanged, not a full
    re-download) without touching the API's own JSON responses."""
    r = client.get("/snipes")
    assert r.headers["cache-control"] == "no-cache"
    r = client.get("/api/config")
    assert "cache-control" not in {k.lower() for k in r.headers}


def test_api_config_reports_default_sell():
    dashboard.app.state.default_sell = 1403
    r = client.get("/api/config")
    assert r.json()["default_sell"] == 1403


def test_api_me_includes_nickname_defaulting_to_none():
    """Backs snipeboard.html's decision to prompt for a nickname the first
    time an account tries to post (see forum.py) -- None until set."""
    r = client.get("/api/me")
    assert r.json()["nickname"] is None


def test_update_nickname_sets_and_returns_it():
    r = client.patch("/api/me/nickname", json={"nickname": "  Snipehunter  "})
    assert r.status_code == 200
    assert r.json()["nickname"] == "Snipehunter"  # stripped


def test_update_nickname_rejects_empty():
    r = client.patch("/api/me/nickname", json={"nickname": "   "})
    assert r.status_code == 400


def test_update_nickname_rejects_too_long():
    r = client.patch("/api/me/nickname", json={"nickname": "x" * 51})
    assert r.status_code == 400


def test_update_nickname_requires_login():
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)
    try:
        r = client.patch("/api/me/nickname", json={"nickname": "Snipehunter"})
        assert r.status_code == 401
    finally:
        dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER


def test_parse_variant_extracts_ilvl_and_ignores_other_modifiers():
    assert dashboard._parse_variant("b:1,2,3|m:28=636,9=1") == {"ilvl": "636", "bonus_count": 3}


def test_parse_variant_falls_back_to_bonus_count_without_ilvl():
    assert dashboard._parse_variant("b:1,2|m:9=1,10=2") == {"ilvl": None, "bonus_count": 2}


def test_parse_variant_handles_empty_string():
    assert dashboard._parse_variant("") == {"ilvl": None, "bonus_count": 0}


def test_realm_info_caches_across_calls(monkeypatch):
    calls = []

    def fake_realms(cr_id):
        calls.append(cr_id)
        return [{"name": "Draenor", "slug": "draenor", "category": "English"}]

    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms", fake_realms)
    assert dashboard._realm_info(1403) == {"name": "Draenor", "slug": "draenor", "category": "English"}
    assert dashboard._realm_info(1403) == {"name": "Draenor", "slug": "draenor", "category": "English"}
    assert calls == [1403]  # second call served from cache, no re-fetch


def test_realm_info_returns_none_fields_on_failure(monkeypatch):
    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms", lambda cr_id: (_ for _ in ()).throw(RuntimeError))
    assert dashboard._realm_info(1403) == {"name": None, "slug": None, "category": None}


def test_poll_interval_is_tight_inside_the_expected_publish_window():
    """Real production data (7 consecutive Draenor retrievals landing within
    ~1.5 minutes of each other) showed AH data reliably arrives around
    :19-:20 past the hour for at least this realm -- poll every
    TIGHT_INTERVAL_SECONDS through a generous window around that mark
    instead of waiting up to the full 10-minute baseline."""
    import datetime
    inside = datetime.datetime(2026, 7, 23, 21, 20, 0, tzinfo=datetime.timezone.utc)
    assert dashboard._next_poll_interval_seconds(inside) == dashboard.TIGHT_INTERVAL_SECONDS


def test_poll_interval_is_normal_outside_the_expected_publish_window():
    import datetime
    outside = datetime.datetime(2026, 7, 23, 21, 45, 0, tzinfo=datetime.timezone.utc)
    assert dashboard._next_poll_interval_seconds(outside) == dashboard.COLLECTION_INTERVAL_SECONDS


def test_poll_interval_window_boundaries():
    import datetime
    start = datetime.datetime(2026, 7, 23, 21, dashboard.TIGHT_WINDOW_START_MINUTE, 0,
                              tzinfo=datetime.timezone.utc)
    just_before_end = datetime.datetime(2026, 7, 23, 21, dashboard.TIGHT_WINDOW_END_MINUTE - 1, 59,
                                        tzinfo=datetime.timezone.utc)
    at_end = datetime.datetime(2026, 7, 23, 21, dashboard.TIGHT_WINDOW_END_MINUTE, 0,
                               tzinfo=datetime.timezone.utc)
    assert dashboard._next_poll_interval_seconds(start) == dashboard.TIGHT_INTERVAL_SECONDS
    assert dashboard._next_poll_interval_seconds(just_before_end) == dashboard.TIGHT_INTERVAL_SECONDS
    assert dashboard._next_poll_interval_seconds(at_end) == dashboard.COLLECTION_INTERVAL_SECONDS
