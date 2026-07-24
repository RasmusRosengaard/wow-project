"""Tests for snipe_check.py: joins sell-realm sold-price percentiles against
region-scanner listings. Reuses the synthetic-pipeline pattern from
test_pipeline.py."""
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
    # item 101 sells twice (20_000 and 22_000 copper) -> p25 sold price = 20_500
    prev = [
        snap_row(1, T0, item_id=101, buyout=20_000),
        snap_row(4, T0, item_id=101, buyout=22_000),
        snap_row(3, T0, item_id=103),   # survives -> no event
    ]
    curr = [snap_row(3, T1, item_id=103)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100),  # cheap -> snipe
        listing_row(BUY_CR_A, item_id=101, buyout=25_000, auction_id=101),  # pricier -> not a snipe
        listing_row(SELL_CR, item_id=101, buyout=5_000, auction_id=102),    # sell realm itself -> excluded
        listing_row(BUY_CR_A, item_id=999, buyout=1, auction_id=103),       # no sold data -> no match
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
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert len(rows) == 1
    r = rows[0]
    assert r["buy_realm"] == BUY_CR_A
    assert r["item_id"] == 101
    assert r["auction_id"] == 100
    assert r["buy_g"] == pytest.approx(1.0)      # 10_000 copper = 1g
    assert r["sell_p_g"] == pytest.approx(2.05)  # p25 of [20_000, 22_000] = 20_500 copper
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
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert rows[0]["market_key"] == market_key(rows[0]["bonus_key"] or "")


def test_find_snipes_does_not_conflate_pet_species(tmp_path, monkeypatch):
    """Caged pets (item 82800) have no bonus_key, so two different species/
    qualities must not be lumped into one sold-price bucket -- a cheap poor
    pet should be judged against the poor pet's own sold price, not a rare
    pet's."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    PET_ITEM = 82800

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [
        snap_row(1, T0, item_id=PET_ITEM, buyout=500_000, pet_species_id=1, pet_quality_id=4),
        snap_row(2, T0, item_id=PET_ITEM, buyout=550_000, pet_species_id=1, pet_quality_id=4),
        snap_row(3, T0, item_id=PET_ITEM, buyout=4_000, pet_species_id=2, pet_quality_id=1),
        snap_row(4, T0, item_id=PET_ITEM, buyout=6_000, pet_species_id=2, pet_quality_id=1),
        snap_row(5, T0, item_id=103),   # survives -> no event
    ]
    curr = [snap_row(5, T1, item_id=103)]
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
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert len(rows) == 1
    r = rows[0]
    assert r["pet_species_id"] == 2
    assert r["pet_quality_id"] == 1
    assert r["sell_p_g"] < 1.0  # poor pet's own sold price, not the rare pet's ~50g


def test_find_snipes_respects_items_filter(data_dir, monkeypatch):
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, items=[999], min_discount=0.3, min_per_day=0.1) == []


def test_find_snipes_respects_min_per_day(data_dir, monkeypatch):
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=1000) == []


def test_main_prints_results_and_caveat(data_dir, monkeypatch, capsys):
    run_diff(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["snipe_check.py", "--sell", str(SELL_CR),
                                      "--min-discount", "0.3", "--min-per-day", "0.1"])
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
    prev = [
        # item 101: sells at 20_000/22_000 -> p25 ~2.05g, cheap listing -> ~49% discount
        snap_row(1, T0, item_id=101, buyout=20_000),
        snap_row(4, T0, item_id=101, buyout=22_000),
        # item 105: sells at 500_000 twice -> p25 = 50g, listing -> 35% discount
        snap_row(5, T0, item_id=105, buyout=500_000),
        snap_row(6, T0, item_id=105, buyout=500_000),
        snap_row(3, T0, item_id=103),   # survives -> no event
    ]
    curr = [snap_row(3, T1, item_id=103)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100),
        listing_row(BUY_CR_A, item_id=105, buyout=308_750, auction_id=200),
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    default_order = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert [r["item_id"] for r in default_order] == [101, 105]  # higher discount% first

    gold_order = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, sort="gold")
    assert [r["item_id"] for r in gold_order] == [105, 101]  # higher sell_p_g first


def test_find_snipes_excludes_single_sale_by_default(tmp_path, monkeypatch):
    """A lone inferred_sale can't be told apart from a cancel-without-relist
    false positive (e.g. a troll-priced decoy listing that never actually
    sold) -- min_sales defaults to 2 so one unverified sample doesn't become
    the entire sold-price percentile. Reproduces a real production case:
    item 15138 (Onyxia Scale Cloak) had exactly one recorded inferred_sale,
    at a wildly implausible price, that alone set its sell price."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [
        snap_row(1, T0, item_id=101, buyout=20_000),  # only one sale ever
        snap_row(3, T0, item_id=103),   # survives -> no event
    ]
    curr = [snap_row(3, T1, item_id=103)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, item_id=101, buyout=10_000, auction_id=100)]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1) == []

    # Explicitly opting into a lower floor still lets it through.
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, min_sales=1)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_find_snipes_max_per_item_caps_and_keeps_best_discounts(tmp_path, monkeypatch):
    """One item with three qualifying listings at different discounts --
    max_per_item=2 should keep only the two highest-discount ones (cheapest
    buy price), dropping the weakest, not an arbitrary two."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [
        snap_row(1, T0, item_id=101, buyout=20_000),
        snap_row(4, T0, item_id=101, buyout=22_000),
        snap_row(3, T0, item_id=103),   # survives -> no event
    ]
    curr = [snap_row(3, T1, item_id=103)]
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

    all_rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert len(all_rows) == 3

    capped = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, max_per_item=2)
    assert len(capped) == 2
    assert {r["auction_id"] for r in capped} == {100, 101}


def test_find_snipes_pools_near_identical_crafted_rolls(tmp_path, monkeypatch):
    """Reproduces the real production case (item 238014, Sun-Blessed Sickle,
    2026-07-23): a crafted item's per-craft stat roll/serial (modifier types
    42/44) fragmented what should be one liquid market into dozens of
    1-sale buckets on exact bonus_key. Three DIFFERENT exact bonus_keys here
    share the same market_key (same b: bonus_lists, differ only in m:42) --
    two inferred sales (one each, neither alone meeting min_sales=2), a
    current live listing at yet another distinct roll, and a buy listing at
    a fourth distinct roll that never appears in the sell realm's history at
    all. All of it should still pool as one market and match."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    roll_a = "b:1,2,3|m:42=100"
    roll_b = "b:1,2,3|m:42=200"
    roll_d = "b:1,2,3|m:42=300"  # currently listed on the sell realm
    roll_c = "b:1,2,3|m:42=400"  # the buy-side listing's roll -- never sold, never listed on sell realm

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [
        snap_row(1, T0, item_id=500, bonus_key=roll_a, buyout=20_000),
        snap_row(2, T0, item_id=500, bonus_key=roll_b, buyout=22_000),
        snap_row(3, T0, item_id=103),   # survives -> no event
    ]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(4, T1, item_id=500, bonus_key=roll_d, buyout=21_000),  # live now, different roll
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

    # Neither exact roll alone has 2 sales -- pooling by market_key is what
    # lets this pass the default min_sales=2 floor at all.
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert len(rows) == 1
    r = rows[0]
    assert r["item_id"] == 500
    assert r["bonus_key"] == roll_c          # exact listing roll still shown for display
    assert r["sell_p_g"] == pytest.approx(2.05)   # p25 of pooled [20_000, 22_000] = 20_500 copper
    assert r["sell_now_g"] == pytest.approx(2.1)  # pooled current listing (roll_d) caps it


def test_find_snipes_respects_min_gold(data_dir, monkeypatch):
    """The only listing cheap enough to qualify (10_000 copper = 1g) is
    excluded once min_gold asks for at least 2g."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, min_gold=2) == []


def test_find_snipes_respects_max_gold(data_dir, monkeypatch):
    """The qualifying listing is 1g -- a max_gold below that excludes it,
    a max_gold above it keeps it."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, max_gold=0.5) == []
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, max_gold=5)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_find_snipes_caps_sell_price_at_current_lowest_listing(tmp_path, monkeypatch):
    """Reproduces a real production case (item 206477, Warsword of Caer
    Darrow, on Draenor 2026-07-23): the sold-price percentile was blown up by
    a troll-priced decoy inferred_sale, but the sell realm had a real, live
    listing sitting at a sane price the whole time. The effective sell_price
    must be capped at that current cheapest listing, not the poisoned
    percentile -- it's real-time verifiable data, not inference."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [
        snap_row(1, T0, item_id=101, buyout=200_000),  # troll listing -> inferred_sale at 20g
        snap_row(3, T0, item_id=103),                  # survives -> no event
    ]
    curr = [
        snap_row(3, T1, item_id=103),                  # survives -> no event
        snap_row(5, T1, item_id=101, buyout=20_000),    # real live listing, still up: 2g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(BUY_CR_A, item_id=101, buyout=5_000, auction_id=100)]  # 0.5g
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1, min_sales=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["sell_p_g"] == pytest.approx(2.0)      # capped at the live listing, not the 20g troll sale
    assert r["sell_now_g"] == pytest.approx(2.0)
    assert r["sell_now_copper"] == 20_000


def test_find_snipes_reports_appearance_sources_without_filtering(data_dir, monkeypatch):
    """With no max_appearance_sources set, appearance_sources is attached to
    every row for display but nothing gets dropped -- including items the
    cache has no entry for (appearance_sources is None, not excluded)."""
    run_diff(monkeypatch)
    write_appearance_cache(appearance.CACHE_PATH, {101: 3})
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1)
    assert len(rows) == 1
    assert rows[0]["appearance_sources"] == 3


def test_find_snipes_max_appearance_sources_filters_common_looks(data_dir, monkeypatch):
    """Item 101's appearance is shared by 3 items -- --max-appearance-sources 1
    ("only looks no other item grants") should drop it; raising the cap to 3
    lets it back through."""
    run_diff(monkeypatch)
    write_appearance_cache(appearance.CACHE_PATH, {101: 3})
    con = analyze.connect(SELL_CR)

    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1,
                                   max_appearance_sources=1) == []

    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1,
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
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1,
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
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1,
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
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, min_per_day=0.1,
                                   max_appearance_sources=1)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_parse_items_combines_flag_and_file(tmp_path):
    f = tmp_path / "watchlist.txt"
    f.write_text("1\n2 3\n")
    assert snipe_check.parse_items("4,5", str(f)) == [4, 5, 1, 2, 3]
    assert snipe_check.parse_items(None, None) is None
