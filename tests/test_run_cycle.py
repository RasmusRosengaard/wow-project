"""End-to-end test for run_cycle.py: poll sell realm -> scan region -> (once
enough history exists) rebuild events -> flag snipes, all in one process per
invocation (meant to be re-run hourly, e.g. via a recurring schedule)."""
import sys

import pytest

import analyze
import diff_snapshots
import fetch_snapshot
import run_cycle
import scan_region
import snipe_check

SELL_CR = 9999
BUY_CR = 1111


class FakeResponse:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = b"{}"
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def sell_payload(price):
    return {"auctions": [{"id": 1, "item": {"id": 101}, "buyout": price,
                          "quantity": 1, "time_left": "VERY_LONG"}]}


def buy_payload(price):
    return {"auctions": [{"id": 501, "item": {"id": 101}, "buyout": price,
                          "quantity": 1, "time_left": "VERY_LONG"}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for mod in (fetch_snapshot, diff_snapshots, analyze, scan_region, snipe_check, run_cycle):
        monkeypatch.setattr(mod, "DATA", tmp_path)
    monkeypatch.setattr(scan_region, "list_connected_realms", lambda: [BUY_CR])
    monkeypatch.setattr(scan_region, "get_auctions_with_backoff",
                        lambda *a, **k: FakeResponse(200, payload=buy_payload(5_000)))
    return tmp_path


def run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    run_cycle.main()


def test_first_cycle_skips_diff_with_one_snapshot(env, monkeypatch, capsys):
    monkeypatch.setattr(fetch_snapshot, "api_get", lambda *a, **k: FakeResponse(
        200, headers={"Last-Modified": "Mon, 14 Jul 2026 12:00:00 GMT"},
        payload=sell_payload(20_000)))
    run(monkeypatch, ["run_cycle.py", "--sell", str(SELL_CR)])
    out = capsys.readouterr().out
    assert "need 2+" in out
    assert not (env / "events" / f"{SELL_CR}.parquet").exists()
    assert (env / "listings" / f"{BUY_CR}.parquet").exists()   # region still scanned


def test_second_cycle_diffs_and_flags_snipe(env, monkeypatch, capsys):
    responses = [
        FakeResponse(200, headers={"Last-Modified": "Mon, 14 Jul 2026 12:00:00 GMT"},
                    payload=sell_payload(20_000)),
        FakeResponse(200, headers={"Last-Modified": "Mon, 14 Jul 2026 13:00:00 GMT"},
                    payload={"auctions": []}),   # item 101's auction vanished -> inferred_sale
    ]
    monkeypatch.setattr(fetch_snapshot, "api_get", lambda *a, **k: responses.pop(0))

    run(monkeypatch, ["run_cycle.py", "--sell", str(SELL_CR)])
    run(monkeypatch, ["run_cycle.py", "--sell", str(SELL_CR),
                      "--min-discount", "0.3", "--min-per-day", "0"])

    out = capsys.readouterr().out
    assert (env / "events" / f"{SELL_CR}.parquet").exists()
    assert "101" in out   # the cheap buy-realm listing got flagged as a snipe
