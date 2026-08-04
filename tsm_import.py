#!/usr/bin/env python3
"""Decode a TradeSkillMaster (TSM4) *group export* string, for Watchlist's
"import a TSM group instead of adding items one at a time" feature (see
.claude/docs/feature-watchlist.md).

A TSM group export is not plain text -- it's `LibSerialize:SerializeEx(...)`
piped through `LibDeflate:CompressDeflate()` then `:EncodeForPrint()`
(confirmed 2026-08-02 against TSM's own real, unmodified addon source,
`Core/Service/Groups/ImportExport.lua`'s `GenerateExport`/`DecodeNewImport`
-- not guessed, not the classic `^`-prefixed AceSerializer format some older
TSM strings use, which this module does not support). Rather than
hand-porting that binary format to Python from documentation (real risk of
a silently-wrong bit offset producing wrong item ids with no error), this
runs the two real, unmodified Lua files TSM itself ships
(`vendor/tsm_lua/LibDeflate.lua`/`LibSerialize.lua`, byte-identical to
TSM's own bundled copies -- LibSerialize is pinned at TSM's MINOR=1, not
current upstream, which has since moved to a different wire format) via a
real embedded Lua interpreter (`lupa`), the same human-approved tradeoff
as `blizz.py`'s reliance on Blizzard's real API rather than a reimplemented
one.

`unpack` is injected as a global before loading LibSerialize.lua -- WoW's
Lua 5.1 has it as a global, modern Lua (what `lupa` embeds) only has
`table.unpack`; this is the one shim needed, everything else these two
files use is either self-contained or a plain local variable (confirmed by
grepping both files for WoW-only globals before writing this module).

Verified end-to-end against a real user-provided TSM group export string
(a 300-item crafting-materials group spanning 112 sub-paths) -- see
tests/test_tsm_import.py, which uses that exact string as its test vector.

Format shape, confirmed live: `items` is a Lua table mapping TSM
itemString -> the item's sub-group path *relative to the exported group*
(empty string if the item sits directly in the exported group, not a
sub-group -- covers both the "group/items" and "group/subcategory/items"
shapes .claude/docs/feature-watchlist.md flagged as an open question). Sub-paths are
backtick-joined (`TSM.CONST.GROUP_SEP`), not `/` -- re-joined here with
`/` for display, matching how the rest of this project already presents
paths to a human.

Known limitation: only `i:<itemId>...` itemStrings are parsed (confirmed
live). TSM's caged-pet itemString format (`p:...`) was not present in the
one real sample string available while building this and its exact shape
is unconfirmed -- rather than guess at it, pet entries in an imported group
are silently skipped (not crashed on), so a group containing pets imports
its non-pet items correctly and simply omits the pets. Worth revisiting
with a real pet-containing sample if this turns out to matter.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import lupa

ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor" / "tsm_lua"

GROUP_SEP = "`"  # TSM.CONST.GROUP_SEP, confirmed live against real TSM source
MAGIC_STR = b"TSM_EXPORT"
EXPECTED_VERSION = 1

PET_CAGE_ITEM_ID = 82800  # matches fetch_snapshot.py's pet-cage convention

_ITEM_STRING_RE = re.compile(r"^i:(\d+)")


class TsmImportError(ValueError):
    """A pasted string isn't a decodable TSM group export."""


@dataclass(frozen=True)
class TsmGroupItem:
    item_id: int
    group_path: str  # "/"-joined, e.g. "Player Housing - Decor || Craft/Alchemy"
    raw_item_string: str


@dataclass(frozen=True)
class TsmGroupExport:
    group_name: str
    items: list[TsmGroupItem]


# A single module-level Lua runtime, lazily created, guarded by a lock --
# lupa's Lua state isn't safe for concurrent calls from multiple threads,
# and creating a fresh interpreter (plus reloading both library files) per
# call is needless overhead for what's a low-frequency, interactive action
# (pasting a group export), not a hot path.
_lock = threading.Lock()
_runtime = None  # (lua, LibDeflate, LibSerialize) once initialized


def _get_runtime():
    global _runtime
    if _runtime is None:
        lua = lupa.LuaRuntime(unpack_returned_tuples=True, encoding=None)
        lua.globals().unpack = lua.globals().table.unpack
        lib_deflate_src = (VENDOR_DIR / "LibDeflate.lua").read_text(encoding="utf-8")
        lib_serialize_src = (VENDOR_DIR / "LibSerialize.lua").read_text(encoding="utf-8")
        lib_deflate = lua.execute(lib_deflate_src)
        lib_serialize = lua.execute(lib_serialize_src)
        _runtime = (lua, lib_deflate, lib_serialize)
    return _runtime


def decode_group_export(export_str: str) -> TsmGroupExport:
    """Raises TsmImportError with a human-readable message on anything that
    isn't a valid, current-format TSM group export string."""
    export_str = export_str.strip()
    if not export_str:
        raise TsmImportError("Paste a TSM group export string.")
    try:
        raw_bytes = export_str.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TsmImportError("Not a valid TSM export string (unexpected characters).") from exc

    with _lock:
        lua, lib_deflate, lib_serialize = _get_runtime()
        decoded = lib_deflate.DecodeForPrint(lib_deflate, raw_bytes)
        if decoded is None:
            raise TsmImportError("Not a valid TSM export string.")
        decompressed, num_extra_bytes = lib_deflate.DecompressDeflate(lib_deflate, decoded)
        if decompressed is None or num_extra_bytes > 0:
            raise TsmImportError("Not a valid TSM export string (decompression failed).")
        result = lib_serialize.Deserialize(lib_serialize, decompressed)

    success, magic, version, group_name, items_table, *_rest = result
    if not success:
        raise TsmImportError("Not a valid TSM export string (corrupt data).")
    if magic != MAGIC_STR:
        raise TsmImportError("Not a TSM group export (unrecognized format).")
    if int(version) != EXPECTED_VERSION:
        raise TsmImportError(
            f"Unsupported TSM export version {version!r} (expected {EXPECTED_VERSION})."
        )

    group_name_str = group_name.decode("utf-8")

    items: list[TsmGroupItem] = []
    for item_string_raw, rel_group_path_raw in items_table.items():
        item_string = item_string_raw.decode("utf-8")
        m = _ITEM_STRING_RE.match(item_string)
        if not m:
            continue  # a non-item TSM string (e.g. an unconfirmed pet/currency format) -- skip
        item_id = int(m.group(1))
        rel_group_path = rel_group_path_raw.decode("utf-8") if rel_group_path_raw else ""
        segments = [group_name_str]
        if rel_group_path:
            segments.extend(p for p in rel_group_path.split(GROUP_SEP) if p)
        items.append(TsmGroupItem(
            item_id=item_id,
            group_path="/".join(segments),
            raw_item_string=item_string,
        ))

    return TsmGroupExport(group_name=group_name_str, items=items)
