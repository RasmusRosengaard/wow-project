"""Tests for speed_check.py -- the experimental +Speed tertiary listing
census (2026-08-12) and its /api/speed route.

Mirrors test_snipe_check.py's synthetic-pipeline fixture style (real
duckdb/pyarrow, no mocking of the query itself). Two things here are load-
bearing beyond ordinary coverage:

1. The Python/SQL parity check, following the precedent set by
   tests/test_market_key.py: has_speed() and SPEED_FILTER_SQL are two
   implementations of one rule, and the vectors they're checked against are
   **real bonus_key strings taken from a live region sweep**, not invented
   ones (CLAUDE.md's definition of done requires real vectors for exactly
   this class of duplicated matching logic).
2. The exact-match tests. A substring implementation of "does this key
   contain bonus 42" passes every naive test while silently matching 142,
   420 and 1042 -- which would quietly turn the whole feature into noise.
"""
import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

import auth
import blizz
import dashboard
import fetch_snapshot
import item_names
import scan_region
import speed_check
from db import User
from scan_region import LISTING_SCHEMA

CR_A, CR_B, CR_C = 1111, 2222, 3333
T1 = 1_700_003_600

# Real bonus_key strings pulled from data/listings/*.parquet on 2026-08-12,
# paired with whether they carry the +Speed tertiary. The first four came
# back from the live `b:42` scan; the last three are real non-Speed keys
# (including two whose bonus ids merely *contain* the digits 42, which is
# the trap a substring implementation falls into).
REAL_VECTORS = [
    ("b:42,1504,10844,12265|m:28=2462", True),
    ("b:42,12769,13668|m:28=3321", True),
    ("b:42,3278,10255,10258,10356,10392,10395|m:28=2462", True),
    ("b:42,7579,13559|m:28=2169", True),
    ("b:1678,6654|m:9=80", False),
    ("b:1420,6654", False),          # 1420 contains "42" -- must not match
    ("b:142,4200,6654", False),      # 142 and 4200 -- must not match
    ("", False),
    ("m:28=2462", False),            # modifiers only, no b: segment at all
]


def listing_row(cr, item_id, buyout, auction_id, bonus_key="", quantity=1):
    return {
        "cr_id": cr, "fetched_ts": T1, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": None, "pet_quality_id": None,
        "pet_level": None, "buyout": buyout, "bid": None, "quantity": quantity,
        "time_left": "VERY_LONG",
    }


SPEED_BK = "b:42,1504,10844|m:28=2462"
PLAIN_BK = "b:1678,6654|m:9=80"


@pytest.fixture
def listings_dir(tmp_path, monkeypatch):
    """Item 101: listed with +Speed on three realms (200g/1000g/1200g) and
    plain on one (100g). Per-realm +Speed floors are 200/1000/1200, so the
    median reference is 1000g and the 200g listing sits 5x under it.

    Item 202: +Speed on a single realm only -- no reference, gap_x must be
    NULL rather than invented.

    Item 303: no +Speed listing at all -- must never appear.
    """
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    d = tmp_path / "listings"
    d.mkdir(parents=True)
    rows = [
        listing_row(CR_A, 101, 200 * 10_000, 1, bonus_key=SPEED_BK),
        listing_row(CR_B, 101, 1000 * 10_000, 2, bonus_key=SPEED_BK),
        listing_row(CR_C, 101, 1200 * 10_000, 3, bonus_key=SPEED_BK),
        # A second, pricier +Speed listing on CR_A: the per-realm floor must
        # take the 200g one, not this, when building the reference.
        listing_row(CR_A, 101, 5000 * 10_000, 4, bonus_key=SPEED_BK),
        listing_row(CR_A, 101, 100 * 10_000, 5, bonus_key=PLAIN_BK),
        listing_row(CR_A, 202, 700 * 10_000, 6, bonus_key=SPEED_BK),
        listing_row(CR_A, 303, 50 * 10_000, 7, bonus_key=PLAIN_BK),
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA), d / f"{CR_A}.parquet")
    return tmp_path


# --------------------------------------------------------------------------
# The bonus-id rule itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bonus_key,expected", REAL_VECTORS)
def test_has_speed_matches_ids_exactly(bonus_key, expected):
    assert speed_check.has_speed(bonus_key) is expected


def test_has_speed_handles_none():
    assert speed_check.has_speed(None) is False


@pytest.mark.parametrize("bonus_key,expected", REAL_VECTORS)
def test_python_sql_parity(bonus_key, expected):
    """SPEED_FILTER_SQL must agree with has_speed() on every real vector.
    These are two independent implementations of one rule (the Python one
    for pure/testable use, the SQL one so ~2.5M listings are filtered
    in-engine); this is the check that keeps them from drifting, same
    convention as test_market_key.py's macro parity test."""
    con = duckdb.connect()
    try:
        sql_result = con.execute(
            f"SELECT {speed_check.SPEED_FILTER_SQL} FROM (SELECT ? AS bonus_key)",
            [bonus_key],
        ).fetchone()[0]
    finally:
        con.close()
    assert bool(sql_result) is expected
    assert bool(sql_result) is speed_check.has_speed(bonus_key)


def test_tertiary_ids_are_the_verified_four():
    """Guards the verified mapping against a careless edit -- each id was
    confirmed against a real listed item's rendered tooltip (2026-08-12),
    and the set's mutual exclusivity was confirmed across 7,932 live
    bonus_keys. Re-verify against real data before changing any of this."""
    assert speed_check.TERTIARY_BONUS_IDS == {
        40: "Avoidance", 41: "Leech", 42: "Speed", 43: "Indestructible"}
    assert speed_check.SPEED_BONUS_ID == 42


# --------------------------------------------------------------------------
# find_speed_listings()
# --------------------------------------------------------------------------

def run(listings_dir, **kwargs):
    con = speed_check.connect()
    try:
        return speed_check.find_speed_listings(con, **kwargs)
    finally:
        con.close()


def test_returns_only_speed_listings(listings_dir):
    rows = run(listings_dir, top=100)
    assert {r["item_id"] for r in rows} == {101, 202}
    assert all(speed_check.has_speed(r["bonus_key"]) for r in rows)
    # The plain listing of item 101 exists in the fixture and must not be a row.
    assert all(r["auction_id"] != 5 for r in rows)


def test_reference_is_median_of_per_realm_floors(listings_dir):
    """The 5000g listing on CR_A must not drag the reference up: CR_A's
    floor is its own cheapest (200g), so the three per-realm floors are
    200/1000/1200 and the median is 1000g."""
    row = next(r for r in run(listings_dir, top=100) if r["auction_id"] == 1)
    assert row["speed_region_median"] == 1000 * 10_000
    assert row["speed_realm_count"] == 3
    assert row["speed_listing_count"] == 4
    assert row["gap_x"] == pytest.approx(5.0)


def test_plain_cheapest_is_the_non_speed_floor(listings_dir):
    row = next(r for r in run(listings_dir, top=100) if r["auction_id"] == 1)
    assert row["plain_cheapest"] == 100 * 10_000


def test_single_realm_item_has_no_invented_gap(listings_dir):
    """Item 202 is listed with +Speed on exactly one realm. There is no
    honest reference for it, so gap_x is NULL -- falling back to the plain
    price (or to the item's own single listing) would be inventing the
    pricing judgement this module deliberately doesn't make."""
    row = next(r for r in run(listings_dir, top=100) if r["item_id"] == 202)
    assert row["gap_x"] is None
    assert row["speed_realm_count"] == 1


def test_nothing_is_filtered_by_default(listings_dir):
    """The default is a census: no discount rule, no value floor, no gap
    cutoff. All four +Speed listings of item 101 plus item 202's come back,
    including the 5000g one that is 5x *above* its own item's reference."""
    rows = run(listings_dir, top=100)
    assert len(rows) == 5
    assert any(r["auction_id"] == 4 for r in rows)


def test_sort_by_gap(listings_dir):
    """sort="gap" was the default until 2026-08-12 (see
    test_default_sort_is_cheapest_first); still supported, now opt-in."""
    rows = run(listings_dir, top=100, sort="gap")
    gaps = [r["gap_x"] for r in rows if r["gap_x"] is not None]
    assert gaps == sorted(gaps, reverse=True)
    assert rows[0]["auction_id"] == 1
    # NULL gaps (single-realm items) sort last rather than dropping out.
    assert rows[-1]["item_id"] == 202


def test_min_gap_is_opt_in(listings_dir):
    assert len(run(listings_dir, top=100, min_gap=3)) == 1
    assert len(run(listings_dir, top=100, min_gap=100)) == 0


def test_gold_bounds_convert_at_the_boundary(listings_dir):
    """min_gold/max_gold are the only gold-denominated inputs; everything
    downstream is copper (CLAUDE.md's units rule)."""
    rows = run(listings_dir, top=100, min_gold=1000)
    assert {r["unit_price"] for r in rows} == {1000 * 10_000, 1200 * 10_000, 5000 * 10_000}
    rows = run(listings_dir, top=100, max_gold=250)
    assert {r["unit_price"] for r in rows} == {200 * 10_000}


def test_item_filter(listings_dir):
    rows = run(listings_dir, top=100, items=[202])
    assert {r["item_id"] for r in rows} == {202}


def test_unit_price_divides_by_quantity(tmp_path, monkeypatch):
    """Stacked listings are compared per unit, same as the snipe path."""
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    d = tmp_path / "listings"
    d.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [listing_row(CR_A, 101, 1000 * 10_000, 1, bonus_key=SPEED_BK, quantity=5)],
            schema=LISTING_SCHEMA),
        d / f"{CR_A}.parquet")
    rows = run(tmp_path, top=10)
    assert rows[0]["unit_price"] == 200 * 10_000


def test_bad_sort_rejected(listings_dir):
    con = speed_check.connect()
    try:
        with pytest.raises(ValueError):
            speed_check.find_speed_listings(con, sort="'; DROP TABLE listings; --")
    finally:
        con.close()


def test_latest_sweep_ts(listings_dir):
    """Read from the listings' own fetched_ts column, not file mtime -- the
    column records when the sweep actually ran and survives a file copy."""
    con = speed_check.connect()
    try:
        assert speed_check.latest_sweep_ts(con) == T1
    finally:
        con.close()


def test_latest_sweep_ts_is_none_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    d = tmp_path / "listings"
    d.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([], schema=LISTING_SCHEMA), d / "empty.parquet")
    con = speed_check.connect()
    try:
        assert speed_check.latest_sweep_ts(con) is None
    finally:
        con.close()


def test_check_data_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    (tmp_path / "listings").mkdir()
    assert speed_check.check_data_ready() is not None
    pq.write_table(pa.Table.from_pylist([listing_row(CR_A, 101, 1, 1)], schema=LISTING_SCHEMA),
                   tmp_path / "listings" / f"{CR_A}.parquet")
    assert speed_check.check_data_ready() is None


# --------------------------------------------------------------------------
# Item level
# --------------------------------------------------------------------------

# Real bonus_key shapes from the live sweep (2026-08-12), with the item level
# each one renders at -- every value verified by rendering that exact bonus id
# against a real listed item and reading the level off the tooltip, and
# confirmed set-wide rather than per-item (12817 gives 266 on both Sentinel's
# Cover 258931 and Corsair's Tunic 258920).
ILVL_VECTORS = [
    ("b:42,12817,13578|m:28=5381", 266),
    ("b:6652,12817,13578|m:28=3321", 266),
    ("b:13900,13578|m:9=90,28=5381", 253),
    ("b:42,13901,13578|m:28=3321", 260),
    ("b:42,12667,12769,13578|m:28=3321", 220),
    ("b:6652,12667,13578,13730|m:9=90,28=3321", 198),
    ("b:13578,13729|m:9=90,28=3321", 192),
    # Carries only ids that turned out to have no item-level effect at all
    # (they leave the item at its base 44), so there is nothing to report.
    ("b:6652,13578,13663|m:28=3321", None),
    ("", None),
    # Looted below max character level, so the upgrade id overstates the real
    # level -- reported as unknown rather than as a number we can't compute.
    ("b:42,12817,13578|m:9=88,28=5381", None),
    ("b:13578,13729|m:9=80,28=3321", None),
]


@pytest.mark.parametrize("bonus_key,expected", ILVL_VECTORS)
def test_ilvl_of(bonus_key, expected):
    # `==`, not `is`: CPython only interns ints up to 256, so `is` would pass
    # for 253 and fail for 260/266 -- which is exactly what it did first time.
    assert speed_check.ilvl_of(bonus_key) == expected


@pytest.mark.parametrize("bonus_key,expected", ILVL_VECTORS)
def test_ilvl_python_sql_parity(bonus_key, expected):
    """ILVL_SQL is generated from ILVL_BONUS_IDS rather than hand-written, so
    the mapping itself can't drift -- this checks the generated expression
    evaluates the same way ilvl_of() does."""
    con = duckdb.connect()
    try:
        got = con.execute(f"SELECT {speed_check.ILVL_SQL} FROM (SELECT ? AS bonus_key)",
                          [bonus_key]).fetchone()[0]
    finally:
        con.close()
    assert got == expected


def test_ilvl_is_not_modifier_28():
    """The whole point of ILVL_BONUS_IDS. Modifier 28 claims to be an item
    level and is what dashboard._variant_label() shows elsewhere, but on this
    family it reports junk -- 5381 on an item that is really 266. Trusting it
    would put confidently wrong levels on every row."""
    bk = "b:42,12817,13578|m:28=5381"
    assert speed_check.ilvl_of(bk) == 266
    assert fetch_snapshot.parse_bonus_key(bk)["mods"][28] == "5381"


def test_downscaled_listing_cannot_masquerade_as_a_tracked_tier():
    """Human, 2026-08-12: an item "dropped at another character lvl e.g. if i
    looted the box at lvl 88 instead of 90" shows a lower level in game. So a
    listing whose upgrade id says 266 but which was looted at 88 is NOT a 266,
    and must not be sold to the user as one -- the same class of error that
    makes modifier 28 unusable here."""
    assert speed_check.ilvl_of("b:42,12817,13578|m:28=5381") == 266
    assert speed_check.ilvl_of("b:42,12817,13578|m:9=90,28=5381") == 266
    assert speed_check.ilvl_of("b:42,12817,13578|m:9=88,28=5381") is None


@pytest.mark.parametrize("bonus_key,expected", [
    ("b:42,12817,13578|m:9=88,28=5381", 88),
    ("b:13578,13729|m:9=90,28=3321", 90),
    ("b:42,12817,13578|m:28=5381", None),
    # 28= and 19= must not be read as a modifier-9 value via their trailing 9.
    ("b:13578|m:19=5,28=3321", None),
    ("", None),
])
def test_acquired_level(bonus_key, expected):
    assert speed_check.acquired_level(bonus_key) == expected


def test_tracked_ilvls_are_the_two_midnight_tiers():
    assert speed_check.TRACKED_ILVLS == [253, 266]
    for lvl in speed_check.TRACKED_ILVLS:
        assert lvl in speed_check.ILVL_BONUS_IDS.values()


def test_ilvl_filter_narrows_rows_and_reference(tmp_path, monkeypatch):
    """The filter applies to the reference stats too, not just the visible
    rows: a 266's "typical +Speed price" must come from other 266s, not from
    the 192s that dominate the item by volume and are worth a fraction as
    much."""
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    d = tmp_path / "listings"
    d.mkdir(parents=True)
    hi, lo = "b:42,12817,13578", "b:42,13578,13729"      # 266, 192
    rows = [
        listing_row(CR_A, 101, 500 * 10_000, 1, bonus_key=hi),
        listing_row(CR_B, 101, 900 * 10_000, 2, bonus_key=hi),
        listing_row(CR_C, 101, 1100 * 10_000, 3, bonus_key=hi),
        # Cheap low-ilvl listings that would drag the reference down.
        listing_row(CR_A, 101, 10 * 10_000, 4, bonus_key=lo),
        listing_row(CR_B, 101, 12 * 10_000, 5, bonus_key=lo),
        listing_row(CR_C, 101, 14 * 10_000, 6, bonus_key=lo),
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=LISTING_SCHEMA), d / f"{CR_A}.parquet")

    got = run(tmp_path, top=50, ilvls=[266])
    assert {r["auction_id"] for r in got} == {1, 2, 3}
    assert all(r["ilvl"] == 266 for r in got)
    # Reference is the median of the three 266 per-realm floors (900g), not
    # something pulled down by the 10-14g ilvl-192 listings.
    assert got[0]["speed_region_median"] == 900 * 10_000

    # Unfiltered, the same query pools both tiers and the reference collapses.
    unfiltered = run(tmp_path, top=50)
    assert len(unfiltered) == 6
    assert unfiltered[0]["speed_region_median"] < 900 * 10_000


def test_ilvl_column_present_when_unfiltered(listings_dir):
    """Fixture bonus_keys carry no upgrade id, so ilvl is None rather than
    absent -- the column always exists."""
    rows = run(listings_dir, top=10)
    assert all("ilvl" in r and r["ilvl"] is None for r in rows)


# --------------------------------------------------------------------------
# Name / Tarnished filter
# --------------------------------------------------------------------------

class FakeNames:
    """Minimal NameCache stand-in -- resolve_item_filter() only needs
    ensure_many/get/quality/item_class/item_subclass/save."""

    def __init__(self, mapping, quality=None, classes=None):
        self.mapping = mapping
        self.qualities = quality or {}
        self.classes = classes or {}   # item_id -> (item_class, item_subclass)
        self.ensured = None

    def ensure_many(self, item_ids, **kwargs):
        self.ensured = list(item_ids)

    def get(self, item_id):
        # Mirrors the real NameCache: an unresolved item comes back as
        # "item <id>", which is exactly why it can never match a name filter.
        return self.mapping.get(item_id, f"item {item_id}")

    def quality(self, item_id):
        return self.qualities.get(item_id)

    def item_class(self, item_id):
        return self.classes.get(item_id, (None, None))[0]

    def item_subclass(self, item_id):
        return self.classes.get(item_id, (None, None))[1]

    def save(self):
        pass


def test_speed_item_ids_are_the_speed_universe(listings_dir):
    con = speed_check.connect()
    try:
        assert sorted(speed_check.speed_item_ids(con)) == [101, 202]
    finally:
        con.close()


def test_resolve_name_filter_matches_case_insensitively(listings_dir):
    names = FakeNames({101: "Tarnished Dawnlit Mace", 202: "Steelscale Striders"})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_name_filter(con, "tarnished dawnlit", names=names) == [101]
        # Only the +Speed universe is ever resolved -- that bound is what
        # makes this filter affordable (see the function's docstring).
        assert sorted(names.ensured) == [101, 202]
    finally:
        con.close()


def test_bare_tarnished_would_catch_legacy_items(listings_dir):
    """The reason TARNISHED_NAME_MATCH is the two-word phrase. Item 202 here
    stands in for the real legacy items ("Tarnished Chain Vest" 2379,
    "Tarnished Plate Belt" 25381, "Tarnished Claymore" 25400 — 22 of them in
    the live name cache, vanilla through Legion). Bare "Tarnished" pulls
    them in; the Midnight phrase does not."""
    names = FakeNames({101: "Tarnished Dawnlit Mace", 202: "Tarnished Chain Vest"})
    con = speed_check.connect()
    try:
        assert sorted(speed_check.resolve_name_filter(con, "Tarnished", names=names)) == [101, 202]
        assert speed_check.resolve_name_filter(
            con, speed_check.TARNISHED_NAME_MATCH, names=names) == [101]
    finally:
        con.close()


def test_resolve_name_filter_intersects_with_explicit_items(listings_dir):
    names = FakeNames({101: "Tarnished Dawnlit Mace", 202: "Tarnished Dawnlit Band"})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_name_filter(
            con, "Tarnished Dawnlit", items=[202], names=names) == [202]
    finally:
        con.close()


def test_unresolved_items_cannot_match(listings_dir):
    """A cold cache that times out mid-prewarm under-reports rather than
    silently letting unnamed items through."""
    names = FakeNames({})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_name_filter(con, "Tarnished Dawnlit", names=names) == []
    finally:
        con.close()


def test_tarnished_match_is_the_two_word_phrase():
    assert speed_check.TARNISHED_NAME_MATCH == "Tarnished Dawnlit"


def test_armor_type_filter(listings_dir):
    names = FakeNames({101: "Leather Hood", 202: "Cloth Robe"},
                      classes={101: (4, 2), 202: (4, 1)})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_item_filter(con, armor_types=["leather"], names=names) == [101]
        assert speed_check.resolve_item_filter(con, armor_types=["cloth"], names=names) == [202]
        assert sorted(speed_check.resolve_item_filter(
            con, armor_types=["cloth", "leather"], names=names)) == [101, 202]
    finally:
        con.close()


def test_weapon_bucket_matches_the_whole_class(listings_dir):
    """Weapons are class 2 with ~15 subclasses; the bucket has subclass None
    so a dagger and a staff both match."""
    names = FakeNames({101: "Dagger", 202: "Staff"},
                      classes={101: (2, 15), 202: (2, 10)})
    con = speed_check.connect()
    try:
        assert sorted(speed_check.resolve_item_filter(
            con, armor_types=["weapon"], names=names)) == [101, 202]
    finally:
        con.close()


def test_cloaks_count_as_cloth(listings_dir):
    """Real Blizzard classification, not a bug: all four Tarnished Dawnlit
    capes are armor subclass 1, including the plate-themed Commander's Cape.
    Pinned so nobody "fixes" it into name-based re-bucketing later."""
    names = FakeNames({101: "Tarnished Dawnlit Commander's Cape"}, classes={101: (4, 1)})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_item_filter(con, armor_types=["cloth"], names=names) == [101]
        assert speed_check.resolve_item_filter(con, armor_types=["plate"], names=names) == []
    finally:
        con.close()


def test_quality_filter_accepts_player_names(listings_dir):
    names = FakeNames({101: "Green Thing", 202: "Blue Thing"},
                      quality={101: "UNCOMMON", 202: "RARE"})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_item_filter(con, qualities=["green"], names=names) == [101]
        assert speed_check.resolve_item_filter(con, qualities=["blue"], names=names) == [202]
        assert sorted(speed_check.resolve_item_filter(
            con, qualities=["green", "blue"], names=names)) == [101, 202]
        # Raw Blizzard tier names work too.
        assert speed_check.resolve_item_filter(con, qualities=["UNCOMMON"], names=names) == [101]
    finally:
        con.close()


def test_filters_combine(listings_dir):
    names = FakeNames({101: "Tarnished Dawnlit Corsair's Hood",
                       202: "Tarnished Dawnlit Spellbinder's Robe"},
                      quality={101: "UNCOMMON", 202: "UNCOMMON"},
                      classes={101: (4, 2), 202: (4, 1)})
    con = speed_check.connect()
    try:
        assert speed_check.resolve_item_filter(
            con, name_contains="Tarnished Dawnlit", qualities=["green"],
            armor_types=["leather"], names=names) == [101]
    finally:
        con.close()


def test_unknown_filter_values_raise(listings_dir):
    """A typo'd filter erroring beats one that silently returns an empty
    page -- the empty page is much harder to diagnose."""
    names = FakeNames({})
    con = speed_check.connect()
    try:
        with pytest.raises(ValueError):
            speed_check.resolve_item_filter(con, armor_types=["lether"], names=names)
        with pytest.raises(ValueError):
            speed_check.resolve_item_filter(con, qualities=["greeen"], names=names)
    finally:
        con.close()


def test_default_sort_is_cheapest_first(listings_dir):
    """Changed 2026-08-12: the buy price is what matters when sniping these,
    not how far under a reference they sit."""
    rows = run(listings_dir, top=100)
    prices = [r["unit_price"] for r in rows]
    assert prices == sorted(prices)


# --------------------------------------------------------------------------
# /api/speed
# --------------------------------------------------------------------------

client = TestClient(dashboard.app)

VERIFIED_USER = User(email="v@example.com", hashed_password="x", is_active=True,
                     is_superuser=False, is_verified=True, subscription_status="active")
UNVERIFIED_USER = User(email="u@example.com", hashed_password="x", is_active=True,
                       is_superuser=False, is_verified=False, subscription_status=None)


@pytest.fixture(autouse=True)
def isolate_publish_state(tmp_path, monkeypatch):
    """/api/speed's freshness figure reads scan_region's publish state (see
    dashboard._region_published_ts). Point it at tmp_path so tests can't
    depend on -- or be broken by -- whatever the real, gitignored local sweep
    state happens to contain."""
    monkeypatch.setattr(scan_region, "DATA", tmp_path)


@pytest.fixture(autouse=True)
def stub_realm_info(monkeypatch):
    monkeypatch.setattr(dashboard, "_realm_info_cache", {})
    monkeypatch.setattr(blizz, "connected_realm_realms",
                        lambda cr_id: [{"name": f"Realm {cr_id}", "slug": f"realm-{cr_id}",
                                        "category": "English"}])


@pytest.fixture(autouse=True)
def isolate_item_names_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(item_names, "CACHE_PATH", tmp_path / "item_names_test_cache.json")
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": None, "quality": None, "level": None,
                                          "inventory_type": None, "item_class": 4,
                                          "item_subclass": 2})


def as_user(monkeypatch, user):
    async def fake_resolve(request):
        return user
    monkeypatch.setattr(auth, "resolve_user_from_request", fake_resolve)


def test_api_speed_requires_login(listings_dir, monkeypatch):
    as_user(monkeypatch, None)
    assert client.get("/api/speed").status_code == 401


def test_api_speed_403s_for_unverified(listings_dir, monkeypatch):
    """403 rather than 401 so the frontend can tell "log in" from "confirm
    your email" -- the same distinction auth.current_verified_user makes."""
    as_user(monkeypatch, UNVERIFIED_USER)
    assert client.get("/api/speed").status_code == 403


def test_api_speed_returns_rows(listings_dir, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    r = client.get("/api/speed", params={"top": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["bonus_id"] == 42
    assert body["tertiary"] == "Speed"
    assert body["caveat"] == speed_check.CAVEAT
    assert body["count"] == 5
    top_row = body["rows"][0]
    assert top_row["item_id"] == 101
    assert top_row["realm_name"] == f"Realm {CR_A}"
    # Both money variants exposed, per CLAUDE.md's units rule.
    assert top_row["price_copper"] == 200 * 10_000
    assert top_row["price_g"] == 200
    assert top_row["speed_region_median_g"] == 1000
    assert top_row["plain_cheapest_g"] == 100
    assert top_row["gap_x"] == pytest.approx(5.0)
    # Freshness. With no publish state recorded, this falls back to the
    # sweep's own fetch time.
    assert body["collected_ts"] == T1
    assert body["fetched_ts"] == T1


def test_api_speed_reports_collected_ts_even_with_no_matches(listings_dir, monkeypatch):
    """The timestamp describes the sweep, not the rows -- an empty result
    still has to say how fresh the data behind it is."""
    as_user(monkeypatch, VERIFIED_USER)
    r = client.get("/api/speed", params={"name_contains": "nothing matches", "top": 100})
    body = r.json()
    assert body["count"] == 0
    assert body["collected_ts"] == T1


def test_api_speed_tarnished_uses_the_server_side_phrase(listings_dir, monkeypatch):
    """The frontend sends only a flag; the phrase itself never leaves the
    server, so there's no second copy to drift."""
    as_user(monkeypatch, VERIFIED_USER)
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": "Tarnished Dawnlit Mace" if item_id == 101
                                                 else "Tarnished Chain Vest",
                                         "quality": None, "level": None,
                                         "inventory_type": None, "item_class": 4,
                                         "item_subclass": 2})
    r = client.get("/api/speed", params={"tarnished": "true", "top": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["name_filter"] == "Tarnished Dawnlit"
    assert body["tarnished_match"] == "Tarnished Dawnlit"
    assert {row["item_id"] for row in body["rows"]} == {101}


def test_api_speed_name_contains_filters(listings_dir, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {"name": f"Thing {item_id}", "quality": None,
                                         "level": None, "inventory_type": None,
                                         "item_class": 4, "item_subclass": 2})
    r = client.get("/api/speed", params={"name_contains": "Thing 202", "top": 100})
    assert r.status_code == 200
    assert {row["item_id"] for row in r.json()["rows"]} == {202}


def test_api_speed_name_filter_with_no_match_returns_empty(listings_dir, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    r = client.get("/api/speed", params={"name_contains": "nothing matches this", "top": 100})
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_api_speed_filters_by_armor_and_quality(listings_dir, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    details = {101: {"name": "Corsair's Hood", "quality": "UNCOMMON", "item_class": 4,
                     "item_subclass": 2},
               202: {"name": "Spellbinder's Robe", "quality": "RARE", "item_class": 4,
                     "item_subclass": 1}}
    monkeypatch.setattr(item_names, "_fetch_item_details",
                        lambda item_id: {**details[item_id], "level": None,
                                         "inventory_type": None})
    r = client.get("/api/speed", params={"armor": "leather", "top": 100})
    assert {row["item_id"] for row in r.json()["rows"]} == {101}
    r = client.get("/api/speed", params={"quality": "blue", "top": 100})
    assert {row["item_id"] for row in r.json()["rows"]} == {202}
    r = client.get("/api/speed", params={"quality": "green,blue", "top": 100})
    assert {row["item_id"] for row in r.json()["rows"]} == {101, 202}


def test_api_speed_filters_by_ilvl(tmp_path, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    d = tmp_path / "listings"
    d.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([
        listing_row(CR_A, 101, 500 * 10_000, 1, bonus_key="b:42,12817,13578"),   # 266
        listing_row(CR_A, 202, 10 * 10_000, 2, bonus_key="b:42,13578,13729"),    # 192
    ], schema=LISTING_SCHEMA), d / f"{CR_A}.parquet")

    r = client.get("/api/speed", params={"ilvl": "253,266", "top": 100})
    body = r.json()
    assert {row["item_id"] for row in body["rows"]} == {101}
    assert body["rows"][0]["ilvl"] == 266
    assert body["ilvl_filter"] == [253, 266]
    assert body["tracked_ilvls"] == [253, 266]
    assert 266 in body["known_ilvls"] and 253 in body["known_ilvls"]


def test_api_speed_400s_on_non_numeric_ilvl(listings_dir, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    assert client.get("/api/speed", params={"ilvl": "266,abc"}).status_code == 400


def test_api_speed_400s_on_unknown_filter_values(listings_dir, monkeypatch):
    """Validated on the route, not inside the worker thread, so a typo is a
    clean 400 rather than a 500."""
    as_user(monkeypatch, VERIFIED_USER)
    assert client.get("/api/speed", params={"armor": "lether"}).status_code == 400
    assert client.get("/api/speed", params={"quality": "greeen"}).status_code == 400


def test_api_speed_rejects_bad_sort(listings_dir, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    assert client.get("/api/speed", params={"sort": "nope"}).status_code == 400


def test_api_speed_400s_without_a_sweep(tmp_path, monkeypatch):
    as_user(monkeypatch, VERIFIED_USER)
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    (tmp_path / "listings").mkdir()
    r = client.get("/api/speed")
    assert r.status_code == 400
    assert "scan_region" in r.json()["detail"]


def test_speed_page_serves(listings_dir):
    r = client.get("/speed")
    assert r.status_code == 200
    assert "+Speed" in r.text


def test_collected_ts_prefers_blizzards_publish_time(listings_dir, monkeypatch, tmp_path):
    """The bug this fixes: /dashboard showed Blizzard's Last-Modified while
    /speed showed `int(time.time())` from when our scanner ran, so the two
    pages disagreed by the scan lag (~4 min live) for the same data. The
    headline number is now the publish moment, with the fetch time kept
    separately."""
    as_user(monkeypatch, VERIFIED_USER)
    published = T1 - 240  # Blizzard published 4 minutes before we fetched
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sweep_publish.json").write_text(json.dumps({
        "1111": {"published_ts": published, "last_modified": "x"},
        "2222": {"published_ts": published + 1, "last_modified": "x"},
    }))
    monkeypatch.setattr(scan_region, "DATA", tmp_path)

    body = client.get("/api/speed", params={"top": 100}).json()
    # Oldest publish across realms, so the figure is never fresher than the
    # stalest data on the page.
    assert body["collected_ts"] == published
    assert body["fetched_ts"] == T1


def test_collected_ts_falls_back_when_no_publish_state(listings_dir, monkeypatch):
    """A volume with listings but no recorded sweep state still has to report
    something rather than blanking the freshness line."""
    as_user(monkeypatch, VERIFIED_USER)
    body = client.get("/api/speed", params={"top": 100}).json()
    assert body["collected_ts"] == T1
