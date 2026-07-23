#!/usr/bin/env python3
"""Local cache mapping item_id (and, for caged pets, pet_species_id) to a
display name, backed by the static-eu item/pet endpoints. Display-only: never
raises on a failed lookup, just falls back to a numeric placeholder, so a
missing/expired token or a dead item id can't break snipe_check's output.

Cache lives at data/item_names.json and is loaded/saved once per NameCache
instance rather than per lookup.
"""
import json
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


def _fetch_item_details(item_id: int) -> dict | None:
    """name + quality + catalog level + inventory slot type in one call --
    they all ride along for free on the same /item/{id} response the name
    lookup already needs. inventory_type is a real structured field
    Blizzard's API returns (e.g. "PROFESSION_TOOL", "HEAD", "TWOHWEAPON"),
    confirmed live against real items (Mining Pick, Blacksmith Hammer,
    Fishing Pole all return PROFESSION_TOOL; profession accessory-slot gear
    returns PROFESSION_GEAR) -- not a guess like the undocumented bonus
    modifiers elsewhere in this project."""
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
        self._dirty = False

    def _ensure_item_details(self, item_id: int) -> None:
        """Fetch name+quality+level+inventory_type together and backfill
        whichever cache a pre-existing entry is missing -- a cache file
        written before a field existed has the name but not that field, so
        don't assume a name-cache hit means everything else is cached too."""
        key = str(item_id)
        if key in self._cache["items"] and key in self._cache["item_quality"] \
                and key in self._cache["item_level"] and key in self._cache["item_inventory_type"]:
            return
        details = _fetch_item_details(item_id)
        if not details:
            return
        if details.get("name"):
            self._cache["items"][key] = details["name"]
        if details.get("quality"):
            self._cache["item_quality"][key] = details["quality"]
        if details.get("level") is not None:
            self._cache["item_level"][key] = details["level"]
        # Unlike quality/level above, always record inventory_type even
        # when it's None -- most items genuinely have no inventory_type
        # (reagents, consumables, quest items aren't equippable at all), so
        # a truthy-only write here would mean the key never lands in the
        # cache for the common case, and _ensure_item_details would refetch
        # every single one of those items on every call forever.
        self._cache["item_inventory_type"][key] = details.get("inventory_type")
        self._dirty = True

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
