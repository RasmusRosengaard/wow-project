"""Tests for collect_all.py: the server-side collection cycle that replaces
the human's local run_cycle.py + Task Scheduler now that the app is hosted.
Mirrors test_run_cycle.py's fixture style -- fake HTTP responses, no real
network, real duckdb/pyarrow for the data layer."""
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import blizz
import collect_all
import fetch_snapshot
import item_names
import scan_region
import tsm
from fetch_snapshot import SCHEMA
from scan_region import LISTING_SCHEMA

FULL_POP_CR = 1403   # e.g. Draenor
HIGH_POP_CR = 1096
LOW_POP_CR = 9999


class FakeResponse:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = b"{}"
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def reset_realm_cache(monkeypatch):
    """deep_collect_realm_ids() caches at module level -- reset between
    tests so one test's monkeypatched population data can't leak into another."""
    monkeypatch.setattr(collect_all, "_deep_collect_realm_ids", None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for mod in (fetch_snapshot, scan_region, collect_all):
        monkeypatch.setattr(mod, "DATA", tmp_path)
    # tsm.CACHE_PATH is computed once from tsm.DATA at import time, not
    # re-derived dynamically -- redirecting tsm.DATA alone wouldn't move it
    # (same reason item_names.py/appearance.py's own test fixtures patch
    # CACHE_PATH directly rather than DATA). Without this,
    # collect_all()'s new TSM refresh step would read/write the real
    # project's data/tsm_sale_rates.json during tests.
    monkeypatch.setattr(tsm, "CACHE_PATH", tmp_path / "tsm_sale_rates.json")
    # No live network in this suite (see module docstring) -- collect_all()
    # now calls tsm.SaleRateCache().refresh_if_stale() every cycle; without
    # stubbing this, every test using `env` would make a real HTTP call to
    # TSM's public data feed.
    monkeypatch.setattr(tsm, "_fetch_csv", lambda: {})
    return tmp_path


def test_deep_collect_realm_ids_filters_to_full_and_high_pop(monkeypatch):
    monkeypatch.setattr(blizz, "list_connected_realms",
                        lambda: [FULL_POP_CR, HIGH_POP_CR, LOW_POP_CR])
    pop = {FULL_POP_CR: "FULL", HIGH_POP_CR: "HIGH", LOW_POP_CR: "LOW"}
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: pop[cr])

    assert sorted(collect_all.deep_collect_realm_ids()) == sorted([FULL_POP_CR, HIGH_POP_CR])


def test_deep_collect_realm_ids_caches_across_calls(monkeypatch):
    calls = []

    def fake_list():
        calls.append(1)
        return [FULL_POP_CR]

    monkeypatch.setattr(blizz, "list_connected_realms", fake_list)
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")

    assert collect_all.deep_collect_realm_ids() == [FULL_POP_CR]
    assert collect_all.deep_collect_realm_ids() == [FULL_POP_CR]
    assert len(calls) == 1  # second call served from cache


def test_deep_collect_realm_ids_force_refresh_requeries(monkeypatch):
    calls = []

    def fake_list():
        calls.append(1)
        return [FULL_POP_CR]

    monkeypatch.setattr(blizz, "list_connected_realms", fake_list)
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")

    collect_all.deep_collect_realm_ids()
    collect_all.deep_collect_realm_ids(force_refresh=True)
    assert len(calls) == 2


def test_deep_collect_realm_ids_survives_one_bad_lookup(monkeypatch):
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR, LOW_POP_CR])

    def fake_pop(cr):
        if cr == LOW_POP_CR:
            raise RuntimeError("boom")
        return "FULL"

    monkeypatch.setattr(blizz, "connected_realm_population", fake_pop)
    assert collect_all.deep_collect_realm_ids() == [FULL_POP_CR]


def snap_row(auction_id, ts, item_id=101, buyout=20_000):
    return {
        "snapshot_ts": ts, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": "", "pet_species_id": None, "pet_quality_id": None,
        "pet_level": None, "buyout": buyout, "bid": None,
        "quantity": 1, "time_left": "VERY_LONG",
    }


def test_prune_to_latest_keeps_only_newest(env):
    """Pricing only ever reads the latest snapshot (see collect_all.py's
    module docstring) -- prune_to_latest() replaced the old day-based
    retention 2026-07-25 once that stopped being necessary."""
    snap_dir = env / "snapshots" / str(FULL_POP_CR)
    snap_dir.mkdir(parents=True)
    now = int(time.time())
    for ts in (now - 7200, now - 3600, now):
        pq.write_table(pa.Table.from_pylist([snap_row(1, ts)], schema=SCHEMA),
                       snap_dir / f"{ts}.parquet")

    removed = collect_all.prune_to_latest(FULL_POP_CR)
    assert removed == 2
    remaining = {int(p.stem) for p in snap_dir.glob("*.parquet")}
    assert remaining == {now}


def test_prune_to_latest_noop_with_one_snapshot(env):
    snap_dir = env / "snapshots" / str(FULL_POP_CR)
    snap_dir.mkdir(parents=True)
    ts = int(time.time())
    pq.write_table(pa.Table.from_pylist([snap_row(1, ts)], schema=SCHEMA),
                   snap_dir / f"{ts}.parquet")

    assert collect_all.prune_to_latest(FULL_POP_CR) == 0
    assert len(list(snap_dir.glob("*.parquet"))) == 1


def test_collect_all_only_deep_collects_high_pop_but_sweeps_everyone(env, monkeypatch):
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR, LOW_POP_CR])
    pop = {FULL_POP_CR: "FULL", LOW_POP_CR: "LOW"}
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: pop[cr])
    monkeypatch.setattr(fetch_snapshot, "api_get", lambda *a, **k: FakeResponse(
        200, headers={"Last-Modified": "Mon, 14 Jul 2026 12:00:00 GMT"},
        payload={"auctions": [{"id": 1, "item": {"id": 101}, "buyout": 20_000,
                              "quantity": 1, "time_left": "VERY_LONG"}]}))
    # scan_region sweeps every realm from blizz.list_connected_realms(), unscoped
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [FULL_POP_CR, LOW_POP_CR])
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload={"auctions": []}))

    summary = collect_all.collect_all()

    assert summary["realms"] == 1  # only the FULL-pop realm counted for deep collection
    assert (env / "snapshots" / str(FULL_POP_CR)).exists()
    assert not (env / "snapshots" / str(LOW_POP_CR)).exists()  # low-pop never deep-collected
    # region sweep is unscoped -- both realms get a listings file regardless of pop
    assert (env / "listings" / f"{FULL_POP_CR}.parquet").exists()
    assert (env / "listings" / f"{LOW_POP_CR}.parquet").exists()


def test_collect_all_skips_prune_when_no_new_snapshot_arrived(env, monkeypatch):
    """A cycle where Blizzard hasn't published a new dump yet (fetch_once
    returns None, e.g. a 304) must leave existing snapshot files alone --
    pruning to latest only makes sense right after a genuinely new one
    lands."""
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR])
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [])

    snap_dir = env / "snapshots" / str(FULL_POP_CR)
    snap_dir.mkdir(parents=True)
    now = int(time.time())
    for ts in (now - 3600, now):
        pq.write_table(pa.Table.from_pylist([snap_row(1, ts)], schema=SCHEMA),
                       snap_dir / f"{ts}.parquet")

    monkeypatch.setattr(fetch_snapshot, "fetch_once", lambda cr: None)  # no new dump this cycle

    summary = collect_all.collect_all()
    assert summary["pruned_snapshots"] == 0
    assert len(list(snap_dir.glob("*.parquet"))) == 2  # untouched


def test_collect_all_prunes_to_latest_after_new_snapshot(env, monkeypatch):
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR])
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [])

    snap_dir = env / "snapshots" / str(FULL_POP_CR)
    snap_dir.mkdir(parents=True)
    old_ts = int(time.time()) - 3600
    pq.write_table(pa.Table.from_pylist([snap_row(1, old_ts)], schema=SCHEMA),
                   snap_dir / f"{old_ts}.parquet")
    new_ts = old_ts + 3600

    def fake_fetch_once(cr):
        out = snap_dir / f"{new_ts}.parquet"
        pq.write_table(pa.Table.from_pylist([snap_row(2, new_ts)], schema=SCHEMA), out)
        return out

    monkeypatch.setattr(fetch_snapshot, "fetch_once", fake_fetch_once)

    summary = collect_all.collect_all()
    assert summary["pruned_snapshots"] == 1
    remaining = {int(p.stem) for p in snap_dir.glob("*.parquet")}
    assert remaining == {new_ts}


def test_collect_all_survives_one_realm_failing(env, monkeypatch):
    ok_cr, bad_cr = FULL_POP_CR, HIGH_POP_CR
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [ok_cr, bad_cr])
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")

    def fake_fetch_once(cr):
        if cr == bad_cr:
            raise RuntimeError("simulated collector failure")
        return None

    monkeypatch.setattr(fetch_snapshot, "fetch_once", fake_fetch_once)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [])

    summary = collect_all.collect_all()
    assert summary["failed"] == [bad_cr]
    assert summary["realms"] == 2


def listing_row(cr, item_id, bonus_key=""):
    return {
        "cr_id": cr, "fetched_ts": int(time.time()), "auction_id": item_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": None, "pet_quality_id": None,
        "pet_level": None, "buyout": 20_000, "bid": None, "quantity": 1,
        "time_left": "VERY_LONG",
    }


def test_prewarm_item_base_levels_noop_when_no_listings(env):
    """No data/listings/*.parquet yet (e.g. before the first sweep ever
    ran) -- must return 0 without erroring, not assume the directory/files
    exist."""
    assert collect_all._prewarm_item_base_levels() == 0


def test_prewarm_item_base_levels_resolves_only_type28_candidates(env, monkeypatch):
    """Added 2026-07-25 alongside snipe_check.MAX_BASE_LEVEL_LOOKUPS_PER_CALL
    -- this is the background half of that fix: resolving item catalog
    levels here, off the request path, so _resolve_base_levels() finds
    them already cached. Only items whose bonus_key actually carries a
    type-28 modifier should trigger a lookup."""
    listings_dir = env / "listings"
    listings_dir.mkdir(parents=True)
    rows = [
        listing_row(1080, 101, bonus_key="m:28=200"),
        listing_row(1080, 102, bonus_key=""),  # no type-28 -- not a candidate
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA),
                    listings_dir / "1080.parquet")

    monkeypatch.setattr(item_names, "CACHE_PATH", env / "item_names_test_cache.json")
    calls = []

    def fake(item_id):
        calls.append(item_id)
        return {"name": "Test Item", "quality": "COMMON", "level": 40,
                "inventory_type": None, "item_class": None, "item_subclass": None}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake)

    candidates = collect_all._prewarm_item_base_levels()
    assert candidates == 1  # only item 101 has a type-28 modifier
    assert calls == [101]


def test_prewarm_item_base_levels_respects_cap(env, monkeypatch):
    listings_dir = env / "listings"
    listings_dir.mkdir(parents=True)
    rows = [listing_row(1080, item_id, bonus_key="m:28=200") for item_id in range(101, 111)]
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA),
                    listings_dir / "1080.parquet")

    monkeypatch.setattr(item_names, "CACHE_PATH", env / "item_names_test_cache.json")
    calls = []

    def fake(item_id):
        calls.append(item_id)
        return {"name": "Test Item", "quality": "COMMON", "level": 40,
                "inventory_type": None, "item_class": None, "item_subclass": None}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake)

    candidates = collect_all._prewarm_item_base_levels(cap=3)
    assert candidates == 10  # all 10 are real candidates...
    assert len(calls) == 3   # ...but only 3 were actually fetched, per the cap


def test_collect_all_includes_prewarm_count_in_summary(env, monkeypatch):
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR])
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")
    monkeypatch.setattr(fetch_snapshot, "fetch_once", lambda cr: None)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [])

    summary = collect_all.collect_all()
    assert summary["base_level_candidates"] == 0  # no listings swept -- nothing to prewarm


def test_collect_all_refreshes_tsm_sale_rates(env, monkeypatch):
    """collect_all() (2026-08-01, human request) must call
    tsm.SaleRateCache().refresh_if_stale() every cycle -- its own internal
    staleness check (tsm.REFRESH_INTERVAL_SECONDS) is what keeps this cheap
    on most calls, not the caller skipping it."""
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR])
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")
    monkeypatch.setattr(fetch_snapshot, "fetch_once", lambda cr: None)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [])
    monkeypatch.setattr(tsm, "_fetch_csv", lambda: {12345: {"sale_rate": 0.5, "sold_per_day": 1.0}})

    summary = collect_all.collect_all()
    assert summary["tsm_refreshed"] is True
    assert tsm.SaleRateCache().get(12345) == {"sale_rate": 0.5, "sold_per_day": 1.0}


def test_collect_all_survives_tsm_refresh_failure(env, monkeypatch):
    """Same "one bad piece never aborts the rest" principle every other
    step in collect_all() already follows."""
    monkeypatch.setattr(blizz, "list_connected_realms", lambda: [FULL_POP_CR])
    monkeypatch.setattr(blizz, "connected_realm_population", lambda cr: "FULL")
    monkeypatch.setattr(fetch_snapshot, "fetch_once", lambda cr: None)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [])

    def raise_error():
        raise RuntimeError("boom")
    monkeypatch.setattr(tsm, "_fetch_csv", raise_error)

    summary = collect_all.collect_all()  # must not raise
    assert summary["tsm_refreshed"] is False
