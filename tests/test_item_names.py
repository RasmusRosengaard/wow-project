"""Tests for item_names.py's NameCache: name/icon/quality/base_level lookups,
local caching, and backward compatibility with cache files written before
later fields (icons, quality, level) existed. All Blizzard API calls are
stubbed -- no live network in this suite."""
import json

import pytest

import item_names
from item_names import NameCache

PET_ITEM = item_names.PET_CAGE_ITEM_ID


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "item_names.json"
    monkeypatch.setattr(item_names, "CACHE_PATH", path)
    return path


def stub_details(monkeypatch, name="Ornate Spyglass", quality="COMMON", level=35, calls=None):
    def fake(item_id):
        if calls is not None:
            calls.append(item_id)
        return {"name": name, "quality": quality, "level": level}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake)


def test_get_fetches_and_caches_new_item(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, name="Ornate Spyglass", calls=calls)
    nc = NameCache()
    assert nc.get(5507) == "Ornate Spyglass"
    assert nc.get(5507) == "Ornate Spyglass"
    assert calls == [5507]  # second call served from cache


def test_get_falls_back_to_placeholder_on_fetch_failure(cache_path, monkeypatch):
    monkeypatch.setattr(item_names, "_fetch_item_details", lambda item_id: None)
    nc = NameCache()
    assert nc.get(999999) == "item 999999"


def test_get_resolves_pet_via_species_id(cache_path, monkeypatch):
    monkeypatch.setattr(item_names, "_fetch_pet_name", lambda species_id: "Larion Cub")
    nc = NameCache()
    assert nc.get(PET_ITEM, pet_species_id=3064) == "Larion Cub"


def test_quality_color_resolves_and_caches(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, quality="EPIC", calls=calls)
    nc = NameCache()
    assert nc.quality_color(5507) == "#a335ee"
    assert nc.quality_color(5507) == "#a335ee"
    assert calls == [5507]


def test_quality_color_unknown_quality_returns_none(cache_path, monkeypatch):
    stub_details(monkeypatch, quality="SOME_NEW_QUALITY_TYPE")
    nc = NameCache()
    assert nc.quality_color(5507) is None


def test_pet_quality_color_uses_positional_palette(cache_path, monkeypatch):
    nc = NameCache()
    assert nc.quality_color(PET_ITEM, pet_quality_id=3) == item_names.PET_QUALITY_COLORS[3]
    assert nc.quality_color(PET_ITEM, pet_quality_id=99) is None  # out of range -> unknown


def test_base_level_resolves_and_caches(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, level=636, calls=calls)
    nc = NameCache()
    assert nc.base_level(5507) == 636
    assert nc.base_level(5507) == 636
    assert calls == [5507]


def test_base_level_none_for_pet_cage_item(cache_path, monkeypatch):
    nc = NameCache()
    assert nc.base_level(PET_ITEM) is None


def test_single_fetch_populates_name_quality_and_level_together(cache_path, monkeypatch):
    """get(), quality_color(), and base_level() should share one fetch per
    item rather than each re-hitting the API."""
    calls = []
    stub_details(monkeypatch, name="Ornate Spyglass", quality="COMMON", level=35, calls=calls)
    nc = NameCache()
    assert nc.get(5507) == "Ornate Spyglass"
    assert nc.quality_color(5507) == "#ffffff"
    assert nc.base_level(5507) == 35
    assert calls == [5507]


def test_backfills_quality_and_level_for_pre_existing_name_only_cache(cache_path, monkeypatch):
    """A cache file written before quality/level support only has the name
    string cached -- quality_color()/base_level() must still resolve by
    fetching independently, not silently return None forever."""
    cache_path.write_text(json.dumps({"items": {"5507": "Ornate Spyglass"}, "pets": {}}))
    calls = []
    stub_details(monkeypatch, quality="RARE", level=40, calls=calls)
    nc = NameCache()
    assert nc.get(5507) == "Ornate Spyglass"  # already cached, no fetch needed for this alone
    assert nc.quality_color(5507) == "#0070dd"  # triggers the backfill fetch
    assert nc.base_level(5507) == 40  # already backfilled by the quality_color() call
    assert calls == [5507]


def test_save_only_writes_when_dirty(cache_path, monkeypatch):
    nc = NameCache()
    nc.save()
    assert not cache_path.exists()

    stub_details(monkeypatch)
    nc.get(5507)
    nc.save()
    assert cache_path.exists()
    saved = json.loads(cache_path.read_text())
    assert saved["items"]["5507"] == "Ornate Spyglass"
