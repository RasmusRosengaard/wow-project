"""Tests for the region scanner's pure functions (rows) and sweep robustness —
no network involved."""
import pyarrow.parquet as pq
import pytest

import scan_region
from scan_region import rows


# --- rows ----------------------------------------------------------------

def test_rows_maps_auction_fields():
    payload = {"auctions": [{
        "id": 7, "item": {"id": 152510, "bonus_lists": [42]},
        "buyout": 50_000, "bid": 40_000, "quantity": 5, "time_left": "LONG",
    }]}
    [r] = rows(payload, cr=1403, ts=1_700_000_000)
    assert r == {
        "cr_id": 1403, "fetched_ts": 1_700_000_000, "auction_id": 7,
        "item_id": 152510, "bonus_key": "b:42", "pet_species_id": None,
        "pet_quality_id": None, "pet_level": None, "buyout": 50_000,
        "bid": 40_000, "quantity": 5, "time_left": "LONG",
    }


def test_rows_handles_empty():
    assert rows({}, cr=1403, ts=0) == []
    assert rows({"auctions": None}, cr=1403, ts=0) == []


# --- scan_one / sweep ------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self.headers = {}
        self.content = b"{}"
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("truncated body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_scan_one_writes_listings(monkeypatch, tmp_path):
    payload = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                             "quantity": 1, "time_left": "LONG"}]}
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload=payload))

    n = scan_region.scan_one(1403, ts=1_700_000_000)
    assert n == 1
    table = pq.read_table(tmp_path / "listings" / "1403.parquet")
    assert table.schema.equals(scan_region.LISTING_SCHEMA)
    assert table.num_rows == 1


def test_scan_one_skips_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, bad_json=True))
    assert scan_region.scan_one(1403, ts=0) == 0
    assert not (tmp_path / "listings" / "1403.parquet").exists()


def test_sweep_excludes_realms_and_survives_one_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "list_connected_realms",
                        lambda: [1096, 1403, 1305])

    def fake_scan_one(cr, ts):
        if cr == 1305:
            raise RuntimeError("HTTP 500")
        return 3

    monkeypatch.setattr(scan_region, "scan_one", fake_scan_one)
    scan_region.sweep(exclude={1403})   # 1403 (sell realm) skipped entirely
    # 1096 succeeds, 1305 raises but sweep() must not propagate the exception.
