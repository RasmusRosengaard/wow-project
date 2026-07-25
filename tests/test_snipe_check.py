"""Tests for snipe_check.py: joins the sell realm's own current cheapest
listing against region-scanner listings elsewhere. Reuses the synthetic-
pipeline pattern from test_pipeline.py.

Pricing model changed 2026-07-25 (human product decision): sell price is
the sell realm's current cheapest live listing, not an inferred sold-price
percentile -- see snipe_check.find_snipes()'s docstring for the full
history of why. Fixtures below establish price via a CURRENT listing
(present at the latest snapshot_ts) rather than a listing that vanishes
into `sales`."""
import sys

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import analyze
import appearance
import diff_snapshots
import item_names
import snipe_check
from fetch_snapshot import SCHEMA, market_key
from scan_region import LISTING_SCHEMA

SELL_CR = 9999
BUY_CR_A = 1111
T0, T1 = 1_700_000_000, 1_700_003_600


@pytest.fixture(autouse=True)
def isolate_appearance_cache(tmp_path, monkeypatch):
    """find_snipes() instantiates a real appearance.AppearanceCache, which
    reads CACHE_PATH (data/appearances.json) unless redirected -- no test
    should depend on whatever the real, gitignored local cache contains."""
    monkeypatch.setattr(appearance, "CACHE_PATH", tmp_path / "appearances_test_cache.json")


@pytest.fixture(autouse=True)
def isolate_item_names_cache(tmp_path, monkeypatch):
    """When max_appearance_sources is set, find_snipes() also instantiates a
    real item_names.NameCache (to exclude profession tools) -- same
    isolation reasoning as isolate_appearance_cache above, plus a stub for
    the live Blizzard API call so these tests stay offline/deterministic.
    Default: no item is a profession tool, matching the vast majority of
    real items -- tests that care about the exclusion override this."""
    monkeypatch.setattr(item_names, "CACHE_PATH", tmp_path / "item_names_test_cache.json")
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": None, "quality": None, "level": None,
                                          "inventory_type": None})


def write_appearance_cache(path, item_sources: dict[int, int]):
    """item_sources: item_id -> source_count. Gives each item its own
    appearance_id (item_id + 1_000_000) so distinct items in a test don't
    accidentally collide -- only source_count is under test."""
    data = {
        "fetched_at": 1_700_000_000,
        "items": {
            str(item_id): {"appearance_id": item_id + 1_000_000, "source_count": count}
            for item_id, count in item_sources.items()
        },
    }
    path.write_text(json.dumps(data))


def snap_row(auction_id, ts, item_id=101, buyout=20_000, quantity=1,
             time_left="VERY_LONG", bonus_key="", pet_species_id=None, pet_quality_id=None):
    return {
        "snapshot_ts": ts, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": pet_species_id,
        "pet_quality_id": pet_quality_id,
        "pet_level": None, "buyout": buyout, "bid": None,
        "quantity": quantity, "time_left": time_left,
    }


def listing_row(cr, item_id, buyout, auction_id, bonus_key="",
                 pet_species_id=None, pet_quality_id=None):
    return {
        "cr_id": cr, "fetched_ts": T1, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": pet_species_id,
        "pet_quality_id": pet_quality_id,
        "pet_level": None, "buyout": buyout, "bid": None, "quantity": 1,
        "time_left": "VERY_LONG",
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    # item 101's sell price is its current live listing: 20_000 copper = 2g.
    prev = [snap_row(3, T0, item_id=103)]   # survives -> no event
    curr = [
        snap_row(3, T1, item_id=103),        # survives -> no event
        snap_row(10, T1, item_id=101, buyout=20_000),  # current sell-realm listing: 2g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100),  # cheap -> snipe
        listing_row(BUY_CR_A, item_id=101, buyout=25_000, auction_id=101),  # pricier -> not a snipe
        listing_row(SELL_CR, item_id=101, buyout=5_000, auction_id=102),    # sell realm itself -> excluded
        listing_row(BUY_CR_A, item_id=999, buyout=1, auction_id=103),       # no sell-realm listing -> no match
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")
    return tmp_path


def run_diff(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["diff_snapshots.py", "--cr-id", str(SELL_CR)])
    diff_snapshots.main()


def test_find_snipes_flags_cheap_listing_and_excludes_others(data_dir, monkeypatch):
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["buy_realm"] == BUY_CR_A
    assert r["item_id"] == 101
    assert r["auction_id"] == 100
    assert r["buy_g"] == pytest.approx(1.0)      # 10_000 copper = 1g
    assert r["sell_p_g"] == pytest.approx(2.0)   # sell realm's current listing: 20_000 copper
    assert r["discount_pct"] > 30


def test_find_snipes_rows_carry_market_key(data_dir, monkeypatch):
    """market_key (added 2026-07-24, previously computed for the join but
    excluded from the output) is what dashboard.html now groups rows by
    instead of the exact bonus_key -- real listings across different realms
    often share a market_key (same price) without sharing an exact
    bonus_key (per-instance modifiers), so the frontend needs this to
    collapse them into one group instead of splitting an already-identically
    -priced market into separate rows."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert rows[0]["market_key"] == market_key(rows[0]["bonus_key"] or "")


def test_find_snipes_does_not_conflate_pet_species(tmp_path, monkeypatch):
    """Caged pets (item 82800) have no bonus_key, so two different species/
    qualities must not be lumped into one price bucket -- a cheap poor pet
    should be judged against the poor pet's own current listing, not a rare
    pet's."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    PET_ITEM = 82800

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(5, T0, item_id=103)]  # survives -> no event
    curr = [
        snap_row(5, T1, item_id=103),
        snap_row(1, T1, item_id=PET_ITEM, buyout=500_000, pet_species_id=1, pet_quality_id=4),
        snap_row(2, T1, item_id=PET_ITEM, buyout=4_000, pet_species_id=2, pet_quality_id=1),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, PET_ITEM, buyout=1_000, auction_id=200,
                    pet_species_id=2, pet_quality_id=1),
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["pet_species_id"] == 2
    assert r["pet_quality_id"] == 1
    assert r["sell_p_g"] == pytest.approx(0.4)  # poor pet's own listing (4_000 copper), not the rare pet's 50g


def test_find_snipes_respects_items_filter(data_dir, monkeypatch):
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, items=[999], min_discount=0.3) == []


def test_main_prints_results_and_caveat(data_dir, monkeypatch, capsys):
    run_diff(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["snipe_check.py", "--sell", str(SELL_CR),
                                      "--min-discount", "0.3"])
    snipe_check.main()
    out = capsys.readouterr().out
    assert "101" in out
    assert snipe_check.CAVEAT in out


def test_find_snipes_sort_gold_orders_by_sell_price(tmp_path, monkeypatch):
    """--sort gold (-g) should rank by absolute sell-price value, not discount%
    -- a low-value item with a bigger discount% shouldn't outrank an
    expensive item with a smaller (but still qualifying) discount%."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(10, T1, item_id=101, buyout=20_000),   # current price 2g
        snap_row(11, T1, item_id=105, buyout=500_000),  # current price 50g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100),   # ~47.4% discount
        listing_row(BUY_CR_A, item_id=105, buyout=308_750, auction_id=200),  # ~35.0% discount
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    default_order = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert [r["item_id"] for r in default_order] == [101, 105]  # higher discount% first

    gold_order = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, sort="gold")
    assert [r["item_id"] for r in gold_order] == [105, 101]  # higher sell_p_g first


def test_find_snipes_max_per_item_caps_and_keeps_best_discounts(tmp_path, monkeypatch):
    """One item with three qualifying listings at different discounts --
    max_per_item=2 should keep only the two highest-discount ones (cheapest
    buy price), dropping the weakest, not an arbitrary two."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(10, T1, item_id=101, buyout=20_000),  # current price 2g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, item_id=101, buyout=5_000, auction_id=100),   # best discount
        listing_row(BUY_CR_A, item_id=101, buyout=8_000, auction_id=101),   # 2nd best
        listing_row(BUY_CR_A, item_id=101, buyout=12_000, auction_id=102),  # weakest -- should be dropped
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    all_rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(all_rows) == 3

    capped = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, max_per_item=2)
    assert len(capped) == 2
    assert {r["auction_id"] for r in capped} == {100, 101}


def test_find_snipes_pools_near_identical_crafted_rolls(tmp_path, monkeypatch):
    """Reproduces the real production case (item 238014, Sun-Blessed Sickle,
    2026-07-23): a crafted item's per-craft stat roll/serial (modifier types
    42/44) fragmented what should be one liquid market into dozens of
    near-unique exact bonus_keys. The sell realm's current listing (roll_d)
    and the buy-side listing (roll_c) have completely different exact rolls
    but must still pool to the same market_key and match."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    roll_d = "b:1,2,3|m:42=300"  # currently listed on the sell realm
    roll_c = "b:1,2,3|m:42=400"  # the buy-side listing's roll -- a totally different exact craft

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(4, T1, item_id=500, bonus_key=roll_d, buyout=21_000),  # current price 2.1g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, item_id=500, buyout=5_000, auction_id=100, bonus_key=roll_c)]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["item_id"] == 500
    assert r["bonus_key"] == roll_c          # exact listing roll still shown for display
    assert r["market_key"] == market_key(roll_c) == market_key(roll_d)
    assert r["sell_p_g"] == pytest.approx(2.1)   # the sell realm's current listing (roll_d), 21_000 copper


def test_find_snipes_pools_near_identical_bonus_list_noise(tmp_path, monkeypatch):
    """Reproduces the real production case (item 36507, Iron-Molded Fist,
    reported 2026-07-25): a genuinely cheap listing on another realm never
    matched the sell realm's own listing because every listing carried a
    different per-craft "instance" bonus_lists id alongside one shared,
    stable id -- the same fragmentation shape as the already-handled
    m:42/44 crafted-roll case, just expressed via `b:` instead of `m:`.
    market_key()'s static ignore-list can't fix this (a raw bonus id isn't
    a typed field the way a modifier type is); _populate_market_keys()'s
    structural (companion/partition) detection has to recognize the
    varying id as noise from the data itself -- here, each noise id sits
    below BONUS_NOISE_LOW_FREQUENCY on its own, so this specific test only
    exercises that simple low-frequency floor, not the companion/partition
    logic (see the two tests below for that). Needs BONUS_NOISE_MIN_SAMPLES
    (20) real samples for the detection to trigger at all -- below that
    floor nothing would be stripped, matching the "not enough data means
    don't strip" default for base_level. All N variants are simultaneously
    live listings (not historical sales -- pricing no longer looks at
    sales), which also happens to be exactly what feeds the noise-detection
    sample."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    STABLE_ID = 9000
    N = 22  # >= BONUS_NOISE_MIN_SAMPLES; each noise id then appears ~1/23 (~4.3%) < 5%

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103)] + [
        snap_row(i, T1, item_id=600, bonus_key=f"b:{7000 + i},{STABLE_ID}", buyout=20_000)
        for i in range(N)
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # Buy-side listing's exact roll never appeared in the sell realm's own
    # current listings at all -- the real reported shape (Blackrock's
    # 5,400g listing vs Draenor's own listings, no exact bonus_key overlap).
    buy_rows = [listing_row(BUY_CR_A, item_id=600, buyout=5_000, auction_id=100,
                            bonus_key=f"b:8500,{STABLE_ID}")]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["item_id"] == 600
    assert r["bonus_key"] == f"b:8500,{STABLE_ID}"   # exact listing roll still shown for display
    assert r["market_key"] == f"b:{STABLE_ID}"        # noise id stripped, stable id kept
    assert r["sell_p_g"] == pytest.approx(2.0)         # pooled current listing price (20_000 copper)


def test_find_snipes_keeps_companion_bonus_pair_distinct(tmp_path, monkeypatch):
    """A frequency-only cutoff (the first version of this fix, shipped and
    live-reverted the same day) can't tell noise from a real dimension when
    both sit in the same ambiguous frequency band -- e.g. item 109168's
    real two-part gem/socket bonus (9145 always paired with 9148) sat at
    just 8.7% document frequency, comparable to some of item 36507's actual
    noise. The fix that replaced it: two values that reliably co-occur
    together in the same bonus_key (a "companion" pair) are real and must
    stay distinct, never pooled away -- regardless of how low their
    individual frequency is. Here, listings carrying the companion pair are
    genuinely currently listed for 5x what listings without it are; if the
    pair were wrongly stripped, both groups would incorrectly pool into one
    blended (and wrong) sell price."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    STABLE_ID = 9000

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = (
        [snap_row(9999, T1, item_id=103)]
        + [snap_row(i, T1, item_id=700, bonus_key=f"b:{STABLE_ID},9001,9002", buyout=100_000)
           for i in range(15)]
        + [snap_row(100 + i, T1, item_id=700, bonus_key=f"b:{STABLE_ID}", buyout=20_000)
           for i in range(10)]
    )
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # Cheap listing WITH the companion pair -- must match against the
    # 100_000-copper group (its own real market), not the 20_000-copper
    # base-tag-only group, which would happen if 9001/9002 got pooled away.
    buy_rows = [listing_row(BUY_CR_A, item_id=700, buyout=50_000, auction_id=100,
                            bonus_key=f"b:{STABLE_ID},9001,9002")]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["item_id"] == 700
    # 9001/9002 correctly kept -- market_key still carries them, not stripped
    assert "9001" in r["market_key"] and "9002" in r["market_key"]
    assert r["sell_p_g"] == pytest.approx(10.0)  # the WITH-pair group's own price, not 2.0


def test_find_snipes_keeps_partition_bonus_values_distinct(tmp_path, monkeypatch):
    """Reproduces item 244752's real shape: a small set of mutually-
    exclusive bonus values (never co-occurring with each other) that
    jointly cover nearly all of an item's listings -- a real item-level-
    upgrade-track system. No single pair of these values sums close to
    100% on its own (there are 3+ of them sharing the space), so this
    specifically exercises the N-way partition detection, not just a
    2-value companion/partition pair. Each tier is genuinely currently
    listed at a different price; pooling them would blend three real
    markets into one wrong sell price."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    STABLE_ID = 8000

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = (
        [snap_row(9999, T1, item_id=103)]
        + [snap_row(i, T1, item_id=800, bonus_key=f"b:{STABLE_ID},8100", buyout=20_000)
           for i in range(16)]
        + [snap_row(100 + i, T1, item_id=800, bonus_key=f"b:{STABLE_ID},8200", buyout=40_000)
           for i in range(14)]
        + [snap_row(200 + i, T1, item_id=800, bonus_key=f"b:{STABLE_ID},8300", buyout=80_000)
           for i in range(10)]
    )
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # Cheap listing at tier 8200 -- must match against the 40_000-copper
    # tier-8200 group specifically, not a blended price across all three
    # tiers, which would happen if 8100/8200/8300 got pooled away.
    buy_rows = [listing_row(BUY_CR_A, item_id=800, buyout=20_000, auction_id=100,
                            bonus_key=f"b:{STABLE_ID},8200")]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["item_id"] == 800
    assert "8200" in r["market_key"]  # tier value correctly kept, not stripped
    assert r["sell_p_g"] == pytest.approx(4.0)  # tier 8200's own current listing (40_000 copper), not a blend


def test_find_snipes_respects_min_gold(data_dir, monkeypatch):
    """The only listing cheap enough to qualify (10_000 copper = 1g) is
    excluded once min_gold asks for at least 2g."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_gold=2) == []


def test_find_snipes_respects_max_gold(data_dir, monkeypatch):
    """The qualifying listing is 1g -- a max_gold below that excludes it,
    a max_gold above it keeps it."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, max_gold=0.5) == []
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, max_gold=5)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_find_snipes_respects_min_sell_now(data_dir, monkeypatch):
    """min_sell_now filters on the sell realm's current price directly (the
    same number that's now the sell price) -- excludes low-value junk
    regardless of discount%."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    # item 101's sell price is 2g -- a floor above that excludes it.
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_sell_now=3) == []
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_sell_now=1)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_find_snipes_excludes_items_with_no_current_sell_listing(tmp_path, monkeypatch):
    """With no sold-price fallback left, an item with no current listing on
    the sell realm has no price to compare against at all -- it must never
    appear, regardless of how cheap it is listed elsewhere."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [snap_row(3, T1, item_id=103)]  # item 101 never listed on the sell realm at all
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, item_id=101, buyout=1, auction_id=100)]  # absurdly cheap
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.0) == []


def test_find_snipes_reports_appearance_sources_without_filtering(data_dir, monkeypatch):
    """With no max_appearance_sources set, appearance_sources is attached to
    every row for display but nothing gets dropped -- including items the
    cache has no entry for (appearance_sources is None, not excluded)."""
    run_diff(monkeypatch)
    write_appearance_cache(appearance.CACHE_PATH, {101: 3})
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    assert rows[0]["appearance_sources"] == 3


def test_find_snipes_max_appearance_sources_filters_common_looks(data_dir, monkeypatch):
    """Item 101's appearance is shared by 3 items -- --max-appearance-sources 1
    ("only looks no other item grants") should drop it; raising the cap to 3
    lets it back through."""
    run_diff(monkeypatch)
    write_appearance_cache(appearance.CACHE_PATH, {101: 3})
    con = analyze.connect(SELL_CR)

    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   max_appearance_sources=1) == []

    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   max_appearance_sources=3)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_find_snipes_max_appearance_sources_excludes_uncached_items(data_dir, monkeypatch):
    """An item missing from the appearance cache has appearance_sources=None
    -- when a rarity filter is actively requested, an unknown item can't be
    proven rare, so it's excluded rather than let through by default."""
    run_diff(monkeypatch)
    # cache built, but has no entry for item 101
    write_appearance_cache(appearance.CACHE_PATH, {999: 1})
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   max_appearance_sources=5) == []


def test_find_snipes_max_appearance_sources_excludes_profession_tools(data_dir, monkeypatch):
    """A profession tool (Mining Pick, Fishing Pole, etc.) trivially has a
    low appearance_sources just because few items share that slot's model --
    but those slots aren't part of the visible paperdoll/transmog system at
    all, so it must be excluded even though it looks "unique" by the raw
    count. Confirmed live: inventory_type.type == "PROFESSION_TOOL" for
    real items (Mining Pick, Blacksmith Hammer, Fishing Pole)."""
    run_diff(monkeypatch)
    write_appearance_cache(appearance.CACHE_PATH, {101: 1})
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": None, "quality": None, "level": None,
                                          "inventory_type": "PROFESSION_TOOL"})
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   max_appearance_sources=1) == []


def test_find_snipes_max_appearance_sources_keeps_real_gear(data_dir, monkeypatch):
    """A normal equipment slot (e.g. a head item) is unaffected by the
    profession-tool exclusion -- only PROFESSION_TOOL/PROFESSION_GEAR are."""
    run_diff(monkeypatch)
    write_appearance_cache(appearance.CACHE_PATH, {101: 1})
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": None, "quality": None, "level": None,
                                          "inventory_type": "HEAD"})
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   max_appearance_sources=1)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_parse_items_combines_flag_and_file(tmp_path):
    f = tmp_path / "watchlist.txt"
    f.write_text("1\n2 3\n")
    assert snipe_check.parse_items("4,5", str(f)) == [4, 5, 1, 2, 3]
    assert snipe_check.parse_items(None, None) is None
