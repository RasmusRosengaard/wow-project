"""Tests for snipe_check.py: joins sell-realm sold-price percentiles against
region-scanner listings. Reuses the synthetic-pipeline pattern from
test_pipeline.py."""
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import analyze
import diff_snapshots
import snipe_check
from fetch_snapshot import SCHEMA
from scan_region import LISTING_SCHEMA

SELL_CR = 9999
BUY_CR_A = 1111
T0, T1 = 1_700_000_000, 1_700_003_600


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


def test_parse_items_combines_flag_and_file(tmp_path):
    f = tmp_path / "watchlist.txt"
    f.write_text("1\n2 3\n")
    assert snipe_check.parse_items("4,5", str(f)) == [4, 5, 1, 2, 3]
    assert snipe_check.parse_items(None, None) is None
