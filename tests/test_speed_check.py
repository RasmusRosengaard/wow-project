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
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

import auth
import blizz
import dashboard
import item_names
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


def test_sort_by_gap_is_the_default_order(listings_dir):
    rows = run(listings_dir, top=100)
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


def test_check_data_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(speed_check, "DATA", tmp_path)
    (tmp_path / "listings").mkdir()
    assert speed_check.check_data_ready() is not None
    pq.write_table(pa.Table.from_pylist([listing_row(CR_A, 101, 1, 1)], schema=LISTING_SCHEMA),
                   tmp_path / "listings" / f"{CR_A}.parquet")
    assert speed_check.check_data_ready() is None


# --------------------------------------------------------------------------
# /api/speed
# --------------------------------------------------------------------------

client = TestClient(dashboard.app)

VERIFIED_USER = User(email="v@example.com", hashed_password="x", is_active=True,
                     is_superuser=False, is_verified=True, subscription_status="active")
UNVERIFIED_USER = User(email="u@example.com", hashed_password="x", is_active=True,
                       is_superuser=False, is_verified=False, subscription_status=None)


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
