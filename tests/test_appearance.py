"""Tests for appearance.py: builds a rarity cache (item_id -> how many
distinct items share its transmog appearance) from wago.tools'
ItemModifiedAppearance export. The network call is stubbed -- no live
requests in this suite."""
import json
import time

import pytest

import appearance
from appearance import AppearanceCache, build_cache


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "appearances.json"
    monkeypatch.setattr(appearance, "CACHE_PATH", path)
    return path


def stub_rows(monkeypatch, rows: list[dict]):
    monkeypatch.setattr(appearance, "_download_rows", lambda: rows)


def row(item_id, appearance_id, modifier_id="0"):
    return {"ItemID": str(item_id), "ItemAppearanceID": str(appearance_id),
            "ItemAppearanceModifierID": str(modifier_id)}


def test_build_cache_counts_distinct_source_items_per_appearance(cache_path, monkeypatch):
    # appearance 500 is granted by items 1 and 2 (shared look); appearance
    # 501 only by item 3 (unique look).
    stub_rows(monkeypatch, [
        row(1, 500), row(2, 500), row(3, 501),
    ])
    cache = build_cache()
    assert cache["items"]["1"] == {"appearance_id": 500, "source_count": 2}
    assert cache["items"]["2"] == {"appearance_id": 500, "source_count": 2}
    assert cache["items"]["3"] == {"appearance_id": 501, "source_count": 1}
    assert cache_path.exists()


def test_build_cache_prefers_modifier_zero_row(cache_path, monkeypatch):
    """An item can have multiple rows (bonus/modifier variants) -- the
    modifier-0 (base/default look) row should win over a later variant row."""
    stub_rows(monkeypatch, [
        row(1, 999, modifier_id="7"),   # variant seen first
        row(1, 500, modifier_id="0"),   # base look -- should win
    ])
    cache = build_cache()
    assert cache["items"]["1"]["appearance_id"] == 500


def test_build_cache_keeps_first_row_when_no_modifier_zero(cache_path, monkeypatch):
    stub_rows(monkeypatch, [
        row(1, 700, modifier_id="3"),
        row(1, 701, modifier_id="5"),
    ])
    cache = build_cache()
    assert cache["items"]["1"]["appearance_id"] == 700


def test_appearance_cache_lookups(cache_path, monkeypatch):
    stub_rows(monkeypatch, [row(1, 500), row(2, 500)])
    build_cache()
    ac = AppearanceCache()
    assert ac.source_count(1) == 2
    assert ac.appearance_id(1) == 500
    assert ac.source_count(999999) is None       # unknown item -> None, never raises
    assert ac.appearance_id(999999) is None


def test_appearance_cache_missing_file_is_empty_not_an_error(cache_path):
    ac = AppearanceCache()
    assert ac.source_count(1) is None
    assert ac.is_stale() is True


def test_appearance_cache_corrupt_file_falls_back_to_empty(cache_path):
    cache_path.write_text("{not valid json")
    ac = AppearanceCache()
    assert ac.source_count(1) is None


def test_is_stale_reflects_cache_age(cache_path):
    fresh = {"fetched_at": int(time.time()), "items": {}}
    cache_path.write_text(json.dumps(fresh))
    assert AppearanceCache().is_stale() is False

    old = {"fetched_at": int(time.time()) - appearance.CACHE_MAX_AGE - 1, "items": {}}
    cache_path.write_text(json.dumps(old))
    assert AppearanceCache().is_stale() is True
