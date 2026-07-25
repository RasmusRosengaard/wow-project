"""Tests for market_key(): the coarser matching key that pools near-identical
bonus_key variants so sold-price percentiles, the current-lowest cap, and
relist detection don't fragment on Blizzard's undocumented modifiers (types
9/42/44 always, type 28 conditionally -- see below). See fetch_snapshot.py's
MARKET_IGNORE_MODIFIER_TYPES docstring for the real production cases this
was caught from: item 238014 (Sun-Blessed Sickle, crafted-item stat roll +
serial, types 42/44) and item 7761 (Steelclaw Reaver, a non-crafted item --
type 9, confirmed by a human not to affect transmog/real value, 2026-07-24).

Includes a parity check between the Python implementation
(fetch_snapshot.market_key) and its SQL mirror (analyze.MARKET_KEY_MACRO_SQL)
-- they're two separate implementations (no shared UDF, see fetch_snapshot's
docstring for why), so this test is what keeps them from silently drifting.
Parametrized over both no-base_level and real base_level cases, since the
type-28 conditional stripping (added 2026-07-25) only ever triggers with one
supplied."""
import duckdb
import pytest

import analyze
from fetch_snapshot import bonus_key, market_key

VECTORS = [
    "",
    "b:1,2,3",
    "b:12251,12253,12502|m:28=3615,29=79,38=8,39=57161,40=2691,41=192,42=487",
    "b:8955,12251,12253,12502|m:28=3615,29=78,38=8,39=57161,40=2691,41=192,42=492,44=245823",
    "b:X|m:42=487,45=1",       # ignored type is first, more follow
    "b:X|m:45=1,42=487",       # ignored type is last
    "b:X|m:44=245822",         # lone ignored type, nothing else
    "m:44=245822",             # lone ignored type, no b: part at all
    "b:X|m:42=487,44=245822",  # only ignored types
    "b:X|m:29=1,30=2",         # no ignored types present -- unchanged
    "b:6710|m:9=30,28=211",    # item 7761 real vector, type 9 first
    "b:6710|m:9=90,28=211",    # item 7761 real vector, different 9= value
    "b:X|m:28=1,9=2",          # ignored type 9 in the middle
    "b:1504,6652,10844,12265,12921|m:28=3031",  # item 237468 real vector (girdle)
    "m:28=645",                                  # item 164353 real vector (weapon), lone modifier
]


@pytest.mark.parametrize("bk", VECTORS)
def test_sql_macro_matches_python_implementation_no_base_level(bk):
    con = duckdb.connect()
    con.execute(analyze.MARKET_KEY_MACRO_SQL)
    sql_result = con.execute("SELECT market_key(?)", [bk]).fetchone()[0]
    assert sql_result == market_key(bk)


@pytest.mark.parametrize("bk,base_level", [
    (bk, base) for bk in VECTORS for base in (60, 610, 636)
])
def test_sql_macro_matches_python_implementation_with_base_level(bk, base_level):
    con = duckdb.connect()
    con.execute(analyze.MARKET_KEY_MACRO_SQL)
    sql_result = con.execute("SELECT market_key(?, ?)", [bk, base_level]).fetchone()[0]
    assert sql_result == market_key(bk, base_level)


def test_implausible_type_28_pools_across_different_values():
    """Real production case: item 164353 (Plundered Scalebane Claymore, a
    Rare weapon, base level 60) had live listings tagged 28=186, 28=189,
    28=645, 28=670, 28=289 -- none remotely close to the real base level.
    With a base_level supplied, all of these must pool to the same market
    key; without one (the pre-2026-07-25 behavior, still used by
    diff_snapshots.relist_key()), they must stay distinct."""
    variants = ["m:28=186", "m:28=189", "m:28=645", "m:28=670", "m:28=289"]
    pooled = {market_key(bk, base_level=60) for bk in variants}
    assert pooled == {""}, f"expected all variants to strip to empty, got {pooled}"
    unpooled = {market_key(bk) for bk in variants}
    assert len(unpooled) == len(variants), "without base_level, variants must stay distinct"


def test_implausible_type_28_pools_via_absolute_ceiling():
    """Real production case: item 237468 (Nightfall Executioner's Girdle,
    base level 610) showed ilvl 3031 -- caught by the absolute ceiling
    (ILVL_ABSOLUTE_MAX) regardless of which ratio multiplier is in effect.
    market_key must strip it too, not just dashboard.py's display logic."""
    bk = "b:1504,6652,10844,12265,12921|m:28=3031"
    assert market_key(bk, base_level=610) == "b:1504,6652,10844,12265,12921"


def test_plausible_type_28_is_never_stripped():
    """The legitimate case market_key must NOT break: a real, current-
    content ilvl-scaling item (base 600, claimed 636) should stay a
    distinct market from a differently-scaled copy of itself -- pooling
    these would incorrectly merge two items with different real prices."""
    low = "b:6652,10844|m:28=610,29=79"
    high = "b:6652,10844|m:28=636,29=79"
    assert market_key(low, base_level=600) != market_key(high, base_level=600)
    assert "28=610" in market_key(low, base_level=600)
    assert "28=636" in market_key(high, base_level=600)


def test_unknown_base_level_never_strips_type_28():
    """base_level=None (unknown) must never be treated as 'assume implausible
    and strip' -- that could incorrectly merge two items that really do have
    different, price-relevant item levels. Only a KNOWN implausible value
    triggers stripping."""
    bk = "m:28=3031"
    assert market_key(bk, base_level=None) == bk
    assert market_key(bk) == bk  # default is also None


def test_strips_ignored_types_keeps_the_rest():
    bk = "b:12251,12253,12502|m:28=3615,29=79,38=8,39=57161,40=2691,41=192,42=487"
    assert market_key(bk) == "b:12251,12253,12502|m:28=3615,29=79,38=8,39=57161,40=2691,41=192"


def test_two_near_identical_crafted_rolls_collapse_to_the_same_market_key():
    """The actual bug: two crafted items differing only in the per-craft
    stat roll (type 42) and serial (type 44) should pool as one market."""
    roll_a = "b:12251,12253,12500|m:28=3615,29=79,38=6,39=57161,40=2691,42=245"
    roll_b = "b:12251,12253,12500|m:28=3615,29=79,38=6,39=57161,40=2691,42=300"
    assert market_key(roll_a) == market_key(roll_b)


def test_different_ilvl_or_core_modifiers_do_not_collapse():
    """market_key must stay distinct for genuinely different items -- only
    the specific ignored types (9, 42, 44) are dropped."""
    a = "b:12251,12253,12500|m:28=3615,29=79,38=6,39=57161,40=2691,42=245"
    b = "b:12251,12253,12500|m:28=9999,29=79,38=6,39=57161,40=2691,42=245"  # different ilvl (28)
    assert market_key(a) != market_key(b)


def test_item_7761_type_9_variants_collapse_to_the_same_market_key():
    """Real production case: item 7761 (Steelclaw Reaver, not crafted) had
    nine distinct m:9=NN values region-wide for what's confirmed to be the
    same item -- without pooling, a genuinely cheap listing under one 9=
    value never matched the sell realm's data under a different 9= value,
    making it invisible to snipe_check entirely."""
    variant_a = "b:6710|m:9=30,28=211"
    variant_b = "b:6710|m:9=90,28=211"
    assert market_key(variant_a) == market_key(variant_b) == "b:6710|m:28=211"


def test_no_ignored_types_present_is_unchanged():
    bk = "b:1,2,3|m:28=100,29=200"
    assert market_key(bk) == bk


def test_empty_bonus_key_is_unchanged():
    assert market_key("") == ""


def test_noise_bonus_ids_strips_matching_b_values():
    """Real production case: item 36507 (Iron-Molded Fist) had listings
    like b:1706,6655|m:9=80,28=1099 vs b:1681,6655|m:9=68,28=1747 -- a
    genuinely cheap listing never matched the sell realm's own sales
    because every listing's first bonus_lists id was a different per-craft
    instance tag. Given the per-item noise set (determined externally by
    document frequency -- see snipe_check._populate_market_keys()), those
    ids must strip out of the b: segment, leaving the real, stable id."""
    a = "b:1706,6655|m:9=80,28=1099"
    b = "b:1681,6655|m:9=68,28=1099"
    noise = frozenset({1706, 1681})
    assert market_key(a, noise_bonus_ids=noise) == market_key(b, noise_bonus_ids=noise) == "b:6655|m:28=1099"


def test_noise_bonus_ids_does_not_touch_m_segment():
    bk = "b:1706,6655|m:9=80,28=1099"
    noise = frozenset({1706})
    # m:9 still unconditionally stripped, m:28 untouched (no base_level given)
    assert market_key(bk, noise_bonus_ids=noise) == "b:6655|m:28=1099"


def test_noise_bonus_ids_dropping_every_b_value_removes_the_segment_entirely():
    bk = "b:1706|m:9=80"
    assert market_key(bk, noise_bonus_ids=frozenset({1706})) == ""


def test_none_noise_bonus_ids_never_strips_b_segment():
    """Default/unknown noise set (None, every caller before 2026-07-25's
    second fix) must never touch the b: segment -- same 'unknown means
    don't strip' principle as base_level."""
    bk = "b:1706,6655|m:9=80"
    assert market_key(bk) == market_key(bk, noise_bonus_ids=None) == "b:1706,6655"


def test_empty_noise_bonus_ids_set_never_strips_b_segment():
    """An empty set (item didn't qualify for any noise ids, e.g. too few
    samples) behaves the same as None -- falsy, not 'strip everything'."""
    bk = "b:1706,6655|m:9=80"
    assert market_key(bk, noise_bonus_ids=frozenset()) == "b:1706,6655"


def test_noise_bonus_ids_only_strips_ids_actually_in_the_set():
    """A real, stable dimension (6655) must survive even when it's in the
    same segment as noise -- only ids explicitly in noise_bonus_ids go."""
    bk = "b:1706,6655"
    assert market_key(bk, noise_bonus_ids=frozenset({1706})) == "b:6655"
    assert market_key(bk, noise_bonus_ids=frozenset({6655})) == "b:1706"


def test_market_key_of_real_bonus_key_output_is_stable():
    """bonus_key() output should always be safely round-trippable through
    market_key() without error, for arbitrary modifier combos."""
    item = {
        "bonus_lists": [12251, 12253, 12502],
        "modifiers": [{"type": 42, "value": 487}, {"type": 28, "value": 3615}],
    }
    bk = bonus_key(item)
    assert market_key(bk) == "b:12251,12253,12502|m:28=3615"
