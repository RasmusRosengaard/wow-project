#!/usr/bin/env python3
"""Diff consecutive snapshots and classify every auction that disappeared.

The API only ever shows *listings*, never sales — so sales must be inferred:

  inferred_sale     gone while LONG/VERY_LONG (couldn't have expired within the
                    gap) and no identical relist appeared -> treat as sold
  likely_relisted   a matching (item, bonuses, qty) listing reappeared under a
                    new auction id at a similar price (+/-15%, see
                    RELIST_PRICE_TOLERANCE) -> cancel + repost, not a sale
  ambiguous         the time_left bucket allows expiry within the gap (MEDIUM,
                    or an oversized gap after collector downtime)
  likely_expired    gone from SHORT (<30 min left) -> almost always expired
  bid_only_gone     no buyout: can't be insta-bought; cancelled early or won at
                    expiry -> excluded from sale stats

Known blind spot: a cancel WITHOUT relist is indistinguishable from a sale.
The verification protocol in the README measures how big that noise floor is.

Writes data/events/<cr>.parquet. Recomputed from scratch each run (idempotent).
"""
import argparse
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from fetch_snapshot import market_key

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Minimum seconds remaining implied by each time_left bucket. If the gap between
# snapshots exceeds this, the auction *could* have expired -> ambiguous.
MIN_REMAINING = {"SHORT": 0, "MEDIUM": 30 * 60, "LONG": 2 * 3600, "VERY_LONG": 12 * 3600}

EVENT_SCHEMA = pa.schema([
    ("ts", pa.int64()),               # snapshot in which the auction was first missing
    ("auction_id", pa.int64()),
    ("item_id", pa.int64()),
    ("bonus_key", pa.string()),
    ("pet_species_id", pa.int64()),
    ("pet_quality_id", pa.int64()),
    ("unit_price", pa.float64()),     # buyout / quantity, in copper
    ("quantity", pa.int64()),
    ("time_left", pa.string()),       # bucket at last sighting
    ("gap_seconds", pa.int64()),
    ("classification", pa.string()),
])


def load_snapshot(path: Path) -> dict[int, dict]:
    return {r["auction_id"]: r for r in pq.read_table(path).to_pylist()}


# A troll camping a listing often reposts it at a different joke price
# instead of the exact same buyout -- requiring an exact price match let
# that slip through as a fake inferred_sale (real case, 2026-07-25: item
# 13051 "Witchfury", two camped listings at 647,999.98g and 667,999.98g,
# same bonus_key, ~3% apart -- misclassified as two separate sales instead
# of one relist). Price is matched within this tolerance band instead of
# exactly; deliberately loose enough to catch a same-day repost at a nearby
# price but not so loose it swallows a genuinely different sale.
RELIST_PRICE_TOLERANCE = 0.15


def relist_key(r: dict) -> tuple:
    # Pet identity lives in pet_species_id/pet_quality_id, not bonus_key (which
    # is empty for caged pets) -- without it, two different pets with the same
    # buyout+quantity would falsely match as a relist of one another.
    # market_key() (not the raw bonus_key) so relisting a crafted item still
    # matches even if Blizzard's undocumented per-craft modifiers (the stat
    # roll, a per-instance serial) render slightly differently on repost --
    # see fetch_snapshot.py's MARKET_IGNORE_MODIFIER_TYPES for why. Price is
    # deliberately excluded from this identity -- see RELIST_PRICE_TOLERANCE
    # above; matched separately by _find_relist_match() since it's now a
    # band, not exact equality.
    return (r["item_id"], market_key(r["bonus_key"]), r["pet_species_id"], r["pet_quality_id"],
            r["quantity"])


def _find_relist_match(buyout: int, candidates: list[int],
                       tolerance: float = RELIST_PRICE_TOLERANCE) -> int | None:
    """Index of the candidate buyout closest to `buyout` within +/-tolerance,
    or None if none qualify. An exact match (the pre-2026-07-25 behavior) is
    always within tolerance, so this is a strict superset of the old rule."""
    best_idx, best_diff = None, None
    for idx, cand in enumerate(candidates):
        diff = abs(cand - buyout)
        if diff <= buyout * tolerance and (best_diff is None or diff < best_diff):
            best_idx, best_diff = idx, diff
    return best_idx


def classify_pair(prev: dict[int, dict], curr: dict[int, dict],
                  gap: int, ts: int) -> list[dict]:
    gone = [prev[i] for i in prev.keys() - curr.keys()]
    # Brand-new listings in `curr`, grouped by non-price identity -- each
    # group holds every fresh candidate buyout still unconsumed, since price
    # matching is now a tolerance band, not an exact multiset lookup (see
    # RELIST_PRICE_TOLERANCE). Bid-only fresh listings (buyout None) can't
    # be a relist of a priced gone auction, so they're excluded here.
    fresh_by_key: dict[tuple, list[int]] = defaultdict(list)
    for i in curr.keys() - prev.keys():
        c = curr[i]
        if c["buyout"] is not None:
            fresh_by_key[relist_key(c)].append(c["buyout"])

    events = []
    for r in gone:
        tl = r["time_left"] or "MEDIUM"
        if r["buyout"] is None:
            cls = "bid_only_gone"
        elif tl == "SHORT":
            cls = "likely_expired"
        else:
            candidates = fresh_by_key.get(relist_key(r), [])
            match_idx = _find_relist_match(r["buyout"], candidates)
            if match_idx is not None:
                candidates.pop(match_idx)
                cls = "likely_relisted"
            elif gap >= MIN_REMAINING.get(tl, 0):
                cls = "ambiguous"
            else:
                cls = "inferred_sale"

        q = r["quantity"] or 1
        events.append({
            "ts": ts,
            "auction_id": r["auction_id"],
            "item_id": r["item_id"],
            "bonus_key": r["bonus_key"],
            "pet_species_id": r["pet_species_id"],
            "pet_quality_id": r["pet_quality_id"],
            "unit_price": (r["buyout"] / q) if r["buyout"] is not None else None,
            "quantity": q,
            "time_left": tl,
            "gap_seconds": gap,
            "classification": cls,
        })
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cr-id", type=int, required=True)
    args = ap.parse_args()

    snap_dir = DATA / "snapshots" / str(args.cr_id)
    paths = sorted(snap_dir.glob("*.parquet"), key=lambda p: int(p.stem))
    if len(paths) < 2:
        raise SystemExit(f"Need at least 2 snapshots in {snap_dir}, found {len(paths)}.")

    all_events: list[dict] = []
    prev_path, prev = paths[0], load_snapshot(paths[0])
    for path in paths[1:]:
        curr = load_snapshot(path)
        ts, prev_ts = int(path.stem), int(prev_path.stem)
        all_events += classify_pair(prev, curr, gap=ts - prev_ts, ts=ts)
        prev_path, prev = path, curr

    out_dir = DATA / "events"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.cr_id}.parquet"
    pq.write_table(pa.Table.from_pylist(all_events, schema=EVENT_SCHEMA),
                   out, compression="zstd")

    counts = Counter(e["classification"] for e in all_events)
    print(f"{len(paths)} snapshots -> {len(all_events)} disappearance events")
    for k, v in counts.most_common():
        print(f"  {k:16s} {v}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
