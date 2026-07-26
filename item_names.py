#!/usr/bin/env python3
"""Local cache mapping item_id (and, for caged pets, pet_species_id) to a
display name, backed by the static-eu item/pet endpoints. Display-only: never
raises on a failed lookup, just falls back to a numeric placeholder, so a
missing/expired token or a dead item id can't break snipe_check's output.

Cache lives at data/item_names.json and is loaded/saved once per NameCache
instance rather than per lookup.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from blizz import api_get

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE_PATH = DATA / "item_names.json"

PET_CAGE_ITEM_ID = 82800

# Standard WoW quality colors (Blizzard's own UI palette), keyed by the
# `quality.type` string the static item API returns.
QUALITY_COLORS = {
    "POOR": "#9d9d9d", "COMMON": "#ffffff", "UNCOMMON": "#1eff00",
    "RARE": "#0070dd", "EPIC": "#a335ee", "LEGENDARY": "#ff8000",
    "ARTIFACT": "#e6cc80", "HEIRLOOM": "#00ccff", "WOW_TOKEN": "#00ccff",
}
# Battle pets use the same palette ordered Poor->Legendary; pet_quality_id's
# exact indexing isn't documented by Blizzard, so this assumes the same
# 0-indexed convention as the item quality enum (0=Poor..5=Legendary).
PET_QUALITY_COLORS = ["#9d9d9d", "#ffffff", "#1eff00", "#0070dd", "#a335ee", "#ff8000"]
# Same Poor->Legendary order as PET_QUALITY_COLORS, as tier names instead of
# colors -- backs quality() below, used for the rarity filter (filtering by
# color would be fragile/unreadable; the tier name is the real identity).
PET_QUALITY_NAMES = ["POOR", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY"]


def _fetch_item_details(item_id: int) -> dict | None:
    """name + quality + catalog level + inventory slot type + item class/
    subclass in one call -- they all ride along for free on the same
    /item/{id} response the name lookup already needs. inventory_type is a
    real structured field Blizzard's API returns (e.g. "PROFESSION_TOOL",
    "HEAD", "TWOHWEAPON"), confirmed live against real items (Mining Pick,
    Blacksmith Hammer, Fishing Pole all return PROFESSION_TOOL; profession
    accessory-slot gear returns PROFESSION_GEAR) -- not a guess like the
    undocumented bonus modifiers elsewhere in this project. item_class/
    item_subclass ids are likewise real, documented fields -- confirmed live
    against GET /data/wow/item-class/index (2026-07-24): 2=Weapon, 4=Armor,
    1=Container, 19=Profession, 20=Housing, 17=Battle Pets, 12=Quest,
    15=Miscellaneous (subclass 5=Mount under it). See dashboard.html's
    item-class filter for the full mapping."""
    try:
        r = api_get(f"/data/wow/item/{item_id}", "static")
        if r.status_code != 200:
            return None
        j = r.json()
        return {
            "name": j.get("name"),
            "quality": (j.get("quality") or {}).get("type"),
            "level": j.get("level"),
            "inventory_type": (j.get("inventory_type") or {}).get("type"),
            "item_class": (j.get("item_class") or {}).get("id"),
            "item_subclass": (j.get("item_subclass") or {}).get("id"),
        }
    except (Exception, SystemExit):
        return None


def _fetch_pet_name(species_id: int) -> str | None:
    try:
        r = api_get(f"/data/wow/pet/{species_id}", "static")
        if r.status_code != 200:
            return None
        return r.json().get("name")
    except (Exception, SystemExit):
        return None


def _fetch_icon(path: str) -> str | None:
    try:
        r = api_get(path, "static")
        if r.status_code != 200:
            return None
        for asset in r.json().get("assets", []):
            if asset.get("key") == "icon":
                return asset.get("value")
        return None
    except (Exception, SystemExit):
        return None


class NameCache:
    """Load once, resolve names/icons on demand, save once (only if anything
    new was actually fetched)."""

    def __init__(self):
        self._cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() \
            else {"items": {}, "pets": {}}
        # older cache files predate icon/quality/level support -- backfill the
        # keys rather than bump a schema version, since this is a local,
        # gitignored cache
        self._cache.setdefault("item_icons", {})
        self._cache.setdefault("pet_icons", {})
        self._cache.setdefault("item_quality", {})
        self._cache.setdefault("item_level", {})
        self._cache.setdefault("item_inventory_type", {})
        self._cache.setdefault("item_class", {})
        self._cache.setdefault("item_subclass", {})
        self._dirty = False

    def _is_complete(self, key: str) -> bool:
        return key in self._cache["items"] and key in self._cache["item_quality"] \
            and key in self._cache["item_level"] and key in self._cache["item_inventory_type"] \
            and key in self._cache["item_class"] and key in self._cache["item_subclass"]

    def _merge_details(self, item_id: int, details: dict) -> None:
        key = str(item_id)
        if details.get("name"):
            self._cache["items"][key] = details["name"]
        if details.get("quality"):
            self._cache["item_quality"][key] = details["quality"]
        if details.get("level") is not None:
            self._cache["item_level"][key] = details["level"]
        # Unlike quality/level above, always record inventory_type/class/
        # subclass even when None -- most items genuinely have no
        # inventory_type (reagents, consumables, quest items aren't
        # equippable at all), so a truthy-only write here would mean the key
        # never lands in the cache for the common case, and
        # _ensure_item_details would refetch every single one of those items
        # on every call forever. item_class should realistically always be
        # present, but the same defensive handling costs nothing.
        self._cache["item_inventory_type"][key] = details.get("inventory_type")
        self._cache["item_class"][key] = details.get("item_class")
        self._cache["item_subclass"][key] = details.get("item_subclass")
        self._dirty = True

    def _ensure_item_details(self, item_id: int) -> None:
        """Fetch name+quality+level+inventory_type+class/subclass together and
        backfill whichever cache a pre-existing entry is missing -- a cache
        file written before a field existed has the name but not that field,
        so don't assume a name-cache hit means everything else is cached
        too."""
        key = str(item_id)
        if self._is_complete(key):
            return
        details = _fetch_item_details(item_id)
        if not details:
            return
        self._merge_details(item_id, details)

    def ensure_many(self, item_ids: list[int], max_workers: int = 16,
                     limit: int | None = None) -> None:
        """Batch version of _ensure_item_details(): resolves every not-yet-
        cached id concurrently via a thread pool instead of one sequential
        Blizzard API call per item. Added 2026-07-25 after a superuser's
        region-wide, largely-uncached query (snipe_check._populate_market_keys,
        which has no --items filter to narrow its candidate set) took 30-175s
        per /api/snipes call doing this one item at a time -- real production
        pain, not a hypothetical (a stuck "Loading" dashboard, greyed-out
        table, for minutes).

        limit (added same day, once concurrency alone turned out not to be
        enough): caps how many NOT-yet-cached items this call will actually
        resolve, in the priority order item_ids was given. Live-confirmed
        the candidate set _populate_market_keys() gathers can be 15,000+
        items on just one sell realm -- type-28 modifiers turn out to be
        ubiquitous on modern ilvl-scaling gear, not the rare old-BoE-item
        case this was first built for. Blizzard's rate limit (100 req/s) is
        a hard ceiling regardless of thread count: 15,000 calls can never
        complete in under ~150s no matter how parallel. Bounding a single
        call's work is the only way to keep any one request fast; the cache
        converges over many calls instead (see collect_all.py's periodic
        pre-warm, which does this in the background so it doesn't depend on
        user traffic to make progress). None (default, and every caller
        before this) means unlimited, matching the original behavior.
        Saved incrementally (every 50 completions) regardless -- an
        interrupted batch still keeps whatever it resolved."""
        missing = []
        seen = set()
        for item_id in item_ids:
            if item_id in seen:
                continue
            seen.add(item_id)
            if not self._is_complete(str(item_id)):
                missing.append(item_id)
                if limit is not None and len(missing) >= limit:
                    break
        if not missing:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_item_details, item_id): item_id for item_id in missing}
            for i, future in enumerate(as_completed(futures)):
                details = future.result()
                if details:
                    self._merge_details(futures[future], details)
                if i % 50 == 49:
                    self.save()
        self.save()

    def ensure_icons_many(self, item_ids: list[int], max_workers: int = 16) -> None:
        """Batch/concurrent version of icon() for items (not pets) -- icon()
        isn't covered by ensure_many()'s _fetch_item_details batch (icons
        come from a separate media endpoint), so a realm queried for the
        first time still resolved every distinct item's icon one at a time,
        sequentially -- same root cause ensure_many() was added for
        (2026-07-25), just for a code path (dashboard.api_snipes's per-row
        names=true resolution) that predates ensure_many() and was never
        updated to prefetch icons the same way. Added 2026-07-26 after this
        was confirmed live to freeze a switch to a never-before-queried sell
        realm."""
        missing = [item_id for item_id in dict.fromkeys(item_ids)
                   if str(item_id) not in self._cache["item_icons"]]
        if not missing:
            return
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_icon, f"/data/wow/media/item/{item_id}"): item_id
                       for item_id in missing}
            for i, future in enumerate(as_completed(futures)):
                url = future.result()
                if url:
                    self._cache["item_icons"][str(futures[future])] = url
                    self._dirty = True
                if i % 50 == 49:
                    self.save()
        self.save()

    def get(self, item_id: int, pet_species_id: int | None = None) -> str:
        if item_id == PET_CAGE_ITEM_ID and pet_species_id is not None:
            key = str(pet_species_id)
            if key in self._cache["pets"]:
                return self._cache["pets"][key]
            name = _fetch_pet_name(pet_species_id)
            if name:
                self._cache["pets"][key] = name
                self._dirty = True
                return name
            return f"pet species {pet_species_id}"

        key = str(item_id)
        self._ensure_item_details(item_id)
        return self._cache["items"].get(key, f"item {item_id}")

    def quality_color(self, item_id: int, pet_species_id: int | None = None,
                       pet_quality_id: int | None = None) -> str | None:
        """CSS color for the item/pet's rarity, or None if unknown -- callers
        should fall back to a neutral color, same as a missing name/icon."""
        if item_id == PET_CAGE_ITEM_ID and pet_quality_id is not None:
            if 0 <= pet_quality_id < len(PET_QUALITY_COLORS):
                return PET_QUALITY_COLORS[pet_quality_id]
            return None
        self._ensure_item_details(item_id)
        return QUALITY_COLORS.get(self._cache["item_quality"].get(str(item_id)))

    def quality(self, item_id: int, pet_species_id: int | None = None,
                pet_quality_id: int | None = None) -> str | None:
        """Quality tier name (e.g. "EPIC"), the same tier quality_color()
        derives its ring color from -- exposed separately so callers can
        filter/group by tier identity, not just paint a color."""
        if item_id == PET_CAGE_ITEM_ID and pet_quality_id is not None:
            if 0 <= pet_quality_id < len(PET_QUALITY_NAMES):
                return PET_QUALITY_NAMES[pet_quality_id]
            return None
        self._ensure_item_details(item_id)
        return self._cache["item_quality"].get(str(item_id))

    def base_level(self, item_id: int) -> int | None:
        """The item's own catalog level from the static API -- used to sanity
        -check a claimed modifier-derived item level against, since that
        modifier isn't documented and can be nonsensical for items outside
        the modern ilvl-scaling system (e.g. old fixed-stat gear)."""
        if item_id == PET_CAGE_ITEM_ID:
            return None
        self._ensure_item_details(item_id)
        return self._cache["item_level"].get(str(item_id))

    def inventory_type(self, item_id: int) -> str | None:
        """The equipment slot type (e.g. "HEAD", "PROFESSION_TOOL"), or None
        if unknown/not equippable. Used to filter profession tool/accessory
        slots (PROFESSION_TOOL, PROFESSION_GEAR) out of transmog-rarity
        results -- those slots aren't part of the visible paperdoll model at
        all, so "unique transmog" never meaningfully applied to them."""
        if item_id == PET_CAGE_ITEM_ID:
            return None
        self._ensure_item_details(item_id)
        return self._cache["item_inventory_type"].get(str(item_id))

    def item_class(self, item_id: int) -> int | None:
        """Blizzard's official item_class id (e.g. 2=Weapon, 4=Armor,
        19=Profession, 20=Housing -- see GET /data/wow/item-class/index),
        or None if unknown. A caged pet (82800) is class 17 (Battle Pets)
        itself, so this works for pets too without needing pet_species_id."""
        self._ensure_item_details(item_id)
        return self._cache["item_class"].get(str(item_id))

    def item_subclass(self, item_id: int) -> int | None:
        """Blizzard's official item_subclass id, scoped within item_class
        (e.g. subclass 5 under class 15/Miscellaneous is Mount) -- or None
        if unknown."""
        self._ensure_item_details(item_id)
        return self._cache["item_subclass"].get(str(item_id))

    def icon(self, item_id: int, pet_species_id: int | None = None) -> str | None:
        """Icon asset URL from Blizzard's render CDN, or None if unresolved --
        display-only, callers should tolerate a missing icon same as a
        missing name."""
        if item_id == PET_CAGE_ITEM_ID and pet_species_id is not None:
            key = str(pet_species_id)
            if key in self._cache["pet_icons"]:
                return self._cache["pet_icons"][key]
            url = _fetch_icon(f"/data/wow/media/pet/{pet_species_id}")
            if url:
                self._cache["pet_icons"][key] = url
                self._dirty = True
            return url

        key = str(item_id)
        if key in self._cache["item_icons"]:
            return self._cache["item_icons"][key]
        url = _fetch_icon(f"/data/wow/media/item/{item_id}")
        if url:
            self._cache["item_icons"][key] = url
            self._dirty = True
        return url

    def save(self) -> None:
        if not self._dirty:
            return
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(self._cache))
        self._dirty = False
