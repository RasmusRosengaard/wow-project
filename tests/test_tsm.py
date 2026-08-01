"""Tests for tsm.py's SaleRateCache -- no live network in this suite."""
import json

import pytest

import tsm
from tsm import SaleRateCache

SAMPLE_CSV = (
    "itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt\n"
    "12345,Example Item,10000,9500,10200,0.75,3.2,2026-08-01T12:00:00Z\n"
    "67890,Another Item,500,480,510,0.1,0.05,2026-08-01T12:00:00Z\n"
)


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "tsm_sale_rates.json"
    monkeypatch.setattr(tsm, "CACHE_PATH", path)
    return path


def test_fetch_csv_parses_rows(monkeypatch):
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse(SAMPLE_CSV))
    result = tsm._fetch_csv()
    assert result[12345] == {"sale_rate": 0.75, "sold_per_day": 3.2}
    assert result[67890] == {"sale_rate": 0.1, "sold_per_day": 0.05}


def test_fetch_csv_skips_malformed_rows(monkeypatch):
    csv_text = (
        "itemId,name,marketValue,historical,avgSalePrice,saleRate,soldPerDay,updatedAt\n"
        "12345,Example Item,10000,9500,10200,0.75,3.2,2026-08-01T12:00:00Z\n"
        "not-a-number,Bad Row,10000,9500,10200,0.5,1.0,2026-08-01T12:00:00Z\n"
    )
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse(csv_text))
    result = tsm._fetch_csv()
    assert list(result.keys()) == [12345]


def test_fetch_csv_returns_empty_on_non_200(monkeypatch):
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse("", status_code=503))
    assert tsm._fetch_csv() == {}


def test_fetch_csv_returns_empty_on_network_exception(monkeypatch):
    def raise_error(*a, **k):
        raise ConnectionError("no route to host")
    monkeypatch.setattr(tsm.requests, "get", raise_error)
    assert tsm._fetch_csv() == {}


def test_get_returns_none_for_unknown_item(cache_path):
    cache = SaleRateCache()
    assert cache.get(999999) is None


def test_refresh_if_stale_fetches_when_cache_is_empty(cache_path, monkeypatch):
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse(SAMPLE_CSV))
    cache = SaleRateCache()
    did_fetch = cache.refresh_if_stale()
    assert did_fetch is True
    assert cache.get(12345) == {"sale_rate": 0.75, "sold_per_day": 3.2}


def test_refresh_if_stale_skips_when_recently_fetched(cache_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tsm.requests, "get",
                        lambda *a, **k: calls.append(1) or FakeResponse(SAMPLE_CSV))
    cache = SaleRateCache()
    cache.refresh_if_stale()
    assert len(calls) == 1
    did_fetch = cache.refresh_if_stale()  # immediately again -- well within the interval
    assert did_fetch is False
    assert len(calls) == 1  # no second network call


def test_refresh_if_stale_refetches_once_interval_elapses(cache_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tsm.requests, "get",
                        lambda *a, **k: calls.append(1) or FakeResponse(SAMPLE_CSV))
    cache = SaleRateCache()
    cache.refresh_if_stale(interval_seconds=100)
    assert len(calls) == 1
    cache._fetched_at -= 200  # simulate 200s having passed, past the 100s interval
    did_fetch = cache.refresh_if_stale(interval_seconds=100)
    assert did_fetch is True
    assert len(calls) == 2


def test_refresh_if_stale_keeps_old_data_on_failed_fetch(cache_path, monkeypatch):
    """A network error mid-cycle must not wipe out a previously-good cache
    -- serving stale data is better than serving none, same philosophy as
    every other display/enrichment cache in this project."""
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse(SAMPLE_CSV))
    cache = SaleRateCache()
    cache.refresh_if_stale()
    assert cache.get(12345) is not None

    def fail(*a, **k):
        raise ConnectionError("transient failure")
    monkeypatch.setattr(tsm.requests, "get", fail)
    cache._fetched_at -= tsm.REFRESH_INTERVAL_SECONDS + 1  # force staleness
    did_fetch = cache.refresh_if_stale()
    assert did_fetch is False
    assert cache.get(12345) == {"sale_rate": 0.75, "sold_per_day": 3.2}  # still there


def test_refresh_if_stale_saves_to_disk(cache_path, monkeypatch):
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse(SAMPLE_CSV))
    cache = SaleRateCache()
    cache.refresh_if_stale()
    assert cache_path.exists()
    saved = json.loads(cache_path.read_text())
    assert saved["items"]["12345"] == {"sale_rate": 0.75, "sold_per_day": 3.2}


def test_new_instance_reads_previously_saved_cache(cache_path, monkeypatch):
    monkeypatch.setattr(tsm.requests, "get", lambda *a, **k: FakeResponse(SAMPLE_CSV))
    SaleRateCache().refresh_if_stale()
    fresh_instance = SaleRateCache()
    assert fresh_instance.get(12345) == {"sale_rate": 0.75, "sold_per_day": 3.2}


def test_load_tolerates_a_torn_or_corrupt_cache_file(cache_path):
    cache_path.write_text("{not valid json")
    cache = SaleRateCache()
    assert cache.get(12345) is None  # falls back to an empty cache, doesn't crash
