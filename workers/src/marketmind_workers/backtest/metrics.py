"""Compute annualised performance + trade-level metrics from a BacktestRun.

The split with the engine (Phase 3.1) is intentional: `compute_metrics`
is a pure function over `BacktestRun` plus the `Timeframe`, so a Phase
4 walk-forward pass can re-compute metrics on rolling sub-windows
without re-running the backtest engine.

Annualization gotchas, baked in:

  - Bars per year is computed from the actual timeframe enum, NOT
    assumed to be daily (365). A 4h backtest has 6 bars/day * 365 =
    2190 bars/year; an annualised Sharpe on a 4h backtest must use
    sqrt(2190), not sqrt(252) or sqrt(365).
  - Volatility uses bar-to-bar log returns of the equity curve, NOT
    trade returns. Trade returns under-count risk because closed-
    position bars have zero variance.
  - Drawdown duration is measured in calendar days (peak → recovery
    or end-of-series if never recovered), to match how every other
    financial-stats library reports it.
  - Edge cases never produce NaN/Inf: zero trades collapses trade
    stats to zeros; infinite profit_factor caps at 1e6 with a flag.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypedDict

import numpy as np
import pandas as pd
from marketmind_shared.schemas import BacktestMetrics, BacktestRun, EquityPoint, Trade
from marketmind_shared.schemas.strategy_spec.common import Timeframe


class _TradeStats(TypedDict):
    num_trades: int
    win_rate: float
    profit_factor: float
    profit_factor_capped: bool
    avg_win_pct: float
    avg_loss_pct: float
    expectancy: float
    largest_win_pct: float
    largest_loss_pct: float
    longest_winning_streak: int
    longest_losing_streak: int
    avg_trade_duration_days: float


# Approximate bars per calendar year for each supported timeframe.
# Used for annualization of Sharpe/Sortino/volatility/CAGR. Crypto
# trades 24/7, so 365 days/year (not 252) is correct here.
_BARS_PER_YEAR: dict[Timeframe, float] = {
    Timeframe.M1: 60.0 * 24.0 * 365.0,
    Timeframe.M5: 12.0 * 24.0 * 365.0,
    Timeframe.M15: 4.0 * 24.0 * 365.0,
    Timeframe.M30: 2.0 * 24.0 * 365.0,
    Timeframe.H1: 24.0 * 365.0,
    Timeframe.H4: 6.0 * 365.0,
    Timeframe.D1: 365.0,
}

# Cap for infinite profit factor (zero losses + at least one win).
# Picked so the value is recognisable as "saturated" in a chart but
# still small enough to be JSON-safe at any precision.
_PF_CAP: float = 1.0e6


def bars_per_year(tf: Timeframe) -> float:
    """Approximate bars per calendar year. Exposed for tests + Phase 4."""
    return _BARS_PER_YEAR[tf]


def compute_metrics(run: BacktestRun, timeframe: Timeframe) -> BacktestMetrics:
    """Compute the full metric set for a finished BacktestRun.

    `timeframe` is taken from the run's meta and passed in explicitly
    rather than re-read so test fixtures can hand a synthetic Run +
    matching timeframe without a full Meta object.
    """
    bpy = _BARS_PER_YEAR[timeframe]
    equity = _equity_series(run.equity_curve)

    total_return_pct, cagr, vol, sharpe, sortino = _return_stats(equity, bpy)
    dd_pct, dd_days = _drawdown_stats(equity)
    calmar = cagr / abs(dd_pct) if dd_pct > 0 else 0.0

    trade_stats = _trade_stats(run.trades)
    exposure = _exposure(run.trades, run.equity_curve)

    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr=cagr,
        annualized_volatility=vol,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=dd_pct,
        max_drawdown_duration_days=dd_days,
        calmar_ratio=calmar,
        num_trades=trade_stats["num_trades"],
        win_rate=trade_stats["win_rate"],
        profit_factor=trade_stats["profit_factor"],
        profit_factor_capped=trade_stats["profit_factor_capped"],
        avg_win_pct=trade_stats["avg_win_pct"],
        avg_loss_pct=trade_stats["avg_loss_pct"],
        expectancy=trade_stats["expectancy"],
        largest_win_pct=trade_stats["largest_win_pct"],
        largest_loss_pct=trade_stats["largest_loss_pct"],
        longest_winning_streak=trade_stats["longest_winning_streak"],
        longest_losing_streak=trade_stats["longest_losing_streak"],
        avg_trade_duration_days=trade_stats["avg_trade_duration_days"],
        exposure_pct=exposure,
        bars_processed=len(equity),
        bars_per_year=bpy,
    )


# ---- equity-curve stats ---------------------------------------------------


def _equity_series(curve: Sequence[EquityPoint]) -> pd.Series:
    """Convert the list-of-objects curve into a Series for vectorised math."""
    if not curve:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([p.timestamp for p in curve])
    return pd.Series([p.value for p in curve], index=idx, dtype="float64")


def _return_stats(
    equity: pd.Series,
    bpy: float,
) -> tuple[float, float, float, float, float]:
    """Compute (total_return, cagr, vol, sharpe, sortino) from the equity
    curve. All return values are fractions (0.12 == 12%).

    Volatility comes from bar-to-bar log returns. Sharpe = mean*bpy /
    (stdev*sqrt(bpy)) which simplifies to mean*sqrt(bpy)/stdev for
    return series — we use the textbook (mean_return * bpy) /
    (stdev_return * sqrt(bpy)) form which is robust to integer-cast
    bpy values.
    """
    if len(equity) < 2:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    initial = float(equity.iloc[0])
    final = float(equity.iloc[-1])
    if initial <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    total_return = final / initial - 1.0

    # CAGR uses calendar years between first and last bar (close enough
    # to backtest "exposure span"). Falls back to 0 for a one-day run.
    start_ts: pd.Timestamp = pd.Timestamp(equity.index[0])  # type: ignore[arg-type]
    end_ts: pd.Timestamp = pd.Timestamp(equity.index[-1])  # type: ignore[arg-type]
    years = max((end_ts - start_ts).total_seconds() / (365.25 * 86400.0), 1e-9)
    cagr = (final / initial) ** (1.0 / years) - 1.0 if final > 0 else -1.0

    # Bar returns: log-returns are more accurate for compounding stats,
    # but simple percent-returns are what every textbook Sharpe formula
    # is built around, so use those.
    rets = equity.pct_change().dropna()
    if len(rets) < 2 or rets.std(ddof=1) == 0:
        return total_return, cagr, 0.0, 0.0, 0.0

    mean_r = float(rets.mean())
    std_r = float(rets.std(ddof=1))
    vol = std_r * math.sqrt(bpy)
    sharpe = (mean_r * bpy) / vol if vol > 0 else 0.0

    # Downside deviation: only count negative returns toward stdev.
    downside = rets.clip(upper=0.0)
    downside_var = float((downside**2).mean())
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0
    sortino = (mean_r * bpy) / (downside_std * math.sqrt(bpy)) if downside_std > 0 else 0.0

    return total_return, cagr, vol, sharpe, sortino


def _drawdown_stats(equity: pd.Series) -> tuple[float, int]:
    """Return (max_drawdown_pct as positive fraction, max_dd_duration_days).

    Drawdown duration measured peak-to-recovery in calendar days. If
    the equity curve never recovers from its deepest drawdown by the
    end of the run, duration is peak-to-end.
    """
    if len(equity) < 2:
        return 0.0, 0
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max  # negative or zero
    if drawdown.empty:
        return 0.0, 0
    min_pos = int(np.argmin(drawdown.to_numpy()))
    max_dd_pct = float(-drawdown.iloc[min_pos])  # flip sign -> positive fraction

    # Identify the peak BEFORE the worst drawdown bar.
    peak_pos = int(np.argmax(equity.iloc[: min_pos + 1].to_numpy()))
    peak_ts: pd.Timestamp = pd.Timestamp(equity.index[peak_pos])  # type: ignore[arg-type]
    peak_value = float(equity.iloc[peak_pos])

    # Recovery: first bar AFTER the trough where equity >= peak_value.
    post = equity.iloc[min_pos:]
    recovered = post[post >= peak_value]
    end_ts_obj: pd.Timestamp
    if recovered.empty:
        end_ts_obj = pd.Timestamp(equity.index[-1])  # type: ignore[arg-type]
    else:
        end_ts_obj = pd.Timestamp(recovered.index[0])  # type: ignore[arg-type]

    duration_days = max(int((end_ts_obj - peak_ts).total_seconds() / 86400.0), 0)
    return max_dd_pct, duration_days


# ---- trade-level stats ----------------------------------------------------


def _trade_stats(trades: Sequence[Trade]) -> _TradeStats:
    """Compute all per-trade metrics in one pass.

    Returns a dict keyed by the BacktestMetrics field name so the
    caller spreads it into the model directly. Avoids a 12-tuple
    return signature.
    """
    num = len(trades)
    if num == 0:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "profit_factor_capped": False,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "expectancy": 0.0,
            "largest_win_pct": 0.0,
            "largest_loss_pct": 0.0,
            "longest_winning_streak": 0,
            "longest_losing_streak": 0,
            "avg_trade_duration_days": 0.0,
        }

    returns = [t.return_pct for t in trades]
    pnls = [t.pnl for t in trades]
    durations_days = [
        max((t.exit_time - t.entry_time).total_seconds() / 86400.0, 0.0) for t in trades
    ]

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    win_rate = len(wins) / num
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    expectancy = float(np.mean(returns))
    largest_win = max(returns) if returns else 0.0
    largest_loss = min(returns) if returns else 0.0
    avg_dur = float(np.mean(durations_days))

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)  # positive
    pf_capped = False
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = _PF_CAP
        pf_capped = True
    else:
        profit_factor = 0.0

    # Winning / losing streaks
    longest_win = 0
    longest_loss = 0
    cur_win = 0
    cur_loss = 0
    for r in returns:
        if r > 0:
            cur_win += 1
            cur_loss = 0
            longest_win = max(longest_win, cur_win)
        elif r < 0:
            cur_loss += 1
            cur_win = 0
            longest_loss = max(longest_loss, cur_loss)
        else:
            # Break-even trades count for neither streak.
            cur_win = 0
            cur_loss = 0

    return {
        "num_trades": num,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "profit_factor_capped": pf_capped,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "expectancy": expectancy,
        "largest_win_pct": largest_win,
        "largest_loss_pct": largest_loss,
        "longest_winning_streak": longest_win,
        "longest_losing_streak": longest_loss,
        "avg_trade_duration_days": avg_dur,
    }


def _exposure(trades: Sequence[Trade], curve: Sequence[EquityPoint]) -> float:
    """Fraction of the backtest duration spent with an open position.

    Sum each trade's duration, divide by total run duration. Capped at
    1.0 in case overlapping fills ever sneak through (they shouldn't
    in the long-only / short-only Phase 3.1 engine).
    """
    if not trades or len(curve) < 2:
        return 0.0
    total = (curve[-1].timestamp - curve[0].timestamp).total_seconds()
    if total <= 0:
        return 0.0
    occupied = sum((t.exit_time - t.entry_time).total_seconds() for t in trades)
    return min(max(occupied / total, 0.0), 1.0)


__all__ = ["bars_per_year", "compute_metrics"]
