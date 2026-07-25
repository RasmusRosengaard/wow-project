"""Tests for the collector's pure functions (bonus_key, rows) and robustness
paths (429/5xx backoff, malformed JSON) — no network involved."""
import json

import pytest

import fetch_snapshot
from fetch_snapshot import bonus_key, get_auctions_with_backoff, parse_bonus_key, rows


# --- bonus_key ---------------------------------------------------------------

def test_bonus_key_empty_item():
    assert bonus_key({}) == ""


def test_bonus_key_sorts_bonus_lists():
    assert bonus_key({"bonus_lists": [6654, 1234]}) == bonus_key({"bonus_lists": [1234, 6654]})
    assert bonus_key({"bonus_lists": [1234, 6654]}) == "b:1234,6654"


def test_bonus_key_sorts_modifiers():
    a = {"modifiers": [{"type": 9, "value": 70}, {"type": 28, "value": 2164}]}
    b = {"modifiers": [{"type": 28, "value": 2164}, {"type": 9, "value": 70}]}
    assert bonus_key(a) == bonus_key(b) == "m:9=70,28=2164"


def test_bonus_key_combines_parts():
    item = {"bonus_lists": [42], "modifiers": [{"type": 9, "value": 70}]}
    assert bonus_key(item) == "b:42|m:9=70"


def test_bonus_key_distinguishes_variants():
    assert bonus_key({"bonus_lists": [42]}) != bonus_key({"bonus_lists": [43]})


# --- parse_bonus_key ----------------------------------------------------------

def test_parse_bonus_key_empty():
    assert parse_bonus_key("") == {"bonus_ids": [], "mods": {}}


def test_parse_bonus_key_round_trips_bonus_key_output():
    item = {"bonus_lists": [6654, 1234], "modifiers": [{"type": 9, "value": 70},
                                                        {"type": 28, "value": 2164}]}
    parsed = parse_bonus_key(bonus_key(item))
    assert parsed == {"bonus_ids": ["1234", "6654"], "mods": {9: "70", 28: "2164"}}


def test_parse_bonus_key_bonus_only():
    assert parse_bonus_key("b:42,43") == {"bonus_ids": ["42", "43"], "mods": {}}


def test_parse_bonus_key_mods_only():
    assert parse_bonus_key("m:28=1747") == {"bonus_ids": [], "mods": {28: "1747"}}


# --- rows --------------------------------------------------------------------

def test_rows_maps_auction_fields():
    payload = {"auctions": [{
        "id": 7, "item": {"id": 152510, "bonus_lists": [42]},
        "buyout": 50_000, "bid": 40_000, "quantity": 5, "time_left": "LONG",
    }]}
    [r] = rows(payload, ts=1_700_000_000)
    assert r == {
        "snapshot_ts": 1_700_000_000, "auction_id": 7, "item_id": 152510,
        "bonus_key": "b:42", "pet_species_id": None, "pet_quality_id": None,
        "pet_level": None, "buyout": 50_000, "bid": 40_000, "quantity": 5,
        "time_left": "LONG",
    }


def test_rows_defaults_quantity_and_handles_empty():
    [r] = rows({"auctions": [{"id": 1, "item": {"id": 2}}]}, ts=0)
    assert r["quantity"] == 1 and r["buyout"] is None
    assert rows({}, ts=0) == []
    assert rows({"auctions": None}, ts=0) == []


# --- backoff & malformed JSON ------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, headers=None, payload=None, bad_json=False):
        self.status_code = status
        self.headers = headers or {}
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


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(fetch_snapshot.time, "sleep", slept.append)
    return slept


def test_backoff_retries_then_succeeds(monkeypatch, no_sleep):
    responses = [FakeResponse(429, headers={"Retry-After": "7"}),
                 FakeResponse(503),
                 FakeResponse(200)]
    monkeypatch.setattr(fetch_snapshot, "api_get",
                        lambda *a, **k: responses.pop(0))
    r = get_auctions_with_backoff(1096, headers={})
    assert r.status_code == 200
    assert no_sleep == [7, 60]      # Retry-After honored, then doubled default


def test_backoff_gives_up_after_attempts(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(fetch_snapshot, "api_get",
                        lambda *a, **k: calls.append(1) or FakeResponse(500))
    r = get_auctions_with_backoff(1096, headers={}, attempts=3)
    assert r.status_code == 500
    assert len(calls) == 3
    assert len(no_sleep) == 2       # no sleep after the final attempt


def test_fetch_once_skips_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_snapshot, "DATA", tmp_path)
    monkeypatch.setattr(fetch_snapshot, "api_get", lambda *a, **k: FakeResponse(
        200, headers={"Last-Modified": "Mon, 14 Jul 2026 12:00:00 GMT"}, bad_json=True))
    assert fetch_snapshot.fetch_once(1096) is None
    assert not list(tmp_path.rglob("*.parquet"))
    assert not (tmp_path / "state" / "1096.json").exists()   # cursor untouched


def test_fetch_once_writes_snapshot_and_state(monkeypatch, tmp_path):
    lm = "Mon, 14 Jul 2026 12:00:00 GMT"
    payload = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                             "quantity": 1, "time_left": "LONG"}]}
    monkeypatch.setattr(fetch_snapshot, "DATA", tmp_path)
    monkeypatch.setattr(fetch_snapshot, "api_get", lambda *a, **k: FakeResponse(
        200, headers={"Last-Modified": lm}, payload=payload))

    out = fetch_snapshot.fetch_once(1096)
    assert out is not None and out.exists()
    state = json.loads((tmp_path / "state" / "1096.json").read_text())
    assert state == {"last_modified": lm}
    # Same dump again (e.g. restart before Blizzard publishes): no rewrite.
    assert fetch_snapshot.fetch_once(1096) is None


def test_fetch_once_304_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_snapshot, "DATA", tmp_path)
    monkeypatch.setattr(fetch_snapshot, "api_get", lambda *a, **k: FakeResponse(304))
    assert fetch_snapshot.fetch_once(1096) is None
