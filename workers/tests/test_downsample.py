"""Tests for equity-curve downsampling.

Invariants we care about:
  - First and last points are always preserved exactly.
  - The peak and trough values appear somewhere in the output.
  - Output length is ≤ target_points.
  - Series shorter than target is returned untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from marketmind_shared.schemas import BenchmarkEquityPoint, EquityPoint
from marketmind_workers.backtest.downsample import (
    downsample_benchmark_curve,
    downsample_equity_curve,
)


def _eq_curve(values: list[float]) -> list[EquityPoint]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [EquityPoint(timestamp=start + timedelta(days=i), value=v) for i, v in enumerate(values)]


def _bm_curve(values: list[float]) -> list[BenchmarkEquityPoint]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BenchmarkEquityPoint(timestamp=start + timedelta(days=i), value=v)
        for i, v in enumerate(values)
    ]


def test_short_curve_returned_unchanged() -> None:
    curve = _eq_curve([100.0, 101.0, 102.0])
    out = downsample_equity_curve(curve, target_points=500)
    assert out == curve


def test_long_curve_downsampled_to_target() -> None:
    curve = _eq_curve([100.0 + i * 0.1 for i in range(5000)])
    out = downsample_equity_curve(curve, target_points=500)
    assert len(out) <= 500


def test_first_and_last_points_preserved() -> None:
    curve = _eq_curve([100.0 + i * 0.5 for i in range(5000)])
    out = downsample_equity_curve(curve, target_points=500)
    assert out[0] == curve[0]
    assert out[-1] == curve[-1]


def test_peak_and_trough_appear_in_output() -> None:
    # Construct a curve with one obvious peak and one obvious trough.
    values = [100.0] * 5000
    values[1234] = 1_000_000.0  # peak
    values[3456] = -1.0  # trough
    curve = _eq_curve(values)
    out = downsample_equity_curve(curve, target_points=500)
    vals = {p.value for p in out}
    assert 1_000_000.0 in vals
    assert -1.0 in vals


def test_output_is_chronological() -> None:
    curve = _eq_curve([100.0 + (i % 50) for i in range(2000)])
    out = downsample_equity_curve(curve, target_points=200)
    timestamps = [p.timestamp for p in out]
    assert timestamps == sorted(timestamps)
    # No duplicate timestamps.
    assert len(timestamps) == len(set(timestamps))


def test_target_too_small_raises() -> None:
    curve = _eq_curve([100.0, 110.0, 120.0])
    with pytest.raises(ValueError, match=">= 2"):
        downsample_equity_curve(curve, target_points=1)


def test_benchmark_curve_downsampled_same_way() -> None:
    curve = _bm_curve([100.0 + i * 0.3 for i in range(3000)])
    out = downsample_benchmark_curve(curve, target_points=300)
    assert len(out) <= 300
    assert out[0] == curve[0]
    assert out[-1] == curve[-1]


def test_target_equal_to_length_returns_input() -> None:
    curve = _eq_curve([100.0 + i for i in range(50)])
    out = downsample_equity_curve(curve, target_points=50)
    assert out == curve


def test_single_bar_repeating_does_not_explode() -> None:
    # Pathological: all same value. Output should still have first +
    # last, no duplicates.
    curve = _eq_curve([42.0] * 2000)
    out = downsample_equity_curve(curve, target_points=100)
    assert len(out) >= 2
    assert out[0] == curve[0]
    assert out[-1] == curve[-1]
    assert all(p.value == 42.0 for p in out)
