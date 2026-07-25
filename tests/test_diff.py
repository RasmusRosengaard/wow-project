"""Synthetic-fixture tests for the sale-inference core (diff_snapshots.classify_pair).

Two snapshots one hour apart (gap 3600s), one auction per classification, plus
edge cases: oversized gaps downgrade to ambiguous, and relist matching consumes
one new listing per vanished duplicate.
"""
import pyarrow as pa
import pytest

from diff_snapshots import EVENT_SCHEMA, MIN_REMAINING, classify_pair

GAP = 3600
TS = 1_700_003_600


def auction(auction_id, item_id=1000, bonus_key="", buyout=10_000, quantity=1,
            time_left="VERY_LONG", pet_species_id=None, pet_quality_id=None):
    return {
        "auction_id": auction_id,
        "item_id": item_id,
        "bonus_key": bonus_key,
        "pet_species_id": pet_species_id,
        "pet_quality_id": pet_quality_id,
        "buyout": buyout,
        "quantity": quantity,
        "time_left": time_left,
    }


def snap(*auctions):
    return {a["auction_id"]: a for a in auctions}


def by_id(events):
    return {e["auction_id"]: e for e in events}


@pytest.fixture
def fixture_events():
    """The canonical Phase 0 fixture: five vanishing auctions, one survivor."""
    prev = snap(
        auction(1, item_id=101, time_left="VERY_LONG"),            # -> inferred_sale
        auction(2, item_id=102, time_left="LONG", buyout=5_000),   # -> likely_relisted
        auction(3, item_id=103, time_left="SHORT"),                # -> likely_expired
        auction(4, item_id=104, time_left="MEDIUM"),               # -> ambiguous
        auction(5, item_id=105, time_left="VERY_LONG", buyout=None),  # -> bid_only_gone
        auction(6, item_id=106, time_left="LONG"),                 # survivor
    )
    curr = snap(
        auction(6, item_id=106, time_left="LONG"),                 # still listed
        auction(7, item_id=102, buyout=5_000, time_left="VERY_LONG"),  # relist of #2
    )
    return classify_pair(prev, curr, gap=GAP, ts=TS)


def test_all_five_classifications(fixture_events):
    got = {e["auction_id"]: e["classification"] for e in fixture_events}
    assert got == {
        1: "inferred_sale",
        2: "likely_relisted",
        3: "likely_expired",
        4: "ambiguous",
        5: "bid_only_gone",
    }


def test_survivor_produces_no_event(fixture_events):
    assert 6 not in by_id(fixture_events)


def test_event_fields(fixture_events):
    e = by_id(fixture_events)[1]
    assert e["ts"] == TS
    assert e["gap_seconds"] == GAP
    assert e["item_id"] == 101
    assert e["time_left"] == "VERY_LONG"
    assert e["unit_price"] == 10_000.0


def test_events_fit_schema(fixture_events):
    table = pa.Table.from_pylist(fixture_events, schema=EVENT_SCHEMA)
    assert table.num_rows == 5


def test_bid_only_has_no_unit_price(fixture_events):
    assert by_id(fixture_events)[5]["unit_price"] is None


def test_unit_price_is_per_unit():
    prev = snap(auction(1, buyout=50_000, quantity=20))
    [e] = classify_pair(prev, snap(), gap=GAP, ts=TS)
    assert e["unit_price"] == 2_500.0
    assert e["quantity"] == 20


def test_oversized_gap_downgrades_to_ambiguous():
    """A gap >= the bucket's minimum remaining time (collector downtime) means
    the auction could have expired -> ambiguous, not inferred_sale."""
    prev = snap(
        auction(1, time_left="LONG"),
        auction(2, time_left="VERY_LONG"),
    )
    big_gap = MIN_REMAINING["VERY_LONG"]  # >= both buckets' minimums
    events = by_id(classify_pair(prev, snap(), gap=big_gap, ts=TS))
    assert events[1]["classification"] == "ambiguous"
    assert events[2]["classification"] == "ambiguous"


def test_gap_just_under_bucket_minimum_is_sale():
    prev = snap(auction(1, time_left="LONG"))
    [e] = classify_pair(prev, snap(), gap=MIN_REMAINING["LONG"] - 1, ts=TS)
    assert e["classification"] == "inferred_sale"


def test_duplicate_vanish_single_relist():
    """Two identical vanished listings but only one fresh copy: the relist match
    is consumed once -> one likely_relisted + one inferred_sale."""
    prev = snap(
        auction(1, item_id=200, buyout=7_500),
        auction(2, item_id=200, buyout=7_500),
    )
    curr = snap(auction(3, item_id=200, buyout=7_500))
    got = sorted(e["classification"] for e in classify_pair(prev, curr, gap=GAP, ts=TS))
    assert got == ["inferred_sale", "likely_relisted"]


def test_relist_requires_exact_variant_match():
    """A new listing with a different bonus_key or price is NOT a relist."""
    prev = snap(
        auction(1, item_id=300, bonus_key="b:6654", buyout=9_000),
        auction(2, item_id=300, bonus_key="b:6654", buyout=9_999),
    )
    curr = snap(auction(3, item_id=300, bonus_key="b:1234", buyout=9_000))
    got = {e["auction_id"]: e["classification"]
           for e in classify_pair(prev, curr, gap=GAP, ts=TS)}
    assert got == {1: "inferred_sale", 2: "inferred_sale"}


def test_relist_matches_across_different_crafted_stat_rolls():
    """A crafted item relisted with a *different* per-craft stat roll
    (modifier type 42) and serial (type 44) -- Blizzard's undocumented
    per-craft modifiers, see fetch_snapshot.MARKET_IGNORE_MODIFIER_TYPES --
    should still match as a relist via market_key(), not get miscounted as
    an inferred_sale just because the exact roll changed on repost."""
    prev = snap(
        auction(1, item_id=400, buyout=5_000,
                bonus_key="b:12251,12253,12502|m:28=3615,42=487,44=245822"),
    )
    curr = snap(
        auction(2, item_id=400, buyout=5_000,
                bonus_key="b:12251,12253,12502|m:28=3615,42=999,44=999999"),
    )
    [e] = classify_pair(prev, curr, gap=GAP, ts=TS)
    assert e["classification"] == "likely_relisted"


def test_relist_requires_matching_pet_identity():
    """Two different caged pets (82800) with the same buyout/quantity must NOT
    be treated as relists of each other -- pet identity lives in
    pet_species_id/pet_quality_id, not bonus_key (empty for cages)."""
    prev = snap(
        auction(1, item_id=82800, buyout=10_000, pet_species_id=1, pet_quality_id=1),
        auction(2, item_id=82800, buyout=10_000, pet_species_id=2, pet_quality_id=4),
    )
    curr = snap(
        auction(3, item_id=82800, buyout=10_000, pet_species_id=1, pet_quality_id=1),
    )
    got = {e["auction_id"]: e["classification"]
           for e in classify_pair(prev, curr, gap=GAP, ts=TS)}
    assert got == {1: "likely_relisted", 2: "inferred_sale"}


def test_relist_tolerates_a_nearby_price_change():
    """A troll reposting the same listing at a different joke price should
    still count as a relist, not a fake inferred_sale -- real production
    case (item 13051, "Witchfury", 2026-07-25): two camped listings priced
    647,999.98g and 667,999.98g (same bonus_key, ~3% apart) were
    misclassified as two separate sales instead of one relist."""
    prev = snap(auction(1, item_id=13051, buyout=6_479_999_800))  # 647,999.98g
    curr = snap(auction(2, item_id=13051, buyout=6_679_999_800))  # 667,999.98g (~3% higher)
    [e] = classify_pair(prev, curr, gap=GAP, ts=TS)
    assert e["classification"] == "likely_relisted"


def test_relist_price_tolerance_has_a_limit():
    """A genuinely different price -- not just a troll's nearby repost --
    must still count as a real sale, not get swallowed by the tolerance
    band into a false relist match."""
    prev = snap(auction(1, item_id=500, buyout=10_000))
    curr = snap(auction(2, item_id=500, buyout=50_000))  # 5x higher, well outside tolerance
    [e] = classify_pair(prev, curr, gap=GAP, ts=TS)
    assert e["classification"] == "inferred_sale"


def test_relist_tolerance_still_consumes_each_candidate_once():
    """Two vanished listings each within tolerance of a *different* fresh
    candidate must each get their own match, not double-match onto the
    same fresh listing -- the tolerance band shouldn't break the existing
    one-candidate-per-relist consumption rule (see
    test_duplicate_vanish_single_relist)."""
    prev = snap(
        auction(1, item_id=700, buyout=10_000),
        auction(2, item_id=700, buyout=10_200),
    )
    curr = snap(
        auction(3, item_id=700, buyout=10_050),
        auction(4, item_id=700, buyout=10_150),
    )
    got = [e["classification"] for e in classify_pair(prev, curr, gap=GAP, ts=TS)]
    assert got == ["likely_relisted", "likely_relisted"]


def test_missing_time_left_defaults_to_medium():
    prev = snap(auction(1, time_left=None))
    [e] = classify_pair(prev, snap(), gap=GAP, ts=TS)
    assert e["time_left"] == "MEDIUM"
    assert e["classification"] == "ambiguous"
