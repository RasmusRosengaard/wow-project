"""End-to-end pipeline test on synthetic parquet files: snapshots on disk ->
diff_snapshots.main() -> events parquet -> all analyze.py commands run clean."""
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import analyze
import diff_snapshots
from diff_snapshots import EVENT_SCHEMA
from fetch_snapshot import SCHEMA

CR = 9999
T0, T1 = 1_700_000_000, 1_700_003_600


def snap_row(auction_id, ts, item_id=101, buyout=20_000, quantity=1,
             time_left="VERY_LONG", bonus_key=""):
    return {
        "snapshot_ts": ts, "auction_id": auction_id, "item_id": item_id,
        "bonus_key": bonus_key, "pet_species_id": None, "pet_quality_id": None,
        "pet_level": None, "buyout": buyout, "bid": None,
        "quantity": quantity, "time_left": time_left,
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(diff_snapshots, "DATA", tmp_path)
    monkeypatch.setattr(analyze, "DATA", tmp_path)
    snap_dir = tmp_path / "snapshots" / str(CR)
    snap_dir.mkdir(parents=True)

    prev = [
        snap_row(1, T0),                                   # vanishes -> inferred_sale
        snap_row(2, T0, item_id=102, time_left="SHORT"),   # vanishes -> likely_expired
        snap_row(3, T0, item_id=103),                      # survives
    ]
    curr = [snap_row(3, T1, item_id=103)]
    for ts, rows_ in ((T0, prev), (T1, curr)):
        pq.write_table(pa.Table.from_pylist(rows_, schema=SCHEMA),
                       snap_dir / f"{ts}.parquet")
    return tmp_path


def run_diff(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["diff_snapshots.py", "--cr-id", str(CR)])
    diff_snapshots.main()


def test_diff_main_writes_events(data_dir, monkeypatch, capsys):
    run_diff(monkeypatch)
    events = pq.read_table(data_dir / "events" / f"{CR}.parquet")
    assert events.schema.equals(EVENT_SCHEMA)
    got = {r["auction_id"]: r["classification"] for r in events.to_pylist()}
    assert got == {1: "inferred_sale", 2: "likely_expired"}
    assert "2 disappearance events" in capsys.readouterr().out


def test_diff_main_is_idempotent(data_dir, monkeypatch, capsys):
    run_diff(monkeypatch)
    run_diff(monkeypatch)
    events = pq.read_table(data_dir / "events" / f"{CR}.parquet")
    assert events.num_rows == 2         # recomputed from scratch, not appended


def test_analyze_commands_run(data_dir, monkeypatch, capsys):
    run_diff(monkeypatch)
    con = analyze.connect(CR)
    analyze.cmd_summary(con, top=10)
    analyze.cmd_item(con, 101, price=15_000.0)
    analyze.cmd_trace(con, 101)
    out = capsys.readouterr().out
    assert "percentile" in out          # cmd_item's --price verdict
    assert "inferred_sale" in out       # trace shows the classification
