"""Walk-forward analysis.

Splits the full date range into N rolling sub-periods. Each sub-period
is split into an in-sample (IS) portion and an out-of-sample (OOS)
portion. The same spec runs on both portions of each window; we don't
re-optimise parameters per window because the Phase 1 schema has no
parameter-tuning surface.

What this *is* checking: does the same fixed strategy work consistently
across regimes? If returns crater on OOS relative to IS, that's a
signal the strategy is curve-fit to a specific market behaviour.

What this is *not* (because it can't be in v1): walk-forward
optimisation in the López de Prado sense — re-fitting parameters on
each IS window and testing on each OOS window. Adding that requires a
parameter-search surface on StrategySpec, which lands in a later phase.

`degradation_ratio = OOS_avg / IS_avg`:
  - close to 1.0  → consistent (good)
  - 0.5 to 0.8    → mild degradation
  - < 0.5         → serious degradation (overfitting signal)
  - <= 0          → either OOS lost money on average, OR IS lost
                    money (in which case the ratio is uninformative;
                    the `degradation_ratio_valid` flag is set False)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import structlog
from marketmind_shared.schemas import (
    BacktestRun,
    StrategySpec,
    WalkForwardResult,
    WindowResult,
)
from marketmind_shared.schemas.strategy_spec import spec_uses_stateful_v2
from marketmind_shared.schemas.strategy_spec.common import Timeframe

from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.metrics import compute_metrics

log = structlog.get_logger(__name__)


# Each window's IS+OOS span must be long enough to hold at least this
# many bars of the spec's primary timeframe; otherwise we can't compute
# meaningful indicators. 20 is conservative — even a 200-period SMA
# can produce signals once we have warmed it up across the full window,
# but the per-half floor protects against absurdly small slivers (1-2
# bars) where nothing meaningful can happen.
_MIN_BARS_PER_HALF: Final[int] = 20

# Approximate seconds per bar — used to pre-validate the window math
# before we start firing real backtests. Mirrors backtest/metrics.py.
_TF_SECONDS: Final[dict[Timeframe, float]] = {
    Timeframe.M1: 60.0,
    Timeframe.M5: 300.0,
    Timeframe.M15: 900.0,
    Timeframe.M30: 1800.0,
    Timeframe.H1: 3600.0,
    Timeframe.H4: 14_400.0,
    Timeframe.D1: 86_400.0,
}


def run_walk_forward(
    spec: StrategySpec,
    full_start: datetime,
    full_end: datetime,
    *,
    n_windows: int = 6,
    train_ratio: float = 0.7,
    initial_capital: float = 10_000.0,
    data_dir: str | Path = "/data",
) -> WalkForwardResult:
    """Run a walk-forward analysis. Pure orchestration over `run_backtest`."""
    if n_windows < 1:
        raise ValueError(f"n_windows must be >= 1; got {n_windows}")
    if not 0.0 < train_ratio < 1.0:
        raise ValueError(f"train_ratio must be in (0, 1); got {train_ratio}")
    if full_end <= full_start:
        raise ValueError(f"full_end ({full_end}) must be > full_start ({full_start})")

    # Drop windows that can't fit the minimum bars per half. For 4h
    # bars and a 50-bar floor, each half needs ~8.3 days, so each
    # window is ~12 days. Anything shorter is rejected.
    bar_seconds = _TF_SECONDS[spec.primary_timeframe]
    min_window_seconds = _MIN_BARS_PER_HALF * bar_seconds / min(train_ratio, 1.0 - train_ratio)
    total_seconds = (full_end - full_start).total_seconds()
    max_windows = max(1, int(total_seconds // min_window_seconds))
    n_windows_actual = min(n_windows, max_windows)

    window_seconds = total_seconds / n_windows_actual
    window_bounds: list[tuple[int, datetime, datetime, datetime]] = []
    for i in range(n_windows_actual):
        win_start = full_start + timedelta(seconds=window_seconds * i)
        win_end = full_start + timedelta(seconds=window_seconds * (i + 1))
        is_end = win_start + timedelta(seconds=window_seconds * train_ratio)
        window_bounds.append((i, win_start, is_end, win_end))

    # A v2 (stateful) spec is walked as ONE continuous backtest sliced into
    # the windows, so regime / ratchet / trade-history state evolves across
    # fold boundaries instead of resetting at each one (design doc §5.2). A
    # v1 spec keeps the cold per-segment path — bit-identical to pre-A.4.
    if spec_uses_stateful_v2(spec):
        windows = _run_windows_continuous(
            spec, window_bounds, full_start, full_end, initial_capital, data_dir,
        )
    else:
        windows = _run_windows_cold(spec, window_bounds, initial_capital, data_dir)

    return _aggregate(windows, train_ratio, n_windows, n_windows_actual)


def _run_windows_cold(
    spec: StrategySpec,
    window_bounds: list[tuple[int, datetime, datetime, datetime]],
    initial_capital: float,
    data_dir: str | Path,
) -> list[WindowResult]:
    """The v1 path: every IS/OOS segment is an independent cold backtest.

    Unchanged from pre-A.4 — `_run_segment` is called with exactly the same
    arguments, so v1 specs produce bit-identical walk-forward results.
    """
    windows: list[WindowResult] = []
    for i, win_start, is_end, win_end in window_bounds:
        is_metrics = _run_segment(spec, win_start, is_end, initial_capital, data_dir)
        oos_metrics = _run_segment(spec, is_end, win_end, initial_capital, data_dir)
        windows.append(_make_window_result(i, win_start, is_end, win_end, is_metrics, oos_metrics))
        log.info(
            "walk_forward_window",
            i=i,
            n=len(window_bounds),
            is_return=is_metrics[0],
            oos_return=oos_metrics[0],
        )
    return windows


def _run_windows_continuous(
    spec: StrategySpec,
    window_bounds: list[tuple[int, datetime, datetime, datetime]],
    full_start: datetime,
    full_end: datetime,
    initial_capital: float,
    data_dir: str | Path,
) -> list[WindowResult]:
    """The v2 path: one continuous backtest over the full range, sliced.

    For a stateful spec the only faithful walk-forward is a single run
    whose state (regime latch, ratchet extremum, TradeHistory,
    SignalHistory) evolves continuously — exactly how the A.5 trader runs
    it. Each window's IS/OOS metrics come from slicing that one run; no
    state resets at a fold boundary (design doc §5.2).
    """
    try:
        run: BacktestRun | None = run_backtest(
            spec, full_start, full_end, initial_capital, data_dir=data_dir,
        )
    except Exception as exc:
        log.warning("walk_forward_continuous_run_failed", error=str(exc))
        run = None

    last_index = window_bounds[-1][0] if window_bounds else -1
    windows: list[WindowResult] = []
    for i, win_start, is_end, win_end in window_bounds:
        if run is None:
            is_metrics: tuple[float, float, int] = (0.0, 0.0, 0)
            oos_metrics: tuple[float, float, int] = (0.0, 0.0, 0)
        else:
            is_metrics = _segment_metrics(run, spec.primary_timeframe, win_start, is_end)
            # The final window's OOS upper bound is left open so the last
            # bar — which can land exactly on full_end — is not dropped.
            oos_hi = None if i == last_index else win_end
            oos_metrics = _segment_metrics(run, spec.primary_timeframe, is_end, oos_hi)
        windows.append(_make_window_result(i, win_start, is_end, win_end, is_metrics, oos_metrics))
        log.info(
            "walk_forward_window",
            i=i,
            n=len(window_bounds),
            is_return=is_metrics[0],
            oos_return=oos_metrics[0],
        )
    return windows


def _segment_metrics(
    run: BacktestRun,
    timeframe: Timeframe,
    lo: datetime,
    hi: datetime | None,
) -> tuple[float, float, int]:
    """Slice the continuous run to ``[lo, hi)`` (``hi=None`` ⇒ open-ended)
    and compute the segment's ``(total_return_pct, sharpe, num_trades)``.

    The window's return is computed relative to the equity at the window's
    start *on the continuous curve* — not a fresh ``initial_capital`` base,
    which is the v1 cold path's convention (design doc §5.2). A trade is
    attributed to the window containing its ``entry_time``.
    """
    equity = [
        p for p in run.equity_curve if p.timestamp >= lo and (hi is None or p.timestamp < hi)
    ]
    trades = [
        t for t in run.trades if t.entry_time >= lo and (hi is None or t.entry_time < hi)
    ]
    # A degenerate slice (<2 equity points) can't yield meaningful metrics;
    # report it as a zero window rather than crash compute_metrics.
    if len(equity) < 2:
        return 0.0, 0.0, len(trades)
    sliced = run.model_copy(update={"equity_curve": equity, "trades": trades})
    metrics = compute_metrics(sliced, timeframe)
    return metrics.total_return_pct, metrics.sharpe_ratio, metrics.num_trades


def _make_window_result(
    index: int,
    win_start: datetime,
    is_end: datetime,
    win_end: datetime,
    is_metrics: tuple[float, float, int],
    oos_metrics: tuple[float, float, int],
) -> WindowResult:
    """Assemble a WindowResult from the IS/OOS `(return, sharpe, trades)`
    tuples. Shared by the cold and continuous paths so the WindowResult
    shape is identical regardless of how the segment metrics were obtained.
    """
    return WindowResult(
        window_index=index,
        in_sample_start=win_start,
        in_sample_end=is_end,
        out_of_sample_start=is_end,
        out_of_sample_end=win_end,
        in_sample_return_pct=is_metrics[0],
        in_sample_sharpe=is_metrics[1],
        in_sample_num_trades=is_metrics[2],
        out_of_sample_return_pct=oos_metrics[0],
        out_of_sample_sharpe=oos_metrics[1],
        out_of_sample_num_trades=oos_metrics[2],
    )


def _run_segment(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    initial_capital: float,
    data_dir: str | Path,
) -> tuple[float, float, int]:
    """Run one IS or OOS segment. Returns (total_return_pct, sharpe, num_trades).

    Any engine failure (not enough bars, no signals, etc.) is caught
    and reported as a zero-return / zero-trade window rather than
    aborting the whole walk-forward.
    """
    try:
        run: BacktestRun = run_backtest(spec, start, end, initial_capital, data_dir=data_dir)
    except Exception as exc:
        log.warning(
            "walk_forward_segment_failed",
            start=start.isoformat(),
            end=end.isoformat(),
            error=str(exc),
        )
        return 0.0, 0.0, 0
    metrics = compute_metrics(run, spec.primary_timeframe)
    return metrics.total_return_pct, metrics.sharpe_ratio, metrics.num_trades


def _aggregate(
    windows: list[WindowResult],
    train_ratio: float,
    n_windows_requested: int,
    n_windows_actual: int,
) -> WalkForwardResult:
    """Roll up per-window stats into the aggregate signals."""
    if not windows:
        return WalkForwardResult(
            windows=[],
            in_sample_avg_return=0.0,
            out_of_sample_avg_return=0.0,
            degradation_ratio=0.0,
            degradation_ratio_valid=False,
            out_of_sample_positive_rate=0.0,
            consistency_score=0.0,
            train_ratio=train_ratio,
            n_windows_requested=n_windows_requested,
            n_windows_actual=n_windows_actual,
        )

    is_returns = [w.in_sample_return_pct for w in windows]
    oos_returns = [w.out_of_sample_return_pct for w in windows]
    is_avg = sum(is_returns) / len(is_returns)
    oos_avg = sum(oos_returns) / len(oos_returns)

    # `degradation_ratio_valid` flags IS-non-positive — the ratio is
    # mathematically defined but uninformative (no IS edge to degrade).
    if is_avg > 0.0:
        degradation = oos_avg / is_avg
        degradation_valid = True
    else:
        degradation = 0.0
        degradation_valid = False

    oos_positive = sum(1 for r in oos_returns if r > 0) / len(oos_returns)
    consistency = _consistency_score(oos_returns)

    return WalkForwardResult(
        windows=windows,
        in_sample_avg_return=is_avg,
        out_of_sample_avg_return=oos_avg,
        degradation_ratio=degradation,
        degradation_ratio_valid=degradation_valid,
        out_of_sample_positive_rate=oos_positive,
        consistency_score=consistency,
        train_ratio=train_ratio,
        n_windows_requested=n_windows_requested,
        n_windows_actual=n_windows_actual,
    )


def _consistency_score(returns: list[float]) -> float:
    """Map cross-window std of OOS returns to a [0, 1] consistency
    score. std = 0 → 1.0; std = 0.5 (i.e., ±50% return swing across
    windows) → 0.5; very large std → ~0.

    This is a soft proxy. We do NOT use coefficient-of-variation
    because OOS returns can sit near zero in a flat regime, making
    abs(mean) tiny and the ratio uselessly spiky.
    """
    if len(returns) < 2:
        return 1.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    return 1.0 / (1.0 + 2.0 * std)


__all__ = ["run_walk_forward"]
