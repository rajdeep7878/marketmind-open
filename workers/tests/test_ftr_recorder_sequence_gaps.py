"""Recorder: lastUpdateId sequence-gap detection + snapshot resync logic,
exercised on fixtures (FIXTURE — NOT MARKET DATA)."""

from __future__ import annotations

from pathlib import Path

from marketmind_workers.ftr.data.recorder import DepthSequencer, Recorder
from marketmind_workers.ftr.data.recorder_fixtures import make_depth_events_fixture


def test_sequencer_applies_contiguous_events() -> None:
    seq = DepthSequencer()
    seq.apply_snapshot(1000)
    events = make_depth_events_fixture(last_update_id=1000, n_events=50)
    verdicts = [seq.check(int(e["U"]), int(e["u"])) for e in events]  # type: ignore[arg-type]
    assert verdicts[0] == "apply"  # bridging event
    assert all(v == "apply" for v in verdicts)


def test_sequencer_detects_gap_and_requests_resync() -> None:
    seq = DepthSequencer()
    seq.apply_snapshot(1000)
    events = make_depth_events_fixture(last_update_id=1000, n_events=50, inject_gap_at=20)
    verdicts = [seq.check(int(e["U"]), int(e["u"])) for e in events]  # type: ignore[arg-type]
    assert verdicts[20] == "resync"


def test_sequencer_skips_pre_snapshot_events() -> None:
    seq = DepthSequencer()
    seq.apply_snapshot(5000)
    # events that predate the snapshot must be skipped, not applied
    assert seq.check(4500, 4600) == "skip"
    # bridging event straddling lastUpdateId+1 applies
    assert seq.check(4990, 5010) == "apply"
    # contiguous continuation applies
    assert seq.check(5011, 5020) == "apply"


def test_recorder_buffers_and_rotates(tmp_path: Path) -> None:
    rec = Recorder(["BTC/USDT"], tmp_path)
    rec.sequencers["BTC/USDT"].apply_snapshot(1000)
    for e in make_depth_events_fixture(last_update_id=1000, n_events=30):
        needs_resync = rec.handle_event("BTC/USDT", "depth", e)
        assert not needs_resync
    rec.handle_event(
        "BTC/USDT",
        "aggTrade",
        {"E": 1, "p": "50000", "q": "0.5", "m": False, "a": 1},
    )
    rec.handle_event(
        "BTC/USDT",
        "bookTicker",
        {"E": 2, "b": "49999", "B": "1.0", "a": "50001", "A": "2.0"},
    )
    written = rec.rotate()
    names = {p.name for p in written}
    assert {"depth.parquet", "trades.parquet", "book_ticker.parquet", "manifest.json"} <= names
    # buffers reset after rotation
    assert rec.buffers["BTC/USDT"].depth == []


def test_recorder_counts_sequence_gaps(tmp_path: Path) -> None:
    rec = Recorder(["BTC/USDT"], tmp_path)
    rec.sequencers["BTC/USDT"].apply_snapshot(1000)
    events = make_depth_events_fixture(last_update_id=1000, n_events=30, inject_gap_at=10)
    resyncs = sum(1 for e in events if rec.handle_event("BTC/USDT", "depth", e))
    assert resyncs >= 1
    assert rec.buffers["BTC/USDT"].sequence_gaps >= 1
