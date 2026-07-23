"""Tests for market_key(): the coarser matching key that pools near-identical
crafted-item bonus_key variants so sold-price percentiles, the current-lowest
cap, and relist detection don't fragment on Blizzard's undocumented per-craft
modifiers (types 42/44). See fetch_snapshot.py's MARKET_IGNORE_MODIFIER_TYPES
docstring for the real production case (item 238014, Sun-Blessed Sickle) this
was caught from.

Includes a parity check between the Python implementation
(fetch_snapshot.market_key) and its SQL mirror (analyze.MARKET_KEY_MACRO_SQL)
-- they're two separate implementations (no shared UDF, see fetch_snapshot's
docstring for why), so this test is what keeps them from silently drifting."""
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
]


@pytest.mark.parametrize("bk", VECTORS)
def test_sql_macro_matches_python_implementation(bk):
    con = duckdb.connect()
    con.execute(analyze.MARKET_KEY_MACRO_SQL)
    sql_result = con.execute("SELECT market_key(?)", [bk]).fetchone()[0]
    assert sql_result == market_key(bk)


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
    the specific ignored types (42, 44) are dropped."""
    a = "b:12251,12253,12500|m:28=3615,29=79,38=6,39=57161,40=2691,42=245"
    b = "b:12251,12253,12500|m:28=9999,29=79,38=6,39=57161,40=2691,42=245"  # different ilvl (28)
    assert market_key(a) != market_key(b)


def test_no_ignored_types_present_is_unchanged():
    bk = "b:1,2,3|m:28=100,29=200"
    assert market_key(bk) == bk


def test_empty_bonus_key_is_unchanged():
    assert market_key("") == ""


def test_market_key_of_real_bonus_key_output_is_stable():
    """bonus_key() output should always be safely round-trippable through
    market_key() without error, for arbitrary modifier combos."""
    item = {
        "bonus_lists": [12251, 12253, 12502],
        "modifiers": [{"type": 42, "value": 487}, {"type": 28, "value": 3615}],
    }
    bk = bonus_key(item)
    assert market_key(bk) == "b:12251,12253,12502|m:28=3615"
