"""Equity-curve downsampling for the results UI.

The full equity curve for a 1-year 1-day backtest is ~365 points; for
a 1-year 1-hour backtest it's ~8760 points. The chart in the UI tops
out at a few hundred visible points before Recharts gets slow, so we
downsample server-side and ship a smaller payload.

Strategy: bucket the series into ~N/2 chronological windows, emit the
*min and max* point in each bucket so visual extremes are preserved,
and always include the first and last point so the curve starts and
ends at the true values. Result: ≤ target_points after deduplication.

This is not LTTB — it's a smaller, simpler algorithm. The visual
faithfulness is acceptable for a portfolio equity curve, where the
high-frequency wobble matters less than peaks, troughs, and endpoints.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from marketmind_shared.schemas import BenchmarkEquityPoint, EquityPoint


def _downsample[T](
    points: list[T],
    target: int,
    *,
    ts_of: Callable[[T], datetime],
    value_of: Callable[[T], float],
) -> list[T]:
    """Bucketed min+max preserving downsampling.

    Returns ≤ target points: first, last, plus min and max per bucket
    (deduplicated when min == max or timestamps collide).
    """
    if target < 2:
        raise ValueError(f"target must be >= 2; got {target}")
    n = len(points)
    if n <= target:
        return list(points)

    out: list[T] = [points[0]]
    middle = points[1:-1]
    if middle:
        # First + last are reserved; each bucket emits up to 2 points
        # (min + max). To stay ≤ target overall we get (target - 2) / 2
        # buckets — fewer if target is small.
        buckets = max(2, (target - 2) // 2)
        step = len(middle) / buckets
        for b in range(buckets):
            lo = int(b * step)
            hi = int((b + 1) * step) if b < buckets - 1 else len(middle)
            if lo >= hi:
                continue
            chunk = middle[lo:hi]
            mn = min(chunk, key=value_of)
            mx = max(chunk, key=value_of)
            mn_ts = ts_of(mn)
            mx_ts = ts_of(mx)
            if mn_ts == mx_ts:
                out.append(mn)
            elif mn_ts < mx_ts:
                out.append(mn)
                out.append(mx)
            else:
                out.append(mx)
                out.append(mn)
    out.append(points[-1])

    deduped: list[T] = []
    prev_ts: datetime | None = None
    for p in out:
        ts = ts_of(p)
        if prev_ts != ts:
            deduped.append(p)
            prev_ts = ts
    return deduped


def downsample_equity_curve(
    curve: list[EquityPoint],
    target_points: int = 500,
) -> list[EquityPoint]:
    """Downsample a strategy equity curve. First and last points always preserved."""
    return _downsample(
        curve,
        target_points,
        ts_of=lambda p: p.timestamp,
        value_of=lambda p: p.value,
    )


def downsample_benchmark_curve(
    curve: list[BenchmarkEquityPoint],
    target_points: int = 500,
) -> list[BenchmarkEquityPoint]:
    """Downsample a benchmark equity curve. Same algorithm as strategy."""
    return _downsample(
        curve,
        target_points,
        ts_of=lambda p: p.timestamp,
        value_of=lambda p: p.value,
    )


__all__ = ["downsample_benchmark_curve", "downsample_equity_curve"]
