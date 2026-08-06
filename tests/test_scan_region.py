"""Tests for the region scanner's pure functions (rows) and sweep robustness —
no network involved."""
import pyarrow.parquet as pq

import scan_region
from scan_region import rows


# --- rows ----------------------------------------------------------------

def test_rows_maps_auction_fields():
    payload = {"auctions": [{
        "id": 7, "item": {"id": 152510, "bonus_lists": [42]},
        "buyout": 50_000, "bid": 40_000, "quantity": 5, "time_left": "LONG",
    }]}
    [r] = rows(payload, cr=1403, ts=1_700_000_000)
    assert r == {
        "cr_id": 1403, "fetched_ts": 1_700_000_000, "auction_id": 7,
        "item_id": 152510, "bonus_key": "b:42", "pet_species_id": None,
        "pet_quality_id": None, "pet_level": None, "buyout": 50_000,
        "bid": 40_000, "quantity": 5, "time_left": "LONG",
    }


def test_rows_handles_empty():
    assert rows({}, cr=1403, ts=0) == []
    assert rows({"auctions": None}, cr=1403, ts=0) == []


# --- scan_one / sweep ------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False, last_modified=None):
        self.status_code = status
        self.headers = {"Last-Modified": last_modified} if last_modified else {}
        self.content = b"{}"
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("truncated body")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_scan_one_writes_listings(monkeypatch, tmp_path):
    payload = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                             "quantity": 1, "time_left": "LONG"}]}
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload=payload))

    n, _lm = scan_region.scan_one(1403, ts=1_700_000_000)
    assert n == 1
    table = pq.read_table(tmp_path / "listings" / "1403.parquet")
    assert table.schema.equals(scan_region.LISTING_SCHEMA)
    assert table.num_rows == 1


def test_scan_one_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    """scan_one() writes to a temp file then atomically renames it over the
    final path (2026-07-25, after a real production crash: a reader could
    open the final path mid-write and get a truncated parquet file). A
    successful write must leave the listings dir with only the final file,
    no leftover .parquet.tmp."""
    payload = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                             "quantity": 1, "time_left": "LONG"}]}
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload=payload))

    scan_region.scan_one(1403, ts=1_700_000_000)
    listings_dir = tmp_path / "listings"
    assert sorted(p.name for p in listings_dir.iterdir()) == ["1403.parquet"]


def test_scan_one_overwrite_is_atomic_rename(monkeypatch, tmp_path):
    """A second sweep overwriting an existing listings file must go through
    the same temp-file-then-rename path, not a direct in-place write."""
    payload_a = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                               "quantity": 1, "time_left": "LONG"}]}
    payload_b = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                               "quantity": 1, "time_left": "LONG"},
                              {"id": 2, "item": {"id": 3}, "buyout": 200,
                               "quantity": 1, "time_left": "LONG"}]}
    monkeypatch.setattr(scan_region, "DATA", tmp_path)

    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload=payload_a))
    scan_region.scan_one(1403, ts=1_700_000_000)

    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload=payload_b))
    n, _lm = scan_region.scan_one(1403, ts=1_700_000_100)

    assert n == 2
    listings_dir = tmp_path / "listings"
    assert sorted(p.name for p in listings_dir.iterdir()) == ["1403.parquet"]
    table = pq.read_table(listings_dir / "1403.parquet")
    assert table.num_rows == 2


def test_scan_one_skips_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, bad_json=True))
    assert scan_region.scan_one(1403, ts=0) == (0, None)
    assert not (tmp_path / "listings" / "1403.parquet").exists()


def test_scan_one_returns_last_modified(monkeypatch, tmp_path):
    """The publish time rides on a response the sweep already makes -- it's
    what lets an alert timestamp be told apart from a listing that merely
    became newly eligible (cooldown expiry, TSM refresh, cache convergence)."""
    payload = {"auctions": [{"id": 1, "item": {"id": 2}, "buyout": 100,
                             "quantity": 1, "time_left": "LONG"}]}
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(
                            200, payload=payload,
                            last_modified="Thu, 06 Aug 2026 05:44:43 GMT"))

    n, lm = scan_region.scan_one(1403, ts=1_700_000_000)
    assert (n, lm) == (1, "Thu, 06 Aug 2026 05:44:43 GMT")


def test_scan_one_reports_no_publish_time_for_malformed_body(monkeypatch, tmp_path):
    """A dump we couldn't parse leaves the previous sweep's parquet in place,
    so its Last-Modified must not be recorded -- doing so would claim a
    freshness the file on disk doesn't have."""
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(
                            200, bad_json=True,
                            last_modified="Thu, 06 Aug 2026 05:44:43 GMT"))
    assert scan_region.scan_one(1403, ts=0) == (0, None)


# --- publish-time recording -----------------------------------------------

def test_parse_http_date_survives_garbage():
    assert scan_region._parse_http_date("Thu, 06 Aug 2026 05:44:43 GMT") == 1785995083
    assert scan_region._parse_http_date(None) is None
    assert scan_region._parse_http_date("") is None
    assert scan_region._parse_http_date("not a date") is None


def test_sweep_records_publish_times(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [1096, 1305])
    monkeypatch.setattr(scan_region, "scan_one",
                        lambda cr, ts: (3, "Thu, 06 Aug 2026 05:44:43 GMT"))
    monkeypatch.setattr(scan_region.time, "time", lambda: 1785995100)

    scan_region.sweep(exclude=set())

    state = scan_region.load_publish_state()
    assert set(state) == {"1096", "1305"}
    assert state["1096"] == {"last_modified": "Thu, 06 Aug 2026 05:44:43 GMT",
                             "published_ts": 1785995083,
                             "first_seen_ts": 1785995100}


def test_sweep_keeps_first_seen_ts_until_the_dump_actually_changes(monkeypatch, tmp_path):
    """first_seen_ts must mark the sweep that first saw *this* dump, not the
    most recent sweep to re-read it -- otherwise it measures our own poll
    cadence instead of detection lag, which is the whole point of recording
    it."""
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [1096])
    monkeypatch.setattr(scan_region, "scan_one",
                        lambda cr, ts: (3, "Thu, 06 Aug 2026 05:44:43 GMT"))

    monkeypatch.setattr(scan_region.time, "time", lambda: 1785995100)
    scan_region.sweep(exclude=set())
    monkeypatch.setattr(scan_region.time, "time", lambda: 1785995160)
    scan_region.sweep(exclude=set())     # same dump, one minute later
    assert scan_region.load_publish_state()["1096"]["first_seen_ts"] == 1785995100

    # A genuinely new publish does move it.
    monkeypatch.setattr(scan_region, "scan_one",
                        lambda cr, ts: (3, "Thu, 06 Aug 2026 06:41:26 GMT"))
    monkeypatch.setattr(scan_region.time, "time", lambda: 1785998500)
    scan_region.sweep(exclude=set())
    entry = scan_region.load_publish_state()["1096"]
    assert (entry["published_ts"], entry["first_seen_ts"]) == (1785998486, 1785998500)


def test_sweep_excludes_realms_and_survives_one_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "list_connected_realms",
                        lambda: [1096, 1403, 1305])

    def fake_scan_one(cr, ts):
        if cr == 1305:
            raise RuntimeError("HTTP 500")
        return 3, "Thu, 06 Aug 2026 05:44:43 GMT"

    monkeypatch.setattr(scan_region, "scan_one", fake_scan_one)
    scan_region.sweep(exclude={1403})   # 1403 (sell realm) skipped entirely
    # 1096 succeeds, 1305 raises but sweep() must not propagate the exception.
    # The failed realm records no publish time; the excluded one is never asked.
    assert set(scan_region.load_publish_state()) == {"1096"}


def test_sweep_survives_unreadable_publish_state(monkeypatch, tmp_path):
    """Corrupt diagnostics state must never take a sweep down -- same
    reasoning as watchlist._rule_state_load()."""
    monkeypatch.setattr(scan_region, "DATA", tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "sweep_publish.json").write_text("{truncated")
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [1096])
    monkeypatch.setattr(scan_region, "scan_one",
                        lambda cr, ts: (3, "Thu, 06 Aug 2026 05:44:43 GMT"))

    scan_region.sweep(exclude=set())     # must not raise
    assert "1096" in scan_region.load_publish_state()
