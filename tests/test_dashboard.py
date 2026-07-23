"""Tests for dashboard.py: the FastAPI read-only web layer over
snipe_check.find_snipes(). Mirrors test_snipe_check.py's synthetic-pipeline
fixture style -- real duckdb/pyarrow, no mocking, only the HTTP/JSON
boundary is new relative to the existing test conventions."""
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

import analyze
import auth
import blizz
import dashboard
import diff_snapshots
import item_names
import snipe_check
from db import User
from fetch_snapshot import SCHEMA
from scan_region import LISTING_SCHEMA

SELL_CR = 9999
BUY_CR_A = 1111
T0, T1 = 1_700_000_000, 1_700_003_600

client = TestClient(dashboard.app)

FAKE_USER = User(email="test@example.com", hashed_password="x",
                 is_active=True, is_superuser=False, is_verified=True)


@pytest.fixture(autouse=True)
def bypass_auth():
    """These tests exercise snipe_check/dashboard business logic, not auth
    itself (see test_auth.py for that) -- override FastAPI's dependency
    injection to skip real login, the standard FastAPI testing pattern,
    rather than requiring every test here to register+log in a real user."""
    dashboard.app.dependency_overrides[auth.current_active_user] = lambda: FAKE_USER
    yield
    dashboard.app.dependency_overrides.pop(auth.current_active_user, None)


@pytest.fixture(autouse=True)
def stub_realm_info(monkeypatch):
    """dashboard._realm_info() calls the live Blizzard API -- stub it and
    reset the in-process cache so tests stay offline and deterministic."""
    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms",
                        lambda cr_id: [{"name": f"Realm {cr_id}", "slug": f"realm-{cr_id}"}])


@pytest.fixture(autouse=True)
def isolate_item_names_cache(tmp_path, monkeypatch):
    """dashboard.py's `names=true` path instantiates a real item_names.NameCache,
    which reads/writes CACHE_PATH (data/item_names.json) unless redirected --
    autouse so no test can accidentally read from or write into the real,
    gitignored project cache."""
    monkeypatch.setattr(item_names, "CACHE_PATH", tmp_path / "item_names_test_cache.json")


def stub_item_details(monkeypatch, name="Stub Item", quality="EPIC", level=600, icon="https://example/icon.jpg"):
    """item_names.NameCache hits the live Blizzard API -- stub the network
    edge so names=true tests stay offline and deterministic."""
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": name, "quality": quality, "level": level})
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
    # item 101 sells twice (20_000 and 22_000 copper) -> p25 sold price = 20_500
    prev = [
        snap_row(1, T0, item_id=101, buyout=20_000),
        snap_row(4, T0, item_id=101, buyout=22_000),
        snap_row(3, T0, item_id=103),   # survives -> no event
    ]
    curr = [snap_row(3, T1, item_id=103)]
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
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3, "min_per_day": 0.1})
    assert r.status_code == 200
    body = r.json()
    assert body["caveat"] == snipe_check.CAVEAT
    assert body["count"] == 1
    assert body["rows"][0]["item_id"] == 101
    assert body["rows"][0]["buy_realm"] == BUY_CR_A
    assert body["rows"][0]["buy_copper"] == 10_000
    assert body["rows"][0]["buy_realm_name"] == f"Realm {BUY_CR_A}"
    assert body["region"] == blizz.REGION
    assert body["sell_realm_slug"] == f"realm-{SELL_CR}"


def _write_single_ilvl_fixture(tmp_path, monkeypatch, bk):
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [
        snap_row(1, T0, item_id=101, buyout=20_000, bonus_key=bk),
        snap_row(4, T0, item_id=101, buyout=22_000, bonus_key=bk),
        snap_row(3, T0, item_id=103),
    ]
    curr = [snap_row(3, T1, item_id=103)]
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
                                          "min_per_day": 0.1, "names": True})
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
                                          "min_per_day": 0.1, "names": True})
    row = r.json()["rows"][0]
    assert row["variant"] == "5 bonuses"
    assert "ilvl" not in row["variant"]
    assert row["variant_raw"] == bk


def test_api_snipes_variant_falls_back_without_names_resolved(tmp_path, monkeypatch):
    """Without names=true there's no base_level to check the claim against,
    so the smarter ilvl label is skipped entirely rather than shown unverified."""
    bk = "b:6652,10844|m:28=636,29=32"
    _write_single_ilvl_fixture(tmp_path, monkeypatch, bk)

    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.3, "min_per_day": 0.1})
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
    prev = [
        snap_row(1, T0, item_id=PET_ITEM, buyout=500_000, pet_species_id=2, pet_quality_id=1),
        snap_row(2, T0, item_id=PET_ITEM, buyout=550_000, pet_species_id=2, pet_quality_id=1),
        snap_row(3, T0, item_id=103),
    ]
    curr = [snap_row(3, T1, item_id=103)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, PET_ITEM, buyout=100_000, auction_id=200,
                             pet_species_id=2, pet_quality_id=1)]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.1, "min_per_day": 0.1})
    row = r.json()["rows"][0]
    assert row["variant"] == "pet:2/1"


def test_api_snipes_caveat_present_even_when_empty(data_dir, monkeypatch):
    run_diff(monkeypatch)
    r = client.get("/api/snipes", params={"sell": SELL_CR, "min_discount": 0.99, "min_per_day": 0.1})
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


def test_api_status_reports_last_modified(data_dir):
    r = client.get("/api/status", params={"sell": SELL_CR})
    assert r.status_code == 200
    body = r.json()
    assert body["last_modified"] == "Thu, 23 Jul 2026 11:20:39 GMT"
    assert body["listings_updated"] is not None
    assert body["events_exist"] is False  # diff not yet run in this test


def test_api_status_handles_unknown_realm(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    r = client.get("/api/status", params={"sell": 424242})
    assert r.status_code == 200
    body = r.json()
    assert body["last_modified"] is None
    assert body["events_exist"] is False


def test_index_serves_html():
    r = client.get("/")
    assert r.status_code == 200
    assert b"<table" in r.content
    assert b"AH Snipe Dashboard" in r.content


def test_api_config_reports_default_sell():
    dashboard.app.state.default_sell = 1403
    r = client.get("/api/config")
    assert r.json()["default_sell"] == 1403


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
        return [{"name": "Draenor", "slug": "draenor"}]

    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms", fake_realms)
    assert dashboard._realm_info(1403) == {"name": "Draenor", "slug": "draenor"}
    assert dashboard._realm_info(1403) == {"name": "Draenor", "slug": "draenor"}
    assert calls == [1403]  # second call served from cache, no re-fetch


def test_realm_info_returns_none_fields_on_failure(monkeypatch):
    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms", lambda cr_id: (_ for _ in ()).throw(RuntimeError))
    assert dashboard._realm_info(1403) == {"name": None, "slug": None}
