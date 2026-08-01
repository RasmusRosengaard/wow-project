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


def test_quality_resolves_and_caches(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, quality="EPIC", calls=calls)
    nc = NameCache()
    assert nc.quality(5507) == "EPIC"
    assert nc.quality(5507) == "EPIC"
    assert calls == [5507]


def test_pet_quality_uses_positional_palette(cache_path, monkeypatch):
    nc = NameCache()
    assert nc.quality(PET_ITEM, pet_quality_id=3) == item_names.PET_QUALITY_NAMES[3]
    assert nc.quality(PET_ITEM, pet_quality_id=99) is None  # out of range -> unknown


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


def test_ensure_many_resolves_multiple_items_concurrently(cache_path, monkeypatch):
    """Batch path for _populate_base_levels()'s region-wide type-28 gather --
    added after a sequential one-item-at-a-time loop there took 30-175s per
    request for a superuser's largely-uncached candidate set (real
    production incident, 2026-07-25)."""
    calls = []

    def fake(item_id):
        calls.append(item_id)
        return {"name": f"Item {item_id}", "quality": "COMMON", "level": item_id,
                "inventory_type": None, "item_class": None, "item_subclass": None}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake)

    nc = NameCache()
    nc.ensure_many([101, 102, 103])
    assert sorted(calls) == [101, 102, 103]
    assert nc.base_level(101) == 101
    assert nc.base_level(102) == 102
    assert nc.base_level(103) == 103
    # second call for an already-cached id -- no new fetch
    assert sorted(calls) == [101, 102, 103]


def test_ensure_many_dedupes_repeated_ids_in_input(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, level=636, calls=calls)
    nc = NameCache()
    nc.ensure_many([5507, 5507, 5507])
    assert calls == [5507]


def test_ensure_many_skips_already_complete_items(cache_path, monkeypatch):
    calls = []
    stub_details(monkeypatch, level=636, calls=calls)
    nc = NameCache()
    nc.ensure_many([5507])
    assert calls == [5507]
    nc.ensure_many([5507])  # already complete -- no second fetch
    assert calls == [5507]


def test_ensure_many_tolerates_fetch_failures(cache_path, monkeypatch):
    def fake(item_id):
        if item_id == 999999:
            return None
        return {"name": "Ornate Spyglass", "quality": "COMMON", "level": 35,
                "inventory_type": None, "item_class": None, "item_subclass": None}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake)

    nc = NameCache()
    nc.ensure_many([5507, 999999])
    assert nc.base_level(5507) == 35
    assert nc.base_level(999999) is None


def test_ensure_many_saves_to_disk(cache_path, monkeypatch):
    stub_details(monkeypatch, level=636)
    nc = NameCache()
    nc.ensure_many([5507])
    assert cache_path.exists()
    saved = json.loads(cache_path.read_text())
    assert saved["item_level"]["5507"] == 636


def test_ensure_many_noop_on_empty_input(cache_path, monkeypatch):
    nc = NameCache()
    nc.ensure_many([])
    assert not cache_path.exists()


def test_ensure_many_deadline_none_preserves_unbounded_behavior(cache_path, monkeypatch):
    """deadline_seconds=None (the default, and what every background caller
    -- collect_all._prewarm_item_base_levels() -- passes) must behave
    exactly as before this parameter existed: resolves everything,
    regardless of how long it takes."""
    calls = []
    stub_details(monkeypatch, level=636, calls=calls)
    nc = NameCache()
    nc.ensure_many([101, 102, 103], deadline_seconds=None)
    assert sorted(calls) == [101, 102, 103]
    assert nc.base_level(101) == 636
    assert nc.base_level(102) == 636
    assert nc.base_level(103) == 636


def test_ensure_many_deadline_seconds_does_not_wait_for_slow_items(cache_path, monkeypatch):
    """The real incident this was added for (2026-08-01): blizz.py's shared
    rate limiter turned api_get() from failing fast on a 429 into waiting
    patiently for the shared budget, which turned a single live
    /api/snipes call's resolution step from bounded to unbounded (158-300+
    seconds observed live). Item 999's stub blocks on an Event that's
    never set (bounded by its own short safety-net timeout so a genuinely
    broken implementation doesn't hang the whole test suite) -- if
    ensure_many() still used `with ThreadPoolExecutor(...) as pool:`'s
    default blocking shutdown(wait=True) instead of the explicit
    shutdown(wait=False, cancel_futures=True) it needs, this call would
    block for the full safety-net timeout instead of returning almost
    immediately once the deadline (0 -- trips on the very first completed
    future) is hit."""
    import threading
    import time as time_module

    never_set = threading.Event()

    def fake(item_id):
        if item_id == 999:
            never_set.wait(timeout=2)  # would block this call for 2s if the deadline didn't work
            return None
        return {"name": f"item {item_id}", "quality": "COMMON", "level": item_id,
                "inventory_type": None, "item_class": None, "item_subclass": None}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake)

    nc = NameCache()
    started = time_module.monotonic()
    nc.ensure_many([101, 999], max_workers=2, deadline_seconds=0)
    elapsed = time_module.monotonic() - started
    assert elapsed < 1.0  # did not wait out item 999's 2s block
    assert nc.base_level(101) == 101  # the fast item, resolved before the deadline tripped
    assert nc.has_class_info(999) is False  # the slow item never got a chance


def test_ensure_icons_many_deadline_seconds_does_not_wait_for_slow_items(cache_path, monkeypatch):
    """Same mechanism/reasoning as ensure_many()'s own version of this test
    -- see that test's docstring."""
    import threading
    import time as time_module

    never_set = threading.Event()

    def fake(path):
        if path.endswith("999"):
            never_set.wait(timeout=2)
            return None
        return "https://example/icon.jpg"
    monkeypatch.setattr(item_names, "_fetch_icon", fake)

    nc = NameCache()
    started = time_module.monotonic()
    nc.ensure_icons_many([5507, 999], max_workers=2, deadline_seconds=0)
    elapsed = time_module.monotonic() - started
    assert elapsed < 1.0
    assert nc.icon(5507) == "https://example/icon.jpg"


def test_ensure_icons_many_resolves_multiple_items_concurrently(cache_path, monkeypatch):
    """Batch path for dashboard.api_snipes' names=true row-building step --
    added (2026-07-26) after icon() turned out not to be covered by
    ensure_many()'s concurrent _fetch_item_details batch at all, so a sell
    realm queried for the first time still resolved every distinct item's
    icon one at a time, sequentially, directly in the per-row translation
    loop -- real production symptom: a switch to a never-before-queried
    realm hung until the request timed out."""
    calls = []

    def fake(path):
        calls.append(path)
        item_id = path.rsplit("/", 1)[-1]
        return f"https://example/{item_id}.jpg"
    monkeypatch.setattr(item_names, "_fetch_icon", fake)

    nc = NameCache()
    nc.ensure_icons_many([201, 202, 203])
    assert sorted(calls) == [
        "/data/wow/media/item/201", "/data/wow/media/item/202", "/data/wow/media/item/203",
    ]
    assert nc.icon(201) == "https://example/201.jpg"
    assert nc.icon(202) == "https://example/202.jpg"
    assert nc.icon(203) == "https://example/203.jpg"
    # second call for already-cached ids -- no new fetch
    nc.ensure_icons_many([201, 202, 203])
    assert len(calls) == 3


def test_ensure_icons_many_dedupes_repeated_ids_in_input(cache_path, monkeypatch):
    calls = []
    monkeypatch.setattr(item_names, "_fetch_icon", lambda path: calls.append(path) or "https://example/icon.jpg")
    nc = NameCache()
    nc.ensure_icons_many([5507, 5507, 5507])
    assert calls == ["/data/wow/media/item/5507"]


def test_ensure_icons_many_tolerates_fetch_failures(cache_path, monkeypatch):
    def fake(path):
        if path.endswith("999999"):
            return None
        return "https://example/icon.jpg"
    monkeypatch.setattr(item_names, "_fetch_icon", fake)

    nc = NameCache()
    nc.ensure_icons_many([5507, 999999])
    assert nc.icon(5507) == "https://example/icon.jpg"


def test_ensure_icons_many_noop_on_empty_input(cache_path, monkeypatch):
    nc = NameCache()
    nc.ensure_icons_many([])
    assert not cache_path.exists()


def test_ensure_icons_many_saves_to_disk(cache_path, monkeypatch):
    monkeypatch.setattr(item_names, "_fetch_icon", lambda path: "https://example/icon.jpg")
    nc = NameCache()
    nc.ensure_icons_many([5507])
    assert cache_path.exists()
    saved = json.loads(cache_path.read_text())
    assert saved["item_icons"]["5507"] == "https://example/icon.jpg"


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


def test_save_does_not_clobber_a_concurrent_instances_write(cache_path, monkeypatch):
    """The real bug traced 2026-08-01 ("items randomly jumping" on the
    dashboard, worst with a small class_quotas bucket like recipes, or with
    sus_item_suspect flickering under "hide flagged"): multiple NameCache()
    instances are constructed within a single /api/snipes call
    (snipe_check._register_class_quota_maps() and dashboard._build_rows()
    each make their own) plus another on collect_all.py's background
    prewarm loop, all racing on the same file. Two instances loaded before
    either saved -- item A resolves and saves first; item B (a different
    item, resolved by the other instance) must not be lost when the first
    instance saves again afterward, even though its own in-memory snapshot
    never saw B."""
    stub_details(monkeypatch, item_class=2, item_subclass=7)

    nc_first = NameCache()   # loads the (still empty) file
    nc_second = NameCache()  # loads the same empty file, independently

    nc_first.item_class(101)   # resolves item 101 (in-memory only so far)
    nc_first.save()             # ... and saves it
    nc_second.item_class(102)  # resolves item 102 on the *other* instance
    nc_second.save()            # file now has both 101 and 102

    # nc_first resolves a third item and saves again. A blind overwrite
    # would write only {101, 103} -- silently dropping 102, which nc_first
    # never loaded into its own in-memory _cache.
    nc_first.item_class(103)
    nc_first.save()

    saved = json.loads(cache_path.read_text())
    assert set(saved["item_class"]) == {"101", "102", "103"}

    # A fresh instance (e.g. the next /api/snipes call) must see all three --
    # this is exactly has_class_info()'s contract that
    # snipe_check._register_class_quota_maps() depends on for bucket
    # assignment to stay stable across requests.
    nc_fresh = NameCache()
    assert nc_fresh.has_class_info(101)
    assert nc_fresh.has_class_info(102)
    assert nc_fresh.has_class_info(103)


def test_save_tolerates_a_torn_read_of_the_cache_file(cache_path, monkeypatch):
    """A concurrent writer's non-atomic write (no temp-file+rename, see the
    class docstring) can leave the file mid-write when another instance's
    save() re-reads it -- this used to propagate as an unhandled
    json.JSONDecodeError, crashing the request. It must not lose *this*
    instance's own pending work."""
    stub_details(monkeypatch, item_class=2, item_subclass=7)
    cache_path.write_text('{"items": {"5507": "Ornate Sp')  # truncated/corrupt

    nc = NameCache()
    nc.item_class(101)
    nc.save()  # re-reads the still-corrupt file

    saved = json.loads(cache_path.read_text())
    assert saved["item_class"]["101"] == 2
