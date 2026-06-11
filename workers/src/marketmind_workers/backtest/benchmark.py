"""Buy-and-hold benchmark and strategy-vs-benchmark comparison.

This is the non-negotiable core of Phase 3.2: every backtest result
must be displayed against a same-period, same-instrument passive hold
so users can answer the real question — "did this strategy actually
add value, or could I have just held BTC?".

Conventions baked in:

  - Entry: first bar's OPEN, lifted by `slippage_pct`, minus an entry
    commission. Matches the strategy engine's "no fill on the signal
    bar's close" convention (Phase 3.1 fills at next-bar open with
    fees + slippage).
  - Exit: last bar's CLOSE, lowered by `slippage_pct`, minus an exit
    commission. The strategy's end_of_data exits pay commission too,
    so the comparison is fair.
  - Equity curve: mark-to-market at each bar's CLOSE using the held
    share count. The exit commission is reflected only in
    `final_value` (one cell), NOT in every curve point — this keeps
    the chart smooth.
  - All percent fields are fractions (0.12 == 12%) for chart math.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from marketmind_shared.schemas import (
    BacktestMetrics,
    BenchmarkComparison,
    BenchmarkEquityPoint,
    BenchmarkResult,
)
from marketmind_shared.schemas.strategy_spec.common import Timeframe

from marketmind_workers.backtest.metrics import bars_per_year as _bars_per_year
from marketmind_workers.services.market_data import get_market_data

# Defaults match Phase 1's DEFAULT_COST_MODEL — kept literal here so a
# benchmark caller doesn't have to import the spec module just to get
# the same numbers. If the spec defaults change, change these too.
_DEFAULT_COMMISSION_PCT: float = 0.001
_DEFAULT_SLIPPAGE_PCT: float = 0.0005


def compute_buy_and_hold(
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    initial_capital: float = 10_000.0,
    commission_pct: float = _DEFAULT_COMMISSION_PCT,
    slippage_pct: float = _DEFAULT_SLIPPAGE_PCT,
    data_dir: str | Path = "/data",
    ohlcv: pd.DataFrame | None = None,
) -> BenchmarkResult:
    """Simulate a buy-at-first-bar / hold-to-last-bar passive position.

    Pass `ohlcv` directly (a DataFrame with columns `open` and `close`
    indexed by tz-aware UTC) to skip the market-data fetch — used by
    tests and by the orchestrator when it already loaded the data for
    the strategy run.
    """
    if initial_capital <= 0:
        raise ValueError(f"initial_capital must be > 0; got {initial_capital}")
    if commission_pct < 0 or commission_pct >= 1:
        raise ValueError(f"commission_pct must be in [0, 1); got {commission_pct}")
    if slippage_pct < 0 or slippage_pct >= 1:
        raise ValueError(f"slippage_pct must be in [0, 1); got {slippage_pct}")

    df = (
        ohlcv
        if ohlcv is not None
        else get_market_data(
            symbol,
            timeframe.value,
            start,
            end,
            data_dir=data_dir,
        )
    )
    if len(df) < 2:
        raise ValueError(
            f"buy-and-hold needs >=2 bars; got {len(df)} for "
            f"{symbol} {timeframe.value} in [{start}, {end})",
        )

    opens = df["open"].astype("float64")
    closes = df["close"].astype("float64")

    entry_price = float(opens.iloc[0]) * (1.0 + slippage_pct)
    if entry_price <= 0:
        raise ValueError(f"non-positive entry price ({entry_price}) for {symbol}")
    cash_in = initial_capital * (1.0 - commission_pct)
    shares = cash_in / entry_price

    exit_price = float(closes.iloc[-1]) * (1.0 - slippage_pct)
    final_value = shares * exit_price * (1.0 - commission_pct)
    total_return_pct = final_value / initial_capital - 1.0

    equity_arr = (shares * closes).to_numpy(dtype="float64")

    ts_first = pd.Timestamp(df.index[0])  # type: ignore[arg-type]
    ts_last = pd.Timestamp(df.index[-1])  # type: ignore[arg-type]
    years = max((ts_last - ts_first).total_seconds() / (365.25 * 86400.0), 1e-9)
    cagr = (final_value / initial_capital) ** (1.0 / years) - 1.0 if final_value > 0 else -1.0

    running_max = np.maximum.accumulate(equity_arr)
    drawdown = (equity_arr - running_max) / running_max
    max_dd = float(-drawdown.min()) if drawdown.size else 0.0

    bpy = _bars_per_year(timeframe)
    rets_arr = np.diff(equity_arr) / equity_arr[:-1]
    if rets_arr.size >= 2 and float(np.std(rets_arr, ddof=1)) > 0:
        mean_r = float(np.mean(rets_arr))
        std_r = float(np.std(rets_arr, ddof=1))
        sharpe = (mean_r * bpy) / (std_r * math.sqrt(bpy))
    else:
        sharpe = 0.0

    curve = [
        BenchmarkEquityPoint(
            timestamp=pd.Timestamp(ts).to_pydatetime(),  # type: ignore[arg-type]
            value=float(v),
        )
        for ts, v in zip(df.index, equity_arr, strict=True)
    ]

    return BenchmarkResult(
        total_return_pct=total_return_pct,
        cagr=cagr,
        max_drawdown_pct=max_dd,
        sharpe_ratio=sharpe,
        final_value=final_value,
        initial_value=initial_capital,
        equity_curve=curve,
    )


def compare_to_benchmark(
    strategy_metrics: BacktestMetrics,
    benchmark: BenchmarkResult,
) -> BenchmarkComparison:
    """Produce alpha + Sharpe deltas + an honest plain-English verdict."""
    s_ret = strategy_metrics.total_return_pct
    b_ret = benchmark.total_return_pct
    alpha = s_ret - b_ret
    s_sharpe = strategy_metrics.sharpe_ratio
    b_sharpe = benchmark.sharpe_ratio
    risk_alpha = s_sharpe - b_sharpe
    verdict = _build_verdict(
        s_ret=s_ret,
        b_ret=b_ret,
        alpha=alpha,
        s_sharpe=s_sharpe,
        b_sharpe=b_sharpe,
        s_dd=strategy_metrics.max_drawdown_pct,
        b_dd=benchmark.max_drawdown_pct,
        num_trades=strategy_metrics.num_trades,
    )
    return BenchmarkComparison(
        strategy_return_pct=s_ret,
        benchmark_return_pct=b_ret,
        alpha_pct=alpha,
        beat_benchmark=s_ret > b_ret,
        strategy_sharpe=s_sharpe,
        benchmark_sharpe=b_sharpe,
        risk_adjusted_alpha=risk_alpha,
        verdict=verdict,
    )


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _fmt_abs_pct(x: float) -> str:
    return f"{abs(x) * 100:.2f}%"


def _build_verdict(
    *,
    s_ret: float,
    b_ret: float,
    alpha: float,
    s_sharpe: float,
    b_sharpe: float,
    s_dd: float,
    b_dd: float,
    num_trades: int,
) -> str:
    """Plain-English headline. Honest about underperformance — that is
    the entire point of the benchmark.
    """
    if num_trades == 0:
        return (
            f"The strategy fired no trades over the test window, so it returned 0.00% "
            f"while buy-and-hold returned {_fmt_pct(b_ret)}. No edge can be inferred — "
            f"the rules never triggered."
        )

    parts: list[str] = []
    if abs(alpha) < 0.005:
        parts.append(
            f"The strategy roughly matched buy-and-hold ({_fmt_pct(s_ret)} vs "
            f"{_fmt_pct(b_ret)}; alpha {_fmt_pct(alpha)}).",
        )
    else:
        direction = "outperformed" if alpha > 0 else "underperformed"
        parts.append(
            f"The strategy {direction} buy-and-hold by {_fmt_abs_pct(alpha)} "
            f"({_fmt_pct(s_ret)} vs {_fmt_pct(b_ret)}).",
        )
        if alpha < 0:
            parts.append(
                "On total return, a passive hold would have beaten this strategy on "
                "this data window — that is the honest result.",
            )

    if abs(s_sharpe) + abs(b_sharpe) > 0:
        if s_sharpe > b_sharpe + 0.05:
            parts.append(
                f"Risk-adjusted, the strategy was better: Sharpe {s_sharpe:.2f} vs {b_sharpe:.2f}.",
            )
        elif s_sharpe + 0.05 < b_sharpe:
            parts.append(
                f"Risk-adjusted, buy-and-hold was better too: Sharpe {b_sharpe:.2f} "
                f"vs {s_sharpe:.2f}.",
            )
        else:
            parts.append(
                f"Risk-adjusted returns were similar (Sharpe {s_sharpe:.2f} vs {b_sharpe:.2f}).",
            )

    if s_dd + 0.02 < b_dd:
        parts.append(
            f"The strategy did avoid {_fmt_abs_pct(b_dd - s_dd)} of the buy-and-hold "
            f"drawdown ({_fmt_abs_pct(s_dd)} max DD vs {_fmt_abs_pct(b_dd)}).",
        )
    elif s_dd > b_dd + 0.02:
        parts.append(
            f"The strategy's worst drawdown ({_fmt_abs_pct(s_dd)}) was deeper than "
            f"buy-and-hold's ({_fmt_abs_pct(b_dd)}).",
        )

    return " ".join(parts)


__all__ = ["compare_to_benchmark", "compute_buy_and_hold"]
