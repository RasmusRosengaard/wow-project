#!/usr/bin/env python3
"""Export the in-game addon's Data.lua from the existing pipeline.

The addon sandbox has no HTTP, so everything it knows has to arrive as a Lua
file loaded at login/"/reload". This is the bridge: it joins the two numbers
the addon filters on and writes them out.

  source_count  -- appearance.py / data/appearances.json. How many distinct
                   ItemIDs grant this item's appearance. 1 == sole-source ==
                   "unique appearance". THE headline filter.
  reference      -- what the item is worth elsewhere, so the addon can judge a
                   listing on the realm the player is standing on. Two are
                   exported, both already shown to users today by snipe_check:
                     r = the sell realm's own current cheapest listing
                         (the product's pricing decision, 2026-07-25)
                     m = region_median_cheapest, the median across every other
                         EU realm's own cheapest listing

Both are exported because for a *sole-source transmog* item the sell realm
very often has no listing at all, which would leave `r` empty for exactly the
items the addon exists to find. Which one the addon actually uses is NOT
decided here -- it ships `r` as primary with `m` available, and the addon's
`useRegionFallback` flag (default off) is the switch. See open question 3 in
.claude/docs/feature-ingame-sniper.md; per CLAUDE.md the mechanism is
proposed, the calibration is the human's.

PRICES ARE COPPER end to end, as everywhere else in this project. The Lua
side formats gold only at the display boundary.

Requires: scan_region.py already run (data/listings/*.parquet), a snapshot for
--sell (data/snapshots/{sell}/*.parquet), and appearance.py --refresh.

Usage:
  python export_addon_data.py --sell 1403
  python export_addon_data.py --sell 1403 --max-sources 3 --min-value-g 500
  python export_addon_data.py --sell 1403 --stats-only
"""
import argparse
import time
from pathlib import Path

import duckdb

from appearance import AppearanceCache
from item_names import NameCache
from snipe_check import NON_TRANSMOG_INVENTORY_TYPES

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DEFAULT_OUT = ROOT / "addon" / "RealmArbSniper" / "Data.lua"

SCHEMA = 1

# Resolving inventory_type is a per-item network fetch on a cold NameCache, so
# cap how many items one export will resolve. Items past the cap keep their
# unresolved slot and survive the profession filter (see _drop_profession_items).
NAME_RESOLVE_LIMIT = 20000


def _reference_prices(sell_cr: int) -> dict[int, dict]:
    """{item_id: {"r": sell_realm_cheapest|None, "m": region_median|None}}, copper.

    Deliberately equipment-only: rows carrying a pet identity are excluded,
    since caged pets have no transmog appearance and this table exists to feed
    an appearance filter. That also lets the table key on a bare item_id,
    matching the addon's Lua table, rather than the product's full
    (item_id, pet_species_id, pet_quality_id) market key.
    """
    con = duckdb.connect()
    snaps_glob = (DATA / "snapshots" / str(sell_cr) / "*.parquet").as_posix()
    listings_glob = (DATA / "listings" / "*.parquet").as_posix()

    con.execute(f"CREATE VIEW snaps AS SELECT * FROM read_parquet('{snaps_glob}')")
    # Same exclusion snipe_check.find_snipes applies: the sell realm is the
    # reference, never its own buy-side candidate.
    con.execute(f"""
        CREATE VIEW listings AS
        SELECT * FROM read_parquet('{listings_glob}')
        WHERE cr_id != {int(sell_cr)} AND buyout IS NOT NULL
    """)

    rows = con.execute("""
        WITH sell_now AS (
            -- Overall cheapest current listing per item, across every
            -- bonus_key it has -- bonus/ilvl variance is one market
            -- (2026-07-26), so it must not split this.
            SELECT item_id, min(buyout * 1.0 / quantity) AS cheapest_now
            FROM snaps
            WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM snaps)
              AND buyout IS NOT NULL
              AND pet_species_id IS NULL
            GROUP BY item_id
        ),
        region_realm_floor AS (
            SELECT cr_id, item_id, min(buyout * 1.0 / quantity) AS realm_cheapest
            FROM listings
            WHERE pet_species_id IS NULL
            GROUP BY cr_id, item_id
        ),
        region_stats AS (
            -- Median of the per-realm floors, not of every individual
            -- auction -- the latter would over-weight realms with more
            -- sellers. Mirrors snipe_check.region_stats exactly.
            SELECT item_id, median(realm_cheapest) AS region_median_cheapest
            FROM region_realm_floor
            GROUP BY item_id
        )
        SELECT COALESCE(s.item_id, r.item_id) AS item_id,
               s.cheapest_now,
               r.region_median_cheapest
        FROM sell_now s
        FULL OUTER JOIN region_stats r ON s.item_id = r.item_id
    """).fetchall()
    con.close()

    out: dict[int, dict] = {}
    for item_id, cheapest_now, region_median in rows:
        if item_id is None:
            continue
        out[int(item_id)] = {
            "r": round(cheapest_now) if cheapest_now is not None else None,
            "m": round(region_median) if region_median is not None else None,
        }
    return out


def _drop_profession_items(item_ids: list[int]) -> set[int]:
    """Profession tool/accessory slots, which must be excluded even when their
    source_count looks unique -- a small item pool trivially produces a low
    count, but neither slot is part of the visible paperdoll model. Same rule
    (and same defensive handling of an unresolved type) as
    snipe_check._filter_by_appearance; without it the addon's headline filter
    would confidently flag profession tools as rare transmog.
    """
    names = NameCache()
    names.ensure_many(item_ids, limit=NAME_RESOLVE_LIMIT)
    dropped = set()
    for item_id in item_ids:
        # An unresolved item_id yields None, which is not in the set, so it is
        # KEPT -- matching snipe_check rather than silently pruning on a cache
        # miss.
        if names.inventory_type(item_id) in NON_TRANSMOG_INVENTORY_TYPES:
            dropped.add(item_id)
    names.save()
    return dropped


def build_rows(sell_cr: int, max_sources: int, min_value_g: float | None) -> tuple[dict, dict]:
    """Returns (rows, stats). rows is {item_id: {"s":, "r":, "m":}}."""
    appearances = AppearanceCache()
    prices = _reference_prices(sell_cr)

    stats = {
        "priced_items": len(prices),
        "no_appearance_data": 0,
        "shared_appearance": 0,
        "below_value_floor": 0,
        "profession_slot": 0,
    }

    floor_copper = min_value_g * 10000 if min_value_g is not None else None

    candidates: dict[int, dict] = {}
    for item_id, price in prices.items():
        source_count = appearances.source_count(item_id)
        if source_count is None:
            stats["no_appearance_data"] += 1
            continue
        if source_count > max_sources:
            stats["shared_appearance"] += 1
            continue
        if floor_copper is not None:
            # Keep the row if EITHER reference clears the floor; only drop when
            # both fall short. Same OR-to-keep shape as find_snipes'
            # min_value_floor_g, so a missing sell-realm listing can't by
            # itself delete an item the region says is valuable.
            best = max((v for v in (price["r"], price["m"]) if v is not None), default=None)
            if best is None or best < floor_copper:
                stats["below_value_floor"] += 1
                continue
        candidates[item_id] = {"s": source_count, "r": price["r"], "m": price["m"]}

    dropped = _drop_profession_items(sorted(candidates))
    stats["profession_slot"] = len(dropped)
    rows = {k: v for k, v in candidates.items() if k not in dropped}
    stats["exported"] = len(rows)
    if appearances.is_stale():
        stats["appearance_cache_stale"] = True
    return rows, stats


def render_lua(rows: dict, sell_cr: int, max_sources: int) -> str:
    lines = [
        "-- Data.lua -- GENERATED by export_addon_data.py. DO NOT EDIT BY HAND.",
        "--",
        f"-- sell realm cr_id : {sell_cr}",
        f"-- max source_count : {max_sources}",
        f"-- items            : {len(rows)}",
        "--",
        "-- Schema, per item:",
        "--   s = source_count       (1 == sole-source appearance)",
        "--   r = sell realm's current cheapest listing, COPPER, nil if unlisted",
        "--   m = region median of per-realm cheapest, COPPER, nil if unlisted",
        "",
        "local ADDON, ns = ...",
        "",
        "ns.Data = {",
        f"    schema = {SCHEMA},",
        f"    generated = {int(time.time())},",
        '    baseline = "sell_realm_cheapest",',
        "    sellRealm = %d," % sell_cr,
        "    items = {",
    ]
    for item_id in sorted(rows):
        row = rows[item_id]
        parts = [f"s={row['s']}"]
        if row["r"] is not None:
            parts.append(f"r={row['r']}")
        if row["m"] is not None:
            parts.append(f"m={row['m']}")
        lines.append(f"        [{item_id}]={{{','.join(parts)}}},")
    lines += ["    },", "}", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sell", type=int, required=True, help="sell realm cr-id (reference price)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"output path (default {DEFAULT_OUT})")
    ap.add_argument("--max-sources", type=int, default=1,
                    help="keep appearances shared by at most N items (default 1 = sole-source)")
    ap.add_argument("--min-value-g", type=float, default=None,
                    help="drop items whose every reference price is below this many gold")
    ap.add_argument("--stats-only", action="store_true", help="report counts, write nothing")
    args = ap.parse_args()

    rows, stats = build_rows(args.sell, args.max_sources, args.min_value_g)

    print(f"priced items region-wide : {stats['priced_items']}")
    print(f"  no appearance data     : -{stats['no_appearance_data']}")
    print(f"  shared appearance      : -{stats['shared_appearance']}")
    print(f"  below value floor      : -{stats['below_value_floor']}")
    print(f"  profession slot        : -{stats['profession_slot']}")
    print(f"exported                 : {stats['exported']}")
    if stats.get("appearance_cache_stale"):
        print("WARNING: appearances.json is stale -- run `python appearance.py --refresh`")
    if not rows:
        print("nothing to export; not writing")
        return
    if args.stats_only:
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_lua(rows, args.sell, args.max_sources), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
