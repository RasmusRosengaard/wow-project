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

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import analyze
import appearance
import diff_snapshots
import item_names
import snipe_check
import tsm
from fetch_snapshot import SCHEMA
from scan_region import LISTING_SCHEMA

SELL_CR = 9999
BUY_CR_A = 1111
BUY_CR_B = 2222
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


@pytest.fixture(autouse=True)
def isolate_tsm_cache(tmp_path, monkeypatch):
    """find_snipes() always instantiates a real tsm.SaleRateCache (to
    annotate region_sale_rate/region_sold_per_day, see
    _filter_by_sale_rate()) -- same isolation reasoning as
    isolate_appearance_cache above. No live network involved (the cache is
    read-only here; only collect_all.py's background loop ever calls
    refresh_if_stale()), but no test should depend on whatever the real,
    gitignored local cache happens to contain."""
    monkeypatch.setattr(tsm, "CACHE_PATH", tmp_path / "tsm_sale_rates_test_cache.json")


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


def write_tsm_cache(path, item_rates: dict[int, tuple[float, float]]):
    """item_rates: item_id -> (sale_rate, sold_per_day), matching
    tsm.SaleRateCache's own on-disk shape."""
    data = {
        "fetched_at": 1_700_000_000,
        "items": {
            str(item_id): {"sale_rate": sale_rate, "sold_per_day": sold_per_day}
            for item_id, (sale_rate, sold_per_day) in item_rates.items()
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


def test_find_snipes_works_without_diff_ever_running(data_dir):
    """The new normal since 2026-07-25: diff_snapshots.py is no longer run
    automatically (see collect_all.py's module docstring), so
    data/events/{sell}.parquet never gets created in production.
    analyze.connect() must not error just because that file is missing,
    and pricing (which never read history in the first place) must still
    work correctly with zero events on disk."""
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101
    assert rows[0]["sell_p_g"] == pytest.approx(2.0)


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


def test_find_snipes_ties_break_on_cheapest_buy_price(tmp_path, monkeypatch):
    """discount_pct is rounded to 1 decimal at the SQL level, so a camped/
    troll sell-realm listing can make many genuinely-different buy prices
    all round to the exact same displayed discount% (confirmed live, human
    report 2026-07-28: a real ~9,999,999g troll-priced item tied dozens of
    rows at 100.0%, and with no secondary sort key their relative order was
    an arbitrary DB scan order, not sorted by anything visible). Once tied,
    rows must resolve to cheapest buy price first."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        # A troll-priced "current cheapest listing": 999,999,900 copper
        # (99,999.99g) -- so far above any real buy price that several very
        # different buy prices below all round to the identical 100.0%
        # discount_pct.
        snap_row(10, T1, item_id=101, buyout=999_999_900),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        # All three round to discount_pct == 100.0 against the troll price
        # above, despite being genuinely different buy prices (30g/10g/5g).
        listing_row(BUY_CR_A, item_id=101, buyout=300_000, auction_id=100),
        listing_row(BUY_CR_A, item_id=101, buyout=50_000, auction_id=101),
        listing_row(BUY_CR_A, item_id=101, buyout=100_000, auction_id=102),
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert [r["discount_pct"] for r in rows] == [100.0, 100.0, 100.0]
    assert [r["buy_g"] for r in rows] == [5.0, 10.0, 30.0]  # cheapest first, tie broken


def test_find_snipes_fully_tied_rows_are_deterministic_across_calls(tmp_path, monkeypatch):
    """Human report 2026-07-31: "items switch even though no filters were
    touched" -- live repro against real unchanged data found re-running
    find_snipes() with identical arguments returned a *different* set of
    rows each time (66 of 200 differed). Root cause: buy_g (the tiebreak
    added for test_find_snipes_ties_break_on_cheapest_buy_price above) only
    covers a discount_pct tie -- plenty of real rows tie on *both*
    discount_pct and buy_g too (round-number decoy prices are common), and
    DuckDB doesn't guarantee stable row order for ties in a parallel query
    plan, so which one "won" a LIMIT/ROW_NUMBER boundary varied between
    otherwise-identical executions. auction_id (stable, unique per listing)
    is now a final tiebreaker everywhere discount_pct/buy_g can still tie.
    This constructs two listings identical on both discount_pct and buy_g,
    differing only by auction_id, and asserts repeated calls always agree."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(3, T0, item_id=103)]
    curr = [
        snap_row(3, T1, item_id=103),
        snap_row(10, T1, item_id=101, buyout=100_000),  # current price 10g
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [
        # Same item, same buyout -- tied on both discount_pct AND buy_g,
        # differing only by auction_id.
        listing_row(BUY_CR_A, item_id=101, buyout=50_000, auction_id=205),
        listing_row(BUY_CR_A, item_id=101, buyout=50_000, auction_id=104),
        listing_row(BUY_CR_A, item_id=101, buyout=50_000, auction_id=317),
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    results = [tuple(r["auction_id"] for r in snipe_check.find_snipes(con, SELL_CR, min_discount=0.3))
               for _ in range(10)]
    assert len(set(results)) == 1, "identical query returned a different row order across repeated calls"
    assert results[0] == (104, 205, 317)  # auction_id ASC once discount_pct/buy_g are exhausted

    # The same tie can decide *which* row survives a ROW_NUMBER() cutoff,
    # not just display order -- max_per_item=1 must keep the same single
    # winner every time, not an arbitrary one of the three.
    capped_results = [
        tuple(r["auction_id"] for r in snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, max_per_item=1))
        for _ in range(10)
    ]
    assert len(set(capped_results)) == 1
    assert capped_results[0] == (104,)


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


def test_find_snipes_ignores_bonus_variance_regardless_of_shape(tmp_path, monkeypatch):
    """Matching changed 2026-07-26 (human product decision, see
    find_snipes()'s docstring): item_id alone is the match key now --
    bonus_key/ilvl differences no longer matter at all, replacing the old
    market_key()-based pooling. Reproduces the real production case (item
    238014, Sun-Blessed Sickle) that originally motivated market_key(): a
    crafted item's per-craft stat roll (modifier type 42) meant the sell
    realm's current listing (roll_d) and the buy-side listing (roll_c)
    never shared an exact bonus_key. Under the old design this needed
    market_key() to recognize both rolls as "the same market"; now it needs
    nothing -- any bonus_key difference is irrelevant to matching by
    construction."""
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
    assert r["sell_p_g"] == pytest.approx(2.1)   # the sell realm's current listing (roll_d), 21_000 copper


def test_find_snipes_pools_bonus_list_noise_with_no_sample_floor(tmp_path, monkeypatch):
    """Reproduces the real production case (item 36507, Iron-Molded Fist,
    reported 2026-07-25) with far fewer samples than the old design ever
    needed. Under the previous market_key()-based approach, per-craft
    "instance" bonus_lists id noise like this only got detected/stripped
    above a 20-sample floor (BONUS_NOISE_MIN_SAMPLES) -- traced live
    2026-07-26 to silently fail on ~1,223 real items sitting under that
    floor since the 2026-07-25 retention change stopped accumulating the
    historical samples the heuristic depended on (see HISTORY.md). With
    item_id-only matching there's no sample floor to clear at all -- a
    single buy-side listing (auction_id=100 below) matches regardless."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    STABLE_ID = 9000

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    # Only 2 samples -- well under the old 20-sample floor.
    curr = [snap_row(9999, T1, item_id=103)] + [
        snap_row(i, T1, item_id=600, bonus_key=f"b:{7000 + i},{STABLE_ID}", buyout=20_000)
        for i in range(2)
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
    assert r["sell_p_g"] == pytest.approx(2.0)         # pooled current listing price (20_000 copper)


def test_find_snipes_merges_price_tiers_using_overall_cheapest(tmp_path, monkeypatch):
    """Before 2026-07-26, a companion bonus pair or a mutually-exclusive
    tier system (real production shapes: item 109168's paired gem/socket
    bonus, item 244752's item-level-upgrade tiers) were deliberately kept
    as separate markets by market_key(), since each tier is a genuinely
    different, differently-priced version of the item. The human's
    2026-07-26 decision explicitly reverses that for pricing: merge every
    bonus/ilvl variant of an item into one listing, sell price = the
    overall cheapest across all of them, with the individual variant still
    shown per row for display (dashboard.py's _variant_label()/variant_raw,
    unaffected by this change). Three price tiers here (20g/40g/80g); the
    buy-side listing carries the priciest tier's bonus_key, but the sell
    price must be the 20g tier's, not its own tier's 80g."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    STABLE_ID = 8000

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=800, bonus_key=f"b:{STABLE_ID},8100", buyout=20_000),
        snap_row(2, T1, item_id=800, bonus_key=f"b:{STABLE_ID},8200", buyout=40_000),
        snap_row(3, T1, item_id=800, bonus_key=f"b:{STABLE_ID},8300", buyout=80_000),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # Buy-side listing carries the most expensive tier's bonus_key -- must
    # still match against the sell realm's overall cheapest (8100, 20g),
    # not get scoped to only its own tier.
    buy_rows = [listing_row(BUY_CR_A, item_id=800, buyout=10_000, auction_id=100,
                            bonus_key=f"b:{STABLE_ID},8300")]
    pq.write_table(pa.Table.from_pylist(buy_rows, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    r = rows[0]
    assert r["item_id"] == 800
    assert r["bonus_key"] == f"b:{STABLE_ID},8300"  # buy-side variant still shown for display
    assert r["sell_p_g"] == pytest.approx(2.0)  # overall cheapest tier (20_000 copper), not the 8300 tier's 8g


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


def test_find_snipes_region_median_uses_median_not_mean(tmp_path, monkeypatch):
    """region_median_g (human request, 2026-07-27) must be the statistical
    median of the per-other-realm cheapest listings, not a mean -- three
    other realms at 1g/2g/100g have a median of 2g but a mean of ~34.33g,
    so this proves it's genuinely the median, not accidentally the mean.
    Also confirms region_median_g is computed over *every* other-realm
    listing (including realm C's, which itself never qualifies as a snipe
    candidate here), not just the qualifying rows."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    ITEM = 900
    BUY_CR_C = 3333

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103),
            snap_row(1, T1, item_id=ITEM, buyout=100_000)]  # sell reference: 10g
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_A, item_id=ITEM, buyout=10_000, auction_id=100)],  # 1g
        schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_A}.parquet")
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_B, item_id=ITEM, buyout=20_000, auction_id=200)],  # 2g
        schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_B}.parquet")
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_C, item_id=ITEM, buyout=1_000_000, auction_id=300)],  # 100g, doesn't qualify
        schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_C}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 2  # only realms A and B qualify as snipe candidates
    assert all(r["region_median_g"] == pytest.approx(2.0) for r in rows)
    assert all(r["region_median_copper"] == 20_000 for r in rows)


def test_find_snipes_price_suspect_flags_sell_price_over_10x_median(tmp_path, monkeypatch):
    """price_suspect (human request, 2026-08-03): flags a row when the sell
    realm's own reference price is >= PRICE_SUSPECT_MULTIPLE (10x) the
    region median -- a likely troll/joke listing on the sell side, not the
    buy side. Two items with an identical region median (1.5g, from the
    same two other-realm listings) but different sell prices: one just over
    the 10x line (suspect), one just under it (not suspect) -- proves the
    flag is a real threshold check, not always-true/always-false."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    ITEM_SUSPECT = 910      # sell price >= 10x the region median
    ITEM_NOT_SUSPECT = 911  # sell price just under 10x the region median

    median_copper = 15_000  # median of 10_000 (1g) and 20_000 (2g) = 1.5g

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=ITEM_SUSPECT, buyout=10 * median_copper),      # exactly 10x -> suspect
        snap_row(2, T1, item_id=ITEM_NOT_SUSPECT, buyout=10 * median_copper - 1),  # just under -> not suspect
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    for item_id, suffix in ((ITEM_SUSPECT, "suspect"), (ITEM_NOT_SUSPECT, "not_suspect")):
        pq.write_table(pa.Table.from_pylist([
            listing_row(BUY_CR_A, item_id=item_id, buyout=10_000, auction_id=1000 + item_id),  # 1g
            listing_row(BUY_CR_B, item_id=item_id, buyout=20_000, auction_id=2000 + item_id),  # 2g
        ], schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_A}_{suffix}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0)
    by_item = {r["item_id"]: r for r in rows}

    assert by_item[ITEM_SUSPECT]["region_median_g"] == pytest.approx(1.5)
    assert by_item[ITEM_SUSPECT]["price_suspect"] is True
    assert by_item[ITEM_NOT_SUSPECT]["region_median_g"] == pytest.approx(1.5)
    assert by_item[ITEM_NOT_SUSPECT]["price_suspect"] is False


def test_find_snipes_sniper_filter_suspect_flags_a_crowded_price(tmp_path, monkeypatch):
    """sniper_filter_suspect ("Sniper filter", human request, 2026-08-04):
    flags a buy-side candidate whose price is corroborated by
    SNIPER_FILTER_MIN_REALMS+ other unique realms clustering within
    SNIPER_FILTER_CLOSE_MULTIPLE of it. Real numbers from the human's own
    example screenshot: buy price 400g, next SNIPER_FILTER_N (5) other
    realms at 400/428/444/500/500g -- median 444g, well inside 1.7x of
    400g. A second item with the same 400g buy price but every other realm
    at 5000g (12.5x away, a genuinely isolated cheap listing) proves the
    flag is a real threshold check, not always-true."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    assert snipe_check.SNIPER_FILTER_N == 5  # test provides exactly 5 other realms

    ITEM_CROWDED = 930      # cluster sits close to the buy price
    ITEM_NOT_CROWDED = 931  # cluster sits far above the buy price

    sell_price_copper = 100_000_000  # 10,000g -- comfortably above both, for a real discount

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=ITEM_CROWDED, buyout=sell_price_copper),
        snap_row(2, T1, item_id=ITEM_NOT_CROWDED, buyout=sell_price_copper),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    BUY_REALM = 9101
    OTHER_REALMS = [9102, 9103, 9104, 9105, 9106]  # exactly SNIPER_FILTER_N=5

    # ITEM_CROWDED: buy realm at 400g, next 5 unique realms at
    # 400/428/444/500/500g -- median 444g, within 1.7x of 400g (680g).
    crowded_prices_g = [400, 428, 444, 500, 500]
    rows = [listing_row(BUY_REALM, item_id=ITEM_CROWDED, buyout=4_000_000, auction_id=9000)]
    for i, (cr, price_g) in enumerate(zip(OTHER_REALMS, crowded_prices_g)):
        rows.append(listing_row(cr, item_id=ITEM_CROWDED, buyout=price_g * 10_000, auction_id=9010 + i))
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA), listings_dir / "crowded.parquet")

    # ITEM_NOT_CROWDED: same 400g buy price, but every other realm sits at
    # 5000g (12.5x away) -- a genuinely isolated cheap listing.
    rows2 = [listing_row(BUY_REALM, item_id=ITEM_NOT_CROWDED, buyout=4_000_000, auction_id=9100)]
    for i, cr in enumerate(OTHER_REALMS):
        rows2.append(listing_row(cr, item_id=ITEM_NOT_CROWDED, buyout=50_000_000, auction_id=9110 + i))
    pq.write_table(pa.Table.from_pylist(rows2, schema=LISTING_SCHEMA), listings_dir / "not_crowded.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows_out = snipe_check.find_snipes(con, SELL_CR, min_discount=0)
    by_item_realm = {(r["item_id"], r["buy_realm"]): r for r in rows_out}

    assert by_item_realm[(ITEM_CROWDED, BUY_REALM)]["sniper_filter_suspect"] is True
    assert by_item_realm[(ITEM_NOT_CROWDED, BUY_REALM)]["sniper_filter_suspect"] is False


def test_find_snipes_sniper_filter_suspect_requires_minimum_realm_count(tmp_path, monkeypatch):
    """Fewer than SNIPER_FILTER_MIN_REALMS other realms even having a
    listing means there isn't enough data to judge clustering -- the flag
    stays False (not enough evidence either way), never True, even when the
    few realms that do exist happen to sit close to the buy price."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    assert snipe_check.SNIPER_FILTER_MIN_REALMS == 3  # test provides exactly 2 other realms: too few

    ITEM = 940
    sell_price_copper = 100_000_000  # 10,000g

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103), snap_row(1, T1, item_id=ITEM, buyout=sell_price_copper)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    BUY_REALM = 9201
    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    rows = [
        listing_row(BUY_REALM, item_id=ITEM, buyout=4_000_000, auction_id=9200),  # 400g
        listing_row(9202, item_id=ITEM, buyout=4_100_000, auction_id=9201),  # 410g -- close
        listing_row(9203, item_id=ITEM, buyout=4_200_000, auction_id=9202),  # 420g -- close, but only 2 others
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA), listings_dir / "sparse.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows_out = snipe_check.find_snipes(con, SELL_CR, min_discount=0)
    by_realm = {r["buy_realm"]: r for r in rows_out if r["item_id"] == ITEM}
    assert by_realm[BUY_REALM]["sniper_filter_suspect"] is False


def test_find_snipes_sniper_filter_suspect_exempts_high_value_items(tmp_path, monkeypatch):
    """SNIPER_FILTER_HIGH_VALUE_EXEMPT_G (human request, 2026-08-04): never
    flags once both the sell price and region median clear the ceiling,
    even when the cluster is tight enough it would otherwise trip the flag
    -- a proportionally close cluster on an expensive item can still be a
    huge absolute gold gap."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    exempt_g = snipe_check.SNIPER_FILTER_HIGH_VALUE_EXEMPT_G
    high_price_g = exempt_g * 3  # comfortably clears the 200k ceiling

    ITEM = 950
    sell_price_copper = int(high_price_g * 4 * 10_000)  # well above buy price and region median both

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103), snap_row(1, T1, item_id=ITEM, buyout=sell_price_copper)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    BUY_REALM = 9301
    OTHER_REALMS = [9302, 9303, 9304, 9305, 9306]
    # Tight cluster right at high_price_g -- would trip the flag if not for
    # the high-value exemption (region median == high_price_g > 200k).
    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    rows = [listing_row(BUY_REALM, item_id=ITEM, buyout=int(high_price_g * 10_000), auction_id=9300)]
    for i, cr in enumerate(OTHER_REALMS):
        rows.append(listing_row(cr, item_id=ITEM, buyout=int(high_price_g * 10_000), auction_id=9310 + i))
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA), listings_dir / "high_value.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows_out = snipe_check.find_snipes(con, SELL_CR, min_discount=0)
    row = next(r for r in rows_out if r["item_id"] == ITEM and r["buy_realm"] == BUY_REALM)
    assert row["sell_p_g"] >= exempt_g
    assert row["region_median_g"] >= exempt_g
    assert row["sniper_filter_suspect"] is False


def test_find_snipes_min_value_floor_keeps_row_if_either_price_clears_it(tmp_path, monkeypatch):
    """min_value_floor_g (human request, 2026-08-01): OR-to-keep, AND-to-drop
    -- a row survives if *either* the sell price or the region median clears
    the floor, only dropped when both fall short. Three items exercise all
    three cases: sell price alone clears it, region median alone clears it,
    neither does."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    # Derived from the real constant, not a hardcoded gold amount -- so a
    # future threshold change (already happened once: 500 -> 2000, both
    # human-specified) doesn't silently break this test's assumptions again.
    floor_copper = int(snipe_check.MIN_VALUE_FLOOR_G) * 10_000
    above_floor_copper = floor_copper * 3  # comfortably clears it
    below_floor_copper = 20_000            # comfortably under it (2g)

    ITEM_SELL_HIGH = 301   # sell price above the floor, region median well below
    ITEM_MEDIAN_HIGH = 302  # sell price well below the floor, region median at it
    ITEM_BOTH_LOW = 303    # sell price and region median both well below

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=ITEM_SELL_HIGH, buyout=above_floor_copper),
        snap_row(2, T1, item_id=ITEM_MEDIAN_HIGH, buyout=below_floor_copper),
        snap_row(3, T1, item_id=ITEM_BOTH_LOW, buyout=below_floor_copper),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # ITEM_SELL_HIGH: one cheap buy realm -> region median is low, but the
    # sell-side price (well above the floor) alone should keep it.
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_A, item_id=ITEM_SELL_HIGH, buyout=100_000, auction_id=301)],  # 10g
        schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_A}.parquet")
    # ITEM_MEDIAN_HIGH: candidate realm B is cheap (for the discount to
    # qualify), two decoy realms are exactly at the floor -- with 3 realms'
    # floors sorted [cheap, floor, floor], the median (the true middle value,
    # not an average -- that only applies to an even count) is exactly the
    # floor, clearing it even though the sell price doesn't.
    pq.write_table(pa.Table.from_pylist([
        listing_row(BUY_CR_B, item_id=ITEM_MEDIAN_HIGH, buyout=1_000, auction_id=302),      # 0.1g
        listing_row(3333, item_id=ITEM_MEDIAN_HIGH, buyout=floor_copper, auction_id=303),
        listing_row(4444, item_id=ITEM_MEDIAN_HIGH, buyout=floor_copper, auction_id=304),
    ], schema=LISTING_SCHEMA), listings_dir / "9001.parquet")
    # ITEM_BOTH_LOW: cheap sell price, cheap region median -- neither clears.
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_A, item_id=ITEM_BOTH_LOW, buyout=1_000, auction_id=305)],  # 0.1g
        schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_A}_low.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    # No floor (default None) -- every case present, matching prior behavior.
    rows_no_floor = snipe_check.find_snipes(con, SELL_CR, min_discount=0)
    item_ids_no_floor = {r["item_id"] for r in rows_no_floor}
    assert {ITEM_SELL_HIGH, ITEM_MEDIAN_HIGH, ITEM_BOTH_LOW} <= item_ids_no_floor

    # Floor on -- only the both-low item is cut.
    rows_floored = snipe_check.find_snipes(con, SELL_CR, min_discount=0,
                                           min_value_floor_g=snipe_check.MIN_VALUE_FLOOR_G)
    item_ids_floored = {r["item_id"] for r in rows_floored}
    assert ITEM_SELL_HIGH in item_ids_floored
    assert ITEM_MEDIAN_HIGH in item_ids_floored
    assert ITEM_BOTH_LOW not in item_ids_floored


def _stub_item_classes(monkeypatch, mapping: dict[int, tuple[int | None, int | None]]):
    """mapping: item_id -> (item_class, item_subclass). Used by class_quotas
    tests to control which bucket (snipe_check._class_bucket()) each test
    item resolves into, without hitting the live Blizzard API."""
    monkeypatch.setattr(
        item_names, "_fetch_item_details",
        lambda item_id: {
            "name": None, "quality": None, "level": None, "inventory_type": None,
            "item_class": mapping.get(item_id, (None, None))[0],
            "item_subclass": mapping.get(item_id, (None, None))[1],
        })


def test_register_class_quota_maps_never_blocks_past_the_resolve_limit(monkeypatch):
    """Real bug fix (2026-07-31, found during a repo-wide bug audit):
    item_class()/item_subclass() transparently fall back to a *blocking*,
    one-at-a-time network fetch for anything not already cached (see
    NameCache._ensure_item_details()). The old _register_class_quota_maps()
    called them unconditionally for every distinct candidate item after
    ensure_many()'s own bounded/concurrent resolution -- so any item past
    CLASS_QUOTA_RESOLVE_LIMIT (or whose concurrent fetch merely failed
    transiently) silently triggered exactly the sequential-blocking-calls
    failure mode this limit exists to prevent (see CLAUDE.md's "Real
    production outage"). Sets the limit to 1 with 2 distinct items and
    asserts the fetch function is only ever called once (ensure_many's own
    bounded call) -- proving the second, unresolved item never falls
    through to a second, blocking fetch."""
    monkeypatch.setattr(snipe_check, "CLASS_QUOTA_RESOLVE_LIMIT", 1)
    calls = []

    def fake_fetch(item_id):
        calls.append(item_id)
        return {"name": f"item {item_id}", "quality": "COMMON", "level": 1,
               "inventory_type": None, "item_class": 20, "item_subclass": None}
    monkeypatch.setattr(item_names, "_fetch_item_details", fake_fetch)

    con = duckdb.connect()
    snipe_check._register_class_quota_maps(con, [700, 701], {"housing": 10})
    assert len(calls) == 1  # only ensure_many's own bounded resolution, no blocking fallback
    resolved = con.execute("SELECT item_id FROM class_quota_item_map").fetchall()
    assert resolved == [(calls[0],)]  # the unresolved item got no bucket at all


def test_find_snipes_class_quotas_prevent_one_category_crowding_out_another(tmp_path, monkeypatch):
    """Reproduces the real production complaint (2026-07-27): a saturated
    category (here Weapons, standing in for the decoy-listing-heavy
    categories seen live) fills every slot of a small top-N budget purely by
    raw discount%, so a real, lower-but-genuine snipe in another category
    (Housing) never appears at all. With class_quotas={"weapon": 2,
    "housing": 1} and top=3, the single Housing row must survive even
    though its ~47% discount ranks below all 5 of the ~99%-discount Weapon
    rows -- proving the quota, not raw rank, decides what's kept."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    WEAPON_ITEM, HOUSING_ITEM = 700, 701
    _stub_item_classes(monkeypatch, {WEAPON_ITEM: (2, None), HOUSING_ITEM: (20, None)})

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=WEAPON_ITEM, buyout=100_000),   # 10g sell reference
        snap_row(2, T1, item_id=HOUSING_ITEM, buyout=100_000),  # 10g sell reference
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # 5 near-100%-discount Weapon listings across 5 different realms --
    # would fill an unquota'd top=3 entirely by themselves.
    buy_rows = [listing_row(1000 + i, WEAPON_ITEM, buyout=1_000, auction_id=100 + i)
                for i in range(5)]
    # One real, qualifying (~47% discount) Housing listing.
    buy_rows.append(listing_row(2000, HOUSING_ITEM, buyout=50_000, auction_id=200))
    for r in buy_rows:
        pq.write_table(pa.Table.from_pylist([r], schema=LISTING_SCHEMA),
                       listings_dir / f"{r['cr_id']}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    # Without quotas, Housing is crowded out entirely by the 5 Weapon rows.
    unquota_rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, top=3)
    assert all(r["item_id"] == WEAPON_ITEM for r in unquota_rows)
    assert len(unquota_rows) == 3

    # With quotas, Housing survives despite its much lower discount%.
    quota_rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, top=3,
                                         class_quotas={"weapon": 2, "housing": 1})
    assert len(quota_rows) == 3
    by_item = [r["item_id"] for r in quota_rows]
    assert by_item.count(WEAPON_ITEM) == 2
    assert by_item.count(HOUSING_ITEM) == 1


def test_find_snipes_class_quotas_caps_one_item_hogging_its_own_bucket(tmp_path, monkeypatch):
    """Reproduces the real production complaint (2026-07-31, human report:
    "why do I only have 1 housing item" + live repro on the human's own
    account): class_quotas prevents one *category* from crowding out
    another (see the test above), but had no protection against one *item*
    crowding out other items within the *same* bucket -- confirmed live,
    item 264709 (Stranglekelp Sack, 663 qualifying region-wide listings)
    took 29 of the free tier's 40-row housing quota (72%), leaving only 5
    distinct housing items visible. One item with 10 qualifying listings
    (all higher discount than everything else) must not take more than
    CLASS_QUOTA_PER_ITEM_CAP slots of a small housing quota, leaving room
    for other distinct items even though every one of its individual rows
    outranks them by discount%."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    DOMINANT_ITEM = 800
    OTHER_ITEMS = [801, 802, 803, 804]  # distinct discounts, best (A) to worst (D)
    _stub_item_classes(monkeypatch, {iid: (20, None) for iid in [DOMINANT_ITEM, *OTHER_ITEMS]})

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103)]
    curr.append(snap_row(1, T1, item_id=DOMINANT_ITEM, buyout=1_000_000))  # 100g sell reference
    for i, iid in enumerate(OTHER_ITEMS):
        curr.append(snap_row(2 + i, T1, item_id=iid, buyout=100_000))  # 10g sell reference each
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # 10 listings of the same item, all ~95% discount -- would rank above
    # every other item below on raw discount% alone.
    buy_rows = [listing_row(3000 + i, DOMINANT_ITEM, buyout=50_000, auction_id=100 + i)
                for i in range(10)]
    # 4 distinct other items, each with one real, lower-but-qualifying and
    # mutually distinct discount (60% / 55% / 50% / 45%).
    for i, (iid, buyout) in enumerate(zip(OTHER_ITEMS, (40_000, 45_000, 50_000, 55_000))):
        buy_rows.append(listing_row(3010 + i, iid, buyout=buyout, auction_id=200 + i))
    for r in buy_rows:
        pq.write_table(pa.Table.from_pylist([r], schema=LISTING_SCHEMA),
                       listings_dir / f"{r['cr_id']}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)

    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, top=5,
                                   class_quotas={"housing": 5})
    assert len(rows) == 5
    item_ids = [r["item_id"] for r in rows]
    assert item_ids.count(DOMINANT_ITEM) == snipe_check.CLASS_QUOTA_PER_ITEM_CAP
    # The remaining slots go to the *best*-discount other items (A, B), not
    # an arbitrary two -- proving genuine diversity, not just a smaller
    # dominant-item count.
    assert set(item_ids) == {DOMINANT_ITEM, 801, 802}


def test_find_snipes_class_quotas_finds_sparse_bucket_candidate_at_any_depth(tmp_path, monkeypatch):
    """Reproduces the exact bug in the *first* class_quotas implementation
    (same day, 2026-07-27, fixed before ever shipping to a real user beyond
    this investigation): that version widened the SQL LIMIT to a fixed
    ceiling and bucketed in Python afterward -- confirmed live on Draenor
    that this doesn't actually guarantee anything, since 450,568 rows
    qualified region-wide and the first genuine Housing candidate sat at
    rank 39,524, past any reasonable fixed widening. This test reproduces
    that shape at a smaller, tractable scale: 30 near-100%-discount Weapon
    rows rank strictly above the single ~47%-discount Housing row -- deep
    enough that any fixed "search this far and give up" cutoff smaller than
    31 would miss it, but the real SQL-side ranking (no row-count
    truncation before class_rank is computed) must not.

    30 *distinct* Weapon items (not 30 rows of the same one) -- deliberately
    so this test's "depth" guarantee stays independent of
    CLASS_QUOTA_PER_ITEM_CAP (added 2026-07-31 for a different, real bug:
    one item hogging its own bucket, see
    test_find_snipes_class_quotas_caps_one_item_hogging_its_own_bucket
    above), which would otherwise collapse a single repeated item down to a
    few rows regardless of how deep this test needs Housing to rank."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    WEAPON_ITEMS = list(range(750, 780))  # 30 distinct item ids
    HOUSING_ITEM = 900
    _stub_item_classes(monkeypatch, {**{iid: (2, None) for iid in WEAPON_ITEMS}, HOUSING_ITEM: (20, None)})

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103), snap_row(2, T1, item_id=HOUSING_ITEM, buyout=100_000)]
    for i, iid in enumerate(WEAPON_ITEMS):
        curr.append(snap_row(3000 + i, T1, item_id=iid, buyout=100_000))
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # 30 distinct Weapon items, one qualifying listing each, all ranking
    # above the Housing row below.
    buy_rows = [listing_row(1000 + i, iid, buyout=1_000, auction_id=100 + i)
                for i, iid in enumerate(WEAPON_ITEMS)]
    buy_rows.append(listing_row(2000, HOUSING_ITEM, buyout=50_000, auction_id=200))
    for r in buy_rows:
        pq.write_table(pa.Table.from_pylist([r], schema=LISTING_SCHEMA),
                       listings_dir / f"{r['cr_id']}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, top=31,
                                   class_quotas={"weapon": 30, "housing": 1})
    assert len(rows) == 31
    by_item = [r["item_id"] for r in rows]
    assert set(by_item) == {*WEAPON_ITEMS, HOUSING_ITEM}
    assert by_item.count(HOUSING_ITEM) == 1


def test_find_snipes_class_quotas_excludes_unlisted_categories(tmp_path, monkeypatch):
    """A bucket with no entry in class_quotas at all (not even 0) is
    excluded entirely -- matches the free tier's real design (deliberately
    shows no Containers/Profession/Quest items, see dashboard.py's
    FREE_CLASS_QUOTAS), not just an oversight for buckets nobody thought of."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    QUEST_ITEM = 800
    _stub_item_classes(monkeypatch, {QUEST_ITEM: (12, None)})  # Quest, not in the quota dict below

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103),
            snap_row(1, T1, item_id=QUEST_ITEM, buyout=100_000)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(
        [listing_row(BUY_CR_A, QUEST_ITEM, buyout=1_000, auction_id=100)], schema=LISTING_SCHEMA),
        listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, top=10,
                                   class_quotas={"weapon": 10})  # no "quest" key at all
    assert rows == []


def test_find_snipes_class_quotas_mount_is_a_subclass_of_misc(tmp_path, monkeypatch):
    """Mounts (item_class 15, item_subclass 5) are a subclass of the generic
    Miscellaneous class, not their own item_class -- a class-15 item with a
    DIFFERENT subclass (ordinary Miscellaneous junk) must not accidentally
    match the "mount" bucket, and with no bucket of its own must be excluded
    the same as any other unlisted category."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    MOUNT_ITEM, MISC_ITEM = 900, 901
    _stub_item_classes(monkeypatch, {MOUNT_ITEM: (15, 5), MISC_ITEM: (15, 99)})

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=MOUNT_ITEM, buyout=100_000),
        snap_row(2, T1, item_id=MISC_ITEM, buyout=100_000),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([
        listing_row(BUY_CR_A, MOUNT_ITEM, buyout=1_000, auction_id=100),
        listing_row(BUY_CR_A, MISC_ITEM, buyout=1_000, auction_id=101),
    ], schema=LISTING_SCHEMA), listings_dir / f"{BUY_CR_A}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, top=10,
                                   class_quotas={"mount": 10})
    assert len(rows) == 1
    assert rows[0]["item_id"] == MOUNT_ITEM


def test_find_snipes_class_quotas_none_is_identical_to_omitting_it(data_dir, monkeypatch):
    """class_quotas=None (the default) must produce byte-identical results
    to not passing it at all -- every existing test in this file relies on
    that, but this makes the equivalence explicit."""
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    default_rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    explicit_none_rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3, class_quotas=None)
    assert default_rows == explicit_none_rows


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


def test_find_snipes_reports_sale_rate_without_filtering(data_dir, monkeypatch):
    """With no min_sale_rate set, region_sale_rate/region_sold_per_day are
    attached to every row for display but nothing gets dropped --
    including items TSM has no data for (both None, not excluded)."""
    run_diff(monkeypatch)
    write_tsm_cache(tsm.CACHE_PATH, {101: (0.42, 1.5)})
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    assert rows[0]["region_sale_rate"] == 0.42
    assert rows[0]["region_sold_per_day"] == 1.5


def test_find_snipes_reports_region_sale_avg_without_filtering(data_dir, monkeypatch):
    """region_sale_avg_copper (human request, 2026-08-03 -- "region sale avg
    from tsm, if it exist") rides along the same way region_sale_rate does:
    attached to every row for display, gates nothing, None for an item TSM
    has no data for. Written directly (not via write_tsm_cache(), whose
    2-tuple shape predates this field) to control avg_sale_price precisely."""
    run_diff(monkeypatch)
    tsm.CACHE_PATH.write_text(json.dumps({
        "fetched_at": 1_700_000_000,
        "items": {"101": {"sale_rate": 0.42, "sold_per_day": 1.5, "avg_sale_price": 18_500.0}},
    }))
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    assert rows[0]["region_sale_avg_copper"] == 18_500.0


def test_find_snipes_region_sale_avg_none_when_tsm_has_no_data(data_dir, monkeypatch):
    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    assert rows[0]["region_sale_avg_copper"] is None


def test_find_snipes_region_sale_avg_none_for_cache_entry_missing_the_field(data_dir, monkeypatch):
    """A cache file written before avg_sale_price existed (or any other
    entry missing the key) must not crash find_snipes() -- self-heals to
    None, same convention as item_names.NameCache's backfill logic."""
    run_diff(monkeypatch)
    tsm.CACHE_PATH.write_text(json.dumps({
        "fetched_at": 1_700_000_000,
        "items": {"101": {"sale_rate": 0.42, "sold_per_day": 1.5}},  # no avg_sale_price
    }))
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 1
    assert rows[0]["region_sale_avg_copper"] is None


def test_find_snipes_min_sale_rate_filters_illiquid_items(data_dir, monkeypatch):
    """Item 101 sells 10% of days -- --min-sale-rate 0.5 should drop it;
    lowering the bar to 0.1 lets it back through."""
    run_diff(monkeypatch)
    write_tsm_cache(tsm.CACHE_PATH, {101: (0.1, 0.2)})
    con = analyze.connect(SELL_CR)

    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   min_sale_rate=0.5) == []

    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   min_sale_rate=0.1)
    assert len(rows) == 1
    assert rows[0]["item_id"] == 101


def test_find_snipes_min_sale_rate_excludes_items_with_no_tsm_data(data_dir, monkeypatch):
    """An item TSM has never tracked (or a cache that hasn't been
    refreshed) has region_sale_rate=None -- when a liquidity floor is
    actively requested, an unknown rate can't be proven to clear it, so
    it's excluded rather than let through by default (same "unknown ->
    excluded when filtering" convention as max_appearance_sources)."""
    run_diff(monkeypatch)
    write_tsm_cache(tsm.CACHE_PATH, {999: (0.9, 5.0)})  # no entry for item 101
    con = analyze.connect(SELL_CR)
    assert snipe_check.find_snipes(con, SELL_CR, min_discount=0.3,
                                   min_sale_rate=0.01) == []


def test_filter_by_appearance_directly_no_duckdb(monkeypatch):
    """_filter_by_appearance() is a pure list->list function -- exercise it
    directly, without going through find_snipes()'s DuckDB query, covering
    the same three rules its find_snipes()-level tests above cover: always
    annotate, drop unknown/uncached items when filtering, drop profession
    tools even at a qualifying count."""
    write_appearance_cache(appearance.CACHE_PATH, {101: 1, 102: 3})
    inventory_types = {101: "HEAD", 102: "PROFESSION_TOOL"}
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": None, "quality": None, "level": None,
                                          "inventory_type": inventory_types.get(item_id)})
    rows = [{"item_id": 101}, {"item_id": 102}, {"item_id": 999}]

    annotated = snipe_check._filter_by_appearance(rows, max_appearance_sources=None)
    assert annotated == rows  # nothing dropped
    assert [r["appearance_sources"] for r in annotated] == [1, 3, None]

    filtered = snipe_check._filter_by_appearance(rows, max_appearance_sources=5)
    # 101 kept (real gear, within cap); 102 dropped (profession tool,
    # even though 3 <= 5); 999 dropped (not in the cache, can't prove rare)
    assert [r["item_id"] for r in filtered] == [101]


def test_parse_items_combines_flag_and_file(tmp_path):
    f = tmp_path / "watchlist.txt"
    f.write_text("1\n2 3\n")
    assert snipe_check.parse_items("4,5", str(f)) == [4, 5, 1, 2, 3]
    assert snipe_check.parse_items(None, None) is None


def test_check_data_ready_missing_snapshots(tmp_path, monkeypatch):
    """Shared by the CLI (SystemExit) and dashboard.py's /api/snipes route
    (HTTPException(400)) -- checks for a snapshot, not an events file
    (2026-07-25 -- pricing only ever reads the latest snapshot, nothing
    generates events automatically anymore, see collect_all.py)."""
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    msg = snipe_check.check_data_ready(SELL_CR)
    assert msg is not None
    assert "fetch_snapshot.py" in msg


def test_check_data_ready_missing_listings(tmp_path, monkeypatch):
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    (snap_dir / "1700000000.parquet").write_bytes(b"")
    msg = snipe_check.check_data_ready(SELL_CR)
    assert msg is not None
    assert "scan_region.py" in msg


def test_check_data_ready_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)
    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    (snap_dir / "1700000000.parquet").write_bytes(b"")
    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    (listings_dir / "1403.parquet").write_bytes(b"")
    assert snipe_check.check_data_ready(SELL_CR) is None


# Arbitrary item_id used across the jewelry-rule tests below -- not in
# CLASS_STARTER_ARMOR_ITEM_IDS, so these tests exercise only the
# ilvl+inventory_type rule, not the curated id set (see the
# test_is_sus_item_class_starter_armor_* tests below for that path).
NON_STARTER_ITEM_ID = 1


def test_is_sus_item_flags_old_jewelry_slots():
    # Live-verified examples (2026-07-31, see LEGACY_JEWELRY_ILVL_MAX's
    # comment): Charm of Potent and Powerful Passions (ilvl 26, NECK),
    # Ornate Band (ilvl 37, FINGER).
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, "NECK", 26) is True
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, "FINGER", 37) is True
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, "TRINKET", 100) is True


def test_is_sus_item_ilvl_boundary():
    assert snipe_check.is_sus_item(
        NON_STARTER_ITEM_ID, "TRINKET", snipe_check.LEGACY_JEWELRY_ILVL_MAX) is True
    assert snipe_check.is_sus_item(
        NON_STARTER_ITEM_ID, "TRINKET", snipe_check.LEGACY_JEWELRY_ILVL_MAX + 1) is False


def test_is_sus_item_false_for_current_tier_jewelry():
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, "NECK", 610) is False


def test_is_sus_item_false_for_non_jewelry_slot():
    # A low ilvl alone isn't enough -- the slot has to be jewelry too (or
    # the item_id has to be a confirmed class-starter piece), or a perfectly
    # ordinary current-relevant leveling item would get flagged.
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, "HEAD", 26) is False


def test_is_sus_item_none_safe():
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, None, 26) is False
    assert snipe_check.is_sus_item(NON_STARTER_ITEM_ID, "NECK", None) is False


def test_is_sus_item_class_starter_armor_flags_regardless_of_slot():
    # Paladin's Girdle (187726, Plate, WAIST) -- confirmed live 2026-07-31,
    # see CLASS_STARTER_ARMOR_ITEM_IDS's comment. WAIST isn't a jewelry slot
    # at all, so this only flags via the curated id set, proving that path
    # works independently of the jewelry rule.
    assert snipe_check.is_sus_item(187726, "WAIST", 1) is True


def test_is_sus_item_class_starter_armor_covers_every_confirmed_class():
    # One representative id per confirmed class (see
    # CLASS_STARTER_ARMOR_ITEM_IDS's comment for the full live-verified
    # list) -- Hunter, Shaman, Paladin, Warrior, Warlock, Mage, Priest,
    # Rogue, Druid.
    representative_ids = [187690, 187716, 187722, 187743, 187751,
                          187757, 187763, 187769, 187774]
    for item_id in representative_ids:
        assert snipe_check.is_sus_item(item_id, "NON_EQUIP", None) is True


def test_is_sus_item_false_for_unrelated_item_at_matching_ilvl():
    # A random item id NOT in CLASS_STARTER_ARMOR_ITEM_IDS, at the exact
    # same ilvl 1 as real starter gear, in a non-jewelry slot -- must not be
    # flagged. Proves this is a curated id match, not "any ilvl-1 item."
    assert snipe_check.is_sus_item(999999, "WAIST", 1) is False


def test_is_sus_item_slithershell_armor_flags_every_confirmed_piece():
    # Slithershell set (added 2026-08-01, human request) -- confirmed live
    # via blizz.api_get(), see SLITHERSHELL_ARMOR_ITEM_IDS's comment: 8
    # Leather pieces + 1 Cloth cloak, all ilvl 58/req level 50. Armwraps
    # (169412, WRIST) is the piece the human named directly.
    for item_id in snipe_check.SLITHERSHELL_ARMOR_ITEM_IDS:
        assert snipe_check.is_sus_item(item_id, "NON_EQUIP", None) is True


def test_is_sus_item_slithershell_warglaive_not_included():
    # The set's 10th search result, Slithershell Warglaive (170119), is a
    # weapon -- deliberately excluded, the human asked for "armors."
    assert 170119 not in snipe_check.SLITHERSHELL_ARMOR_ITEM_IDS
    assert snipe_check.is_sus_item(170119, "WEAPON", None) is False


def test_is_sus_item_black_tooth_grunt_flags_every_confirmed_piece():
    # Black Tooth Grunt's set (added 2026-08-01, human request) -- the Plate
    # counterpart to Slithershell, confirmed live via blizz.api_get(), see
    # BLACK_TOOTH_GRUNT_ARMOR_ITEM_IDS's comment: 8 Plate pieces, all ilvl
    # 60/req level 50. Armplates (169288, WRIST) is the piece the human
    # named directly.
    for item_id in snipe_check.BLACK_TOOTH_GRUNT_ARMOR_ITEM_IDS:
        assert snipe_check.is_sus_item(item_id, "NON_EQUIP", None) is True


def test_is_sus_item_black_tooth_face_splitter_not_included():
    # Plundered Black Tooth Face-Splitter (169290) is a different naming
    # pattern ("Plundered ..." not "Black Tooth Grunt's ...") and a
    # different quality tier (RARE, not UNCOMMON) -- deliberately excluded.
    assert 169290 not in snipe_check.BLACK_TOOTH_GRUNT_ARMOR_ITEM_IDS
    assert snipe_check.is_sus_item(169290, "WEAPON", None) is False
