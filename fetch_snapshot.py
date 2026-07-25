#!/usr/bin/env python3
"""Collect hourly auction-house snapshots for one connected realm into parquet files.

Usage:
  python fetch_snapshot.py --find silvermoon        # look up your connected-realm id
  python fetch_snapshot.py --cr-id 1096             # fetch once (only if new data)
  python fetch_snapshot.py --cr-id 1096 --loop      # poll every 10 min; leave running 48h+

Blizzard publishes a new AH dump roughly hourly; If-Modified-Since means the loop
only downloads when there's actually a new snapshot (~6 tiny requests/hour otherwise).
"""
import argparse
import json
import logging
import logging.handlers
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from blizz import api_get, find_connected_realm

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

log = logging.getLogger("collector")

# HTTP statuses worth retrying with backoff instead of failing the poll.
RETRYABLE = {429, 500, 502, 503, 504}


def setup_logging() -> None:
    log_dir = DATA / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_h = logging.handlers.RotatingFileHandler(
        log_dir / "collector.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console_h = logging.StreamHandler()
    for h in (file_h, console_h):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)

SCHEMA = pa.schema([
    ("snapshot_ts", pa.int64()),      # epoch seconds, taken from Last-Modified header
    ("auction_id", pa.int64()),       # stable per-auction id -> the diff key
    ("item_id", pa.int64()),
    ("bonus_key", pa.string()),       # canonical hash of bonus_lists + modifiers
    ("pet_species_id", pa.int64()),   # battle pets (item 82800 cages)
    ("pet_quality_id", pa.int64()),
    ("pet_level", pa.int64()),
    ("buyout", pa.int64()),           # copper; None = bid-only auction
    ("bid", pa.int64()),
    ("quantity", pa.int64()),
    ("time_left", pa.string()),       # SHORT <30m | MEDIUM 30m-2h | LONG 2-12h | VERY_LONG 12-48h
])


def bonus_key(item: dict) -> str:
    """Canonical string for an item's bonus lists + modifiers, so two listings of the
    'same' piece of gear (same ilvl, tertiary, crafted stats...) compare equal."""
    parts = []
    bl = sorted(item.get("bonus_lists") or [])
    if bl:
        parts.append("b:" + ",".join(map(str, bl)))
    mods = sorted((m.get("type", -1), m.get("value", -1))
                  for m in (item.get("modifiers") or []))
    if mods:
        parts.append("m:" + ",".join(f"{t}={v}" for t, v in mods))
    return "|".join(parts)


# Undocumented by Blizzard (same caveat as modifier 28's ilvl use in
# dashboard.py) -- this is inference from real production data, not a
# documented fact. Found 2026-07-23 investigating item 238014 (Sun-Blessed
# Sickle, a crafted item) reporting wildly wrong prices: type 44 values
# increment sequentially per craft (245822, 245823, 245824...) -- a
# per-instance serial number, never meaningful for price comparison; type 42
# varies continuously within an otherwise-identical bonus_lists+modifiers
# group -- the crafted stat roll, granular enough that near-identical items
# almost never share an exact bonus_key. Together they fragmented one liquid
# ~1000-1300g market into 25+ near-unique variants, each with 1-2 sales --
# thin enough that a single misclassified camped relist (a known separate
# bug, see diff_snapshots.py's blind-spot note) could dominate a variant's
# entire percentile.
#
# Type 9 added 2026-07-24, investigating item 7761 (Steelclaw Reaver, a
# non-crafted level-21 rare weapon): region-wide listings carried nine
# distinct m:9=NN values (25/30/60/61/79/80/81/84/90) for what a human
# confirmed is the same transmog appearance regardless of value -- i.e. not
# a real, value-changing variant, same category as 42/44 even though this
# item isn't crafted. Corroborating evidence from the same investigation:
# Draenor's own troll/camped listings priced identically (~398,605g)
# whether tagged 9=30 or 9=90, which wouldn't happen if the modifier were a
# stat that mattered. Consequence of NOT pooling it: a genuinely cheap
# region-wide listing (28,500g, tagged 9=30) never joined against Draenor's
# sell-side data for 9=90, so it was invisible to snipe_check entirely.
MARKET_IGNORE_MODIFIER_TYPES = {9, 42, 44}


# Same plausibility check dashboard.py's ilvl display uses (kept here, not
# duplicated in dashboard.py, since market_key() below now needs it too --
# dashboard.py imports these instead of defining its own copy). A claimed
# modifier-28 "item level" is only trusted if it's both within a generous
# multiple of the item's own catalog base level AND under an absolute
# ceiling -- real WoW item levels have never approached four digits, so
# ILVL_ABSOLUTE_MAX is deliberately generous headroom for future content,
# not a tightly-fitted bound. Two independent guards because neither alone
# covers both failure modes: the ratio catches implausible claims on low-
# base-level items (where even a moderate absolute value is nonsense), the
# absolute cap catches implausible claims on high-base-level items (where
# the ratio alone isn't tight enough) -- see dashboard.py's ILVL_* comment
# for the two real production cases (item 1112-vs-34, item 3031-vs-610)
# that motivated each guard.
#
# Multiplier lowered 5 -> 3 on 2026-07-25, investigating item 164353
# (Plundered Scalebane Claymore, base level 60): real live listings carried
# 28=186/189/289, all comfortably inside the old 5x ratio (300) and so left
# unpooled -- market_key() kept treating them as distinct markets from the
# item's real sales (28=645/670, already caught by 5x), so the bug this
# guard exists to fix only partially resolved. Verified 3x still keeps the
# one known-legitimate case distinct (base 600, claimed 636) while catching
# every known junk value across both real cases (164353 and 237468) -- see
# tests/test_market_key.py.
ILVL_PLAUSIBILITY_MULTIPLE = 3
ILVL_ABSOLUTE_MAX = 1000


def ilvl_plausible(claimed: int, base_level: int | None) -> bool:
    return (base_level is not None
            and claimed <= base_level * ILVL_PLAUSIBILITY_MULTIPLE
            and claimed <= ILVL_ABSOLUTE_MAX)


# Thresholds for snipe_check._populate_market_keys()'s per-item noise-bonus-
# id detection (see market_key()'s noise_bonus_ids docstring).
#
# **A single frequency threshold was tried first and shown live to be
# insufficient (2026-07-25, same day).** A 5%-only cutoff correctly caught
# item 36507's clearly-noise values (<=4%) but left its 6-14%-frequency
# noise untouched, since real dimensions on OTHER items (e.g. 109168's
# socket/gem bonuses) also sit as low as 14-17% -- no single frequency band
# separates "real" from "noise" across every item. Confirmed live: item
# 36507's own reported cheap listing still didn't surface as a snipe with
# the frequency-only version deployed.
#
# **Replaced with a structural test**: a value's frequency alone isn't
# enough signal; whether it has a *partner* is. Real dimensions come in
# recognizable shapes -- a companion pair that (almost) always appears
# together (e.g. item 109168's two-part gem/socket bonus, 9145 paired with
# 9148), or a small set of mutually-exclusive values that jointly cover
# most of the item's listings (e.g. a binary quality flag summing to ~100%
# of listings, or -- found investigating this exact fix -- item 244752's
# five-value item-level-upgrade-track system, each track mutually exclusive
# with the others, none individually below BASE_TAG_FREQ). Per-craft noise
# (item 36507's ~17 near-random instance ids, and several similarly-shaped
# items found by the same live investigation: 82070, 25218, 82194, 15009)
# has neither shape -- each value stands alone, no reliable partner.
#
# A pure cardinality check ("few distinct values in the ambiguous band =
# real, many = noise") was tried and rejected before this: it correctly
# handled every single-dimension case, but broke on item 210108, which
# legitimately has ~10 ambiguous-band values across several real paired
# dimensions stacked together -- a large TOTAL count driven entirely by
# real structure, not noise. The partner-based test judges each value on
# its own relationship to the data, so it isn't fooled by an item simply
# having several real dimensions at once.
#
# BASE_TAG_FREQ: a value this frequent is unambiguously a real, load-
# bearing tag (present on nearly every listing) -- always kept, and
# excluded from being used as evidence for any OTHER value's pairing (an
# ever-present tag trivially "sums to a lot" with anything, which would
# otherwise let genuine noise falsely pass the partition check against it
# -- caught live during calibration).
# COMPANION_THRESHOLD: how consistently two values must co-occur in the
# SAME bonus_key for that co-occurrence to count as a real, structured
# pairing rather than coincidence.
# MAX_EXCLUSIVE_GROUP / MIN_EXCLUSIVE_COVERAGE: a value's "exclusive
# partners" (other ambiguous-band values that never co-occur with it) count
# as a real small partition only if there are few of them (a real tier/
# quality system, not a big per-craft pool that also happens to never
# repeat within one listing) AND together they explain most of the item's
# observed listings.
# MIN_SAMPLES: below this floor there isn't enough data to trust any of the
# above -- nothing is stripped (same "not enough data means don't strip"
# principle as an unknown base_level).
#
# Validated live 2026-07-25 against every real case found so far: three
# per-craft-noise items (36507, 82070, 25218, plus 82194/15009 sharing the
# same shape) fully and correctly stripped down to their one real tag; six
# real multi-value items (109168, 114813, 210108, 244752, 239675, 240947,
# 244718, 244591 -- tier systems, quality flags, and paired gem/socket
# bonuses) fully preserved with zero false-positive stripping.
BONUS_NOISE_MIN_SAMPLES = 20
BONUS_NOISE_LOW_FREQUENCY = 0.05
BONUS_NOISE_BASE_TAG_FREQUENCY = 0.80
BONUS_NOISE_COMPANION_THRESHOLD = 0.7
BONUS_NOISE_MAX_EXCLUSIVE_GROUP = 8
BONUS_NOISE_MIN_EXCLUSIVE_COVERAGE = 0.8


def market_key(bk: str, base_level: int | None = None,
               noise_bonus_ids: frozenset[int] | None = None) -> str:
    """Coarser version of bonus_key for MATCHING/GROUPING only -- sold-price
    percentiles, the current-lowest-listing cap, relist detection, and the
    buy/sell join in snipe_check.py -- so near-identical crafted items pool
    into one market instead of fragmenting into dozens of 1-2-sale buckets
    (see MARKET_IGNORE_MODIFIER_TYPES above for why). The raw bonus_key is
    still stored and displayed as-is everywhere; only matching uses this.

    base_level (added 2026-07-25, optional, defaults to None) additionally
    strips modifier type 28 -- but ONLY when its claimed value fails
    ilvl_plausible() against this item's own catalog base level. Type 28
    isn't unconditionally ignorable like 9/42/44: it's genuinely meaningful
    ilvl-scaling data for *some* items (a real high-ilvl variant should stay
    a distinct, differently-priced market from a low-ilvl one), but for
    others it's junk that happens to look numeric -- e.g. item 164353
    (Plundered Scalebane Claymore, a Rare weapon, base level 60) had live
    listings tagged 28=186/189/645/670/289, none remotely close to 60,
    fragmenting one item into unrelated buckets that could never match each
    other for snipe purposes. Callers that don't have a base_level handy
    (diff_snapshots.py's relist_key(), still just market_key(bonus_key), no
    behavior change there) get the pre-2026-07-25 behavior unchanged --
    unknown base_level always means "don't strip 28," never "assume it's
    junk," since a wrong strip could incorrectly merge two items that really
    do have different, price-relevant item levels.

    noise_bonus_ids (added same day, once base_level's conditional-pooling
    pattern turned out not to be enough): a per-item set of `b:` bonus_lists
    ids to also strip. Real case that motivated this: item 36507
    (Iron-Molded Fist) had listings like `b:1706,6655|m:9=80,28=1099` vs
    `b:1681,6655|m:9=68,28=1747` -- a genuinely cheap 5,400g listing never
    matched the sell realm's own sales because every listing's *first*
    bonus_lists value (1677-1709, a different one almost every time) is
    per-craft instance noise, the exact same shape as the already-handled
    m:42/44, just expressed as a bonus_lists id instead of a modifier. This
    can't be a fixed global ignore-list like MARKET_IGNORE_MODIFIER_TYPES,
    though: a raw bonus id is just a number, not a typed field, so the same
    id could mean something completely different on a different item (see
    snipe_check._populate_market_keys() for how this set is determined --
    document-frequency based, per item: a value that repeats across a large
    fraction of an item's own listings is almost certainly a real,
    price-relevant dimension -- socket count, a quality-tier flag, a gem
    choice -- and MUST NOT be stripped; a value that shows up in only a
    handful out of hundreds is almost certainly per-craft noise). None
    (default, and every caller without item-specific frequency data handy)
    means "don't strip anything from the b: segment" -- same "unknown means
    don't strip" principle as base_level.

    Mirrored as a SQL macro in analyze.connect() for DuckDB-side grouping
    (sold-price/current-lowest queries) rather than shared as a single
    Python UDF -- this duckdb version's Python UDF support requires numpy,
    which isn't otherwise a project dependency (see CLAUDE.md's "minimal
    deps" convention) and is disproportionate weight for one string
    transform. The two implementations are kept honest by
    tests/test_market_key.py, which runs the same vectors through both and
    asserts identical results -- **except** noise_bonus_ids, which is
    Python-only (see snipe_check.py's "why Python, not SQL" note): the SQL
    macro only ever receives base_level, matching its pre-existing 2-arg
    signature."""
    if not bk:
        return bk
    ignore_types = set(MARKET_IGNORE_MODIFIER_TYPES)
    if base_level is not None:
        for part in bk.split("|"):
            if part.startswith("m:"):
                for pair in part[2:].split(","):
                    t, _, v = pair.partition("=")
                    if t == "28" and v and not ilvl_plausible(int(v), base_level):
                        ignore_types.add(28)
    parts = []
    for part in bk.split("|"):
        if part.startswith("b:"):
            if noise_bonus_ids:
                ids = [x for x in part[2:].split(",") if int(x) not in noise_bonus_ids]
                if ids:
                    parts.append("b:" + ",".join(ids))
                continue
            parts.append(part)
            continue
        if not part.startswith("m:"):
            parts.append(part)
            continue
        mods = [pair for pair in part[2:].split(",")
                if int(pair.split("=", 1)[0]) not in ignore_types]
        if mods:
            parts.append("m:" + ",".join(mods))
    return "|".join(parts)


def rows(payload: dict, ts: int) -> list[dict]:
    out = []
    for a in payload.get("auctions") or []:
        it = a.get("item") or {}
        out.append({
            "snapshot_ts": ts,
            "auction_id": a.get("id"),
            "item_id": it.get("id"),
            "bonus_key": bonus_key(it),
            "pet_species_id": it.get("pet_species_id"),
            "pet_quality_id": it.get("pet_quality_id"),
            "pet_level": it.get("pet_level"),
            "buyout": a.get("buyout"),
            "bid": a.get("bid"),
            "quantity": a.get("quantity", 1),
            "time_left": a.get("time_left"),
        })
    return out


def state_path(cr: int) -> Path:
    return DATA / "state" / f"{cr}.json"


def get_auctions_with_backoff(cr: int, headers: dict, attempts: int = 4):
    """GET the auctions endpoint, backing off on 429/5xx (honors Retry-After)."""
    delay = 30
    for attempt in range(1, attempts + 1):
        r = api_get(f"/data/wow/connected-realm/{cr}/auctions", "dynamic", headers=headers)
        if r.status_code not in RETRYABLE or attempt == attempts:
            return r
        wait = min(int(r.headers.get("Retry-After") or delay), 600)
        log.warning("HTTP %s from auctions endpoint, retrying in %ss (attempt %s/%s)",
                    r.status_code, wait, attempt, attempts)
        time.sleep(wait)
        delay *= 2
    return r


def fetch_once(cr: int) -> Path | None:
    sp = state_path(cr)
    headers = {}
    if sp.exists():
        lm = json.loads(sp.read_text()).get("last_modified")
        if lm:
            headers["If-Modified-Since"] = lm

    r = get_auctions_with_backoff(cr, headers)
    if r.status_code == 304:
        return None                     # no new hourly dump yet
    r.raise_for_status()

    lm = r.headers.get("Last-Modified")
    ts = int(parsedate_to_datetime(lm).timestamp()) if lm else int(time.time())
    out_dir = DATA / "snapshots" / str(cr)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{ts}.parquet"
    if out.exists():                    # same dump fetched twice (e.g. after restart)
        return None

    try:
        payload = r.json()
    except ValueError:
        # Truncated/garbled body: skip this poll; state file is untouched, so the
        # next poll re-requests the same dump.
        log.error("malformed JSON body (%s bytes), skipping this poll", len(r.content))
        return None

    table = pa.Table.from_pylist(rows(payload, ts), schema=SCHEMA)
    pq.write_table(table, out, compression="zstd")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"last_modified": lm}))
    log.info("saved %s: %s auctions", out.name, table.num_rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", metavar="REALM_SLUG",
                    help="look up connected-realm id by realm slug and exit")
    ap.add_argument("--cr-id", type=int, help="connected realm id to collect")
    ap.add_argument("--loop", action="store_true", help="poll every 10 minutes forever")
    args = ap.parse_args()

    if args.find:
        hits = find_connected_realm(args.find)
        if not hits:
            sys.exit(f"No connected realm found for slug '{args.find}' "
                     f"(slug = lowercase, spaces -> hyphens, e.g. 'tarren-mill').")
        for cr_id, realms in hits:
            print(f"connected-realm id {cr_id}: {', '.join(realms)}")
        return

    if not args.cr_id:
        ap.error("--cr-id required (use --find <realm> first)")

    setup_logging()

    if not args.loop:
        p = fetch_once(args.cr_id)
        print("no new snapshot yet" if p is None else f"wrote {p}")
        return

    log.info("collecting connected realm %s; ctrl-c to stop", args.cr_id)
    while True:
        try:
            fetch_once(args.cr_id)
        except Exception:               # keep the multi-day run alive no matter what
            log.exception("poll failed; retrying in 10 min")
        time.sleep(600)


if __name__ == "__main__":
    main()
