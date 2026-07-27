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


def test_find_snipes_real_troll_case_stays_unflagged_at_93x(tmp_path, monkeypatch):
    """Real production case, traced live 2026-07-27: Draenor item 36519
    (Moonlit Katana) had only 4 current listings, across 4 different
    bonus_keys, all priced at exactly 1,398,467,500 copper (139,846g75s) --
    Undermine Exchange showed ~1,500g as the real going rate elsewhere.
    That's ~93x the real price, but the human's calibration for
    sell_price_suspect (SELL_PRICE_SCAM_MULTIPLE) is deliberately much
    higher (500x) -- this documents that choice honestly: the exact real
    number that started this investigation does NOT trip the flag, by
    design (the human explicitly chose a high bar over filtering/flagging
    aggressively)."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    TROLL_PRICE = 1_398_467_500  # copper = 139,846g75s, the real observed price
    ITEM = 36519

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103)] + [
        snap_row(i, T1, item_id=ITEM, bonus_key=f"b:170{i}|m:28=237", buyout=TROLL_PRICE)
        for i in range(4)
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    # Two other realms, each showing the real ~1,500g going rate.
    buy_rows_a = [listing_row(BUY_CR_A, item_id=ITEM, buyout=15_000_000, auction_id=100)]
    buy_rows_b = [listing_row(BUY_CR_B, item_id=ITEM, buyout=15_000_000, auction_id=200)]
    pq.write_table(pa.Table.from_pylist(buy_rows_a, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")
    pq.write_table(pa.Table.from_pylist(buy_rows_b, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_B}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 2  # one qualifying row per other realm
    assert all(r["item_id"] == ITEM for r in rows)
    assert all(r["sell_price_suspect"] is False for r in rows)


def test_find_snipes_flags_reference_price_far_above_region_average(tmp_path, monkeypatch):
    """Synthetic case exercising the mechanism itself: sell realm's
    reference price is 1000x the region average (well over
    SELL_PRICE_SCAM_MULTIPLE) -- flagged, but critically NOT excluded from
    results (human product decision 2026-07-27: surface a suspect price,
    don't silently filter it -- every prior heuristic in this project has
    eventually had a blind spot, see market_key()'s noise-detection
    history, so the human get to judge rather than the system)."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    ITEM = 700
    REGION_PRICE = 10_000               # 1g on two other realms -> region average = 10_000
    TROLL_PRICE = REGION_PRICE * 1000   # 1000x the region average

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [snap_row(9999, T1, item_id=103),
            snap_row(1, T1, item_id=ITEM, buyout=TROLL_PRICE)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows_a = [listing_row(BUY_CR_A, item_id=ITEM, buyout=REGION_PRICE, auction_id=100)]
    buy_rows_b = [listing_row(BUY_CR_B, item_id=ITEM, buyout=REGION_PRICE, auction_id=200)]
    pq.write_table(pa.Table.from_pylist(buy_rows_a, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")
    pq.write_table(pa.Table.from_pylist(buy_rows_b, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_B}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    assert len(rows) == 2
    assert all(r["item_id"] == ITEM for r in rows)
    assert all(r["sell_price_suspect"] is True for r in rows)


def test_find_snipes_suspect_flag_boundary_is_strictly_over_500x(tmp_path, monkeypatch):
    """Exactly SELL_PRICE_SCAM_MULTIPLE (500x) the region average must NOT
    flag -- only *over* 500x does (matches the module comment's "over
    500x", and the SQL's strict `>`)."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    REGION_PRICE = 10_000
    ITEM_AT_BOUNDARY = 800
    ITEM_JUST_OVER = 801
    EXACTLY_500X = REGION_PRICE * snipe_check.SELL_PRICE_SCAM_MULTIPLE
    JUST_OVER_500X = EXACTLY_500X + 1

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=ITEM_AT_BOUNDARY, buyout=EXACTLY_500X),
        snap_row(2, T1, item_id=ITEM_JUST_OVER, buyout=JUST_OVER_500X),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows_a = [
        listing_row(BUY_CR_A, item_id=ITEM_AT_BOUNDARY, buyout=REGION_PRICE, auction_id=100),
        listing_row(BUY_CR_A, item_id=ITEM_JUST_OVER, buyout=REGION_PRICE, auction_id=101),
    ]
    buy_rows_b = [
        listing_row(BUY_CR_B, item_id=ITEM_AT_BOUNDARY, buyout=REGION_PRICE, auction_id=200),
        listing_row(BUY_CR_B, item_id=ITEM_JUST_OVER, buyout=REGION_PRICE, auction_id=201),
    ]
    pq.write_table(pa.Table.from_pylist(buy_rows_a, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_A}.parquet")
    pq.write_table(pa.Table.from_pylist(buy_rows_b, schema=LISTING_SCHEMA),
                   listings_dir / f"{BUY_CR_B}.parquet")

    run_diff(monkeypatch)
    con = analyze.connect(SELL_CR)
    rows = snipe_check.find_snipes(con, SELL_CR, min_discount=0.3)
    by_item = {r["item_id"]: r["sell_price_suspect"] for r in rows}
    assert by_item[ITEM_AT_BOUNDARY] is False
    assert by_item[ITEM_JUST_OVER] is True


def test_find_snipes_region_median_uses_median_not_mean(tmp_path, monkeypatch):
    """region_median_g (human request, 2026-07-27) must be the statistical
    median of the per-other-realm cheapest listings, not the mean that
    powers sell_price_suspect -- three other realms at 1g/2g/100g have a
    median of 2g but a mean of ~34.33g, so this proves the two aren't
    accidentally sharing the same computation. Also confirms region_median_g
    is computed over *every* other-realm listing (including realm C's,
    which itself never qualifies as a snipe candidate here), not just the
    qualifying rows."""
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
    truncation before class_rank is computed) must not."""
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    monkeypatch.setattr(snipe_check, "DATA", tmp_path)

    WEAPON_ITEM, HOUSING_ITEM = 750, 751
    _stub_item_classes(monkeypatch, {WEAPON_ITEM: (2, None), HOUSING_ITEM: (20, None)})

    snap_dir = tmp_path / "snapshots" / str(SELL_CR)
    snap_dir.mkdir(parents=True)
    prev = [snap_row(9999, T0, item_id=103)]
    curr = [
        snap_row(9999, T1, item_id=103),
        snap_row(1, T1, item_id=WEAPON_ITEM, buyout=100_000),
        snap_row(2, T1, item_id=HOUSING_ITEM, buyout=100_000),
    ]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA), snap_dir / f"{ts}.parquet")

    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(parents=True)
    buy_rows = [listing_row(1000 + i, WEAPON_ITEM, buyout=1_000, auction_id=100 + i)
                for i in range(30)]  # 30 rows, all ranking above the Housing row below
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
    assert by_item.count(WEAPON_ITEM) == 30
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
