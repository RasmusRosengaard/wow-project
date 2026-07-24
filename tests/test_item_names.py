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


def stub_details(monkeypatch, name="Ornate Spyglass", quality="COMMON", level=35,
                 inventory_type=None, item_class=None, item_subclass=None, calls=None):
    def fake(item_id):
        if calls is not None:
            calls.append(item_id)
        return {"name": name, "quality": quality, "level": level, "inventory_type": inventory_type,
                "item_class": item_class, "item_subclass": item_subclass}
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


def test_inventory_type_resolves_and_caches(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, inventory_type="PROFESSION_TOOL", calls=calls)
    nc = NameCache()
    assert nc.inventory_type(2901) == "PROFESSION_TOOL"
    assert nc.inventory_type(2901) == "PROFESSION_TOOL"
    assert calls == [2901]  # second call served from cache, not refetched


def test_inventory_type_none_is_cached_not_refetched_forever(cache_path, monkeypatch):
    """Most items (reagents, consumables, quest items) have no
    inventory_type at all -- that's a real, common, permanent answer, not
    "not fetched yet," so it must not trigger a refetch on every call."""
    calls = []
    stub_details(monkeypatch, inventory_type=None, calls=calls)
    nc = NameCache()
    assert nc.inventory_type(6948) is None
    assert nc.inventory_type(6948) is None
    assert calls == [6948]


def test_inventory_type_unknown_item_returns_none(cache_path, monkeypatch):
    monkeypatch.setattr(item_names, "_fetch_item_details", lambda item_id: None)
    nc = NameCache()
    assert nc.inventory_type(999999) is None


def test_inventory_type_pet_cage_returns_none(cache_path, monkeypatch):
    nc = NameCache()
    assert nc.inventory_type(PET_ITEM) is None


def test_item_class_resolves_and_caches(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, item_class=2, item_subclass=7, calls=calls)  # Weapon/Sword
    nc = NameCache()
    assert nc.item_class(19019) == 2
    assert nc.item_subclass(19019) == 7
    assert nc.item_class(19019) == 2
    assert calls == [19019]  # second call served from cache, not refetched


def test_item_class_unknown_item_returns_none(cache_path, monkeypatch):
    monkeypatch.setattr(item_names, "_fetch_item_details", lambda item_id: None)
    nc = NameCache()
    assert nc.item_class(999999) is None
    assert nc.item_subclass(999999) is None


def test_backfills_item_class_for_pre_existing_name_only_cache(cache_path, monkeypatch):
    """Same backfill guarantee as inventory_type -- a cache file predating
    item_class support must still resolve it on demand."""
    cache_path.write_text(json.dumps({"items": {"19019": "Thunderfury"}, "pets": {}}))
    calls = []
    stub_details(monkeypatch, name="Thunderfury", item_class=2, item_subclass=7, calls=calls)
    nc = NameCache()
    assert nc.get(19019) == "Thunderfury"  # already cached, no fetch needed for this alone
    assert nc.item_class(19019) == 2  # triggers the backfill fetch
    assert nc.item_subclass(19019) == 7  # already backfilled by the item_class() call
    assert calls == [19019]


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
    """get(), quality_color(), base_level(), and inventory_type() should
    share one fetch per item rather than each re-hitting the API."""
    calls = []
    stub_details(monkeypatch, name="Ornate Spyglass", quality="COMMON", level=35,
                 inventory_type="HEAD", calls=calls)
    nc = NameCache()
    assert nc.get(5507) == "Ornate Spyglass"
    assert nc.quality_color(5507) == "#ffffff"
    assert nc.base_level(5507) == 35
    assert nc.inventory_type(5507) == "HEAD"
    assert calls == [5507]


def test_backfills_quality_and_level_for_pre_existing_name_only_cache(cache_path, monkeypatch):
    """A cache file written before quality/level/inventory_type support only
    has the name string cached -- the newer accessors must still resolve by
    fetching independently, not silently return None forever."""
    cache_path.write_text(json.dumps({"items": {"5507": "Ornate Spyglass"}, "pets": {}}))
    calls = []
    stub_details(monkeypatch, quality="RARE", level=40, inventory_type="CHEST", calls=calls)
    nc = NameCache()
    assert nc.get(5507) == "Ornate Spyglass"  # already cached, no fetch needed for this alone
    assert nc.quality_color(5507) == "#0070dd"  # triggers the backfill fetch
    assert nc.base_level(5507) == 40  # already backfilled by the quality_color() call
    assert nc.inventory_type(5507) == "CHEST"  # already backfilled too
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
