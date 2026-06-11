"""Hand-verified tests for the buy-and-hold benchmark + comparison.

We feed compute_buy_and_hold a fabricated OHLCV DataFrame so the tests
never touch the network. The math is small enough that every expected
return is checked against a closed-form value.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import BacktestMetrics
from marketmind_shared.schemas.strategy_spec.common import Timeframe
from marketmind_workers.backtest.benchmark import (
    compare_to_benchmark,
    compute_buy_and_hold,
)

# ---- Builders --------------------------------------------------------------


def _ohlcv(prices: list[float]) -> pd.DataFrame:
    """OHLCV frame whose open and close both equal `prices[i]`."""
    idx = pd.DatetimeIndex(
        [datetime(2024, 1, 1, tzinfo=UTC) + pd.Timedelta(days=i) for i in range(len(prices))],
    )
    arr = np.array(prices, dtype="float64")
    return pd.DataFrame(
        {
            "open": arr,
            "high": arr * 1.001,
            "low": arr * 0.999,
            "close": arr,
            "volume": np.full(len(prices), 1000.0),
        },
        index=idx,
    )


def _metrics(
    *,
    total_return_pct: float,
    sharpe: float = 0.0,
    max_dd: float = 0.0,
    num_trades: int = 5,
) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr=total_return_pct,
        annualized_volatility=0.2,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe * 1.1,
        max_drawdown_pct=max_dd,
        max_drawdown_duration_days=10,
        calmar_ratio=0.0 if max_dd == 0 else total_return_pct / max_dd,
        num_trades=num_trades,
        win_rate=0.5,
        profit_factor=1.2,
        profit_factor_capped=False,
        avg_win_pct=0.05,
        avg_loss_pct=-0.03,
        expectancy=0.01,
        largest_win_pct=0.1,
        largest_loss_pct=-0.08,
        longest_winning_streak=3,
        longest_losing_streak=2,
        avg_trade_duration_days=4.0,
        exposure_pct=0.5,
        bars_processed=365,
        bars_per_year=365.0,
    )


# ---- compute_buy_and_hold -------------------------------------------------


def test_flat_price_returns_negative_to_cover_round_trip_fees() -> None:
    df = _ohlcv([100.0] * 30)
    out = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
        initial_capital=10_000.0,
        ohlcv=df,
    )
    # Flat price + entry fee + slippage + exit fee + slippage means a
    # small negative total_return (the chart curve sits below initial).
    assert out.total_return_pct < 0
    assert out.max_drawdown_pct < 0.01
    assert out.equity_curve[0].value == pytest.approx(
        out.initial_value * (1.0 - 0.001) * (1.0 / (1.0 + 0.0005)),
        rel=1e-6,
    )


def test_doubling_price_doubles_capital_minus_fees() -> None:
    df = _ohlcv([100.0 + i for i in range(101)])  # 100 → 200
    out = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 5, 1, tzinfo=UTC),
        initial_capital=10_000.0,
        ohlcv=df,
    )
    # ~2x minus fees + slippage ≈ 1.997 → ~99.7% total return
    assert 0.99 < out.total_return_pct < 1.0
    assert out.final_value > out.initial_value
    assert out.max_drawdown_pct == pytest.approx(0.0, abs=1e-9)


def test_drawdown_captures_peak_to_trough() -> None:
    # Peak 200, trough 80 → max DD 60%
    df = _ohlcv([100, 150, 200, 180, 120, 80, 90, 100])
    out = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 9, tzinfo=UTC),
        initial_capital=10_000.0,
        ohlcv=df,
    )
    assert out.max_drawdown_pct == pytest.approx(0.60, abs=0.01)


def test_equity_curve_length_matches_bar_count() -> None:
    df = _ohlcv([100.0 * (1 + i * 0.01) for i in range(50)])
    out = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 20, tzinfo=UTC),
        initial_capital=10_000.0,
        ohlcv=df,
    )
    assert len(out.equity_curve) == 50


def test_rejects_fewer_than_two_bars() -> None:
    df = _ohlcv([100.0])
    with pytest.raises(ValueError, match=">=2 bars"):
        compute_buy_and_hold(
            "BTC/USDT",
            Timeframe.D1,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
            ohlcv=df,
        )


def test_rejects_non_positive_initial_capital() -> None:
    df = _ohlcv([100.0, 110.0])
    with pytest.raises(ValueError, match="initial_capital"):
        compute_buy_and_hold(
            "BTC/USDT",
            Timeframe.D1,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
            initial_capital=0.0,
            ohlcv=df,
        )


def test_zero_commission_and_slippage_recovers_clean_return() -> None:
    df = _ohlcv([100.0, 120.0])
    out = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        initial_capital=10_000.0,
        commission_pct=0.0,
        slippage_pct=0.0,
        ohlcv=df,
    )
    assert out.total_return_pct == pytest.approx(0.20, rel=1e-9)
    assert out.final_value == pytest.approx(12_000.0, rel=1e-9)


# ---- compare_to_benchmark -------------------------------------------------


def test_compare_strategy_beats_benchmark() -> None:
    df = _ohlcv([100.0, 105.0, 110.0])
    bench = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 4, tzinfo=UTC),
        ohlcv=df,
    )
    m = _metrics(total_return_pct=0.50, sharpe=1.5)
    cmp = compare_to_benchmark(m, bench)
    assert cmp.beat_benchmark is True
    assert cmp.alpha_pct > 0
    assert "outperformed" in cmp.verdict.lower()


def test_compare_strategy_loses_to_benchmark_is_honest() -> None:
    df = _ohlcv([100.0 * (1 + i * 0.005) for i in range(50)])  # ~+25%
    bench = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 20, tzinfo=UTC),
        ohlcv=df,
    )
    m = _metrics(total_return_pct=0.05, sharpe=0.3)
    cmp = compare_to_benchmark(m, bench)
    assert cmp.beat_benchmark is False
    assert cmp.alpha_pct < 0
    v = cmp.verdict.lower()
    assert "underperformed" in v
    assert "honest" in v  # the verdict must admit the loss explicitly


def test_compare_zero_trades_calls_it_out() -> None:
    df = _ohlcv([100.0, 110.0, 120.0])
    bench = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 4, tzinfo=UTC),
        ohlcv=df,
    )
    m = _metrics(total_return_pct=0.0, num_trades=0, sharpe=0.0)
    cmp = compare_to_benchmark(m, bench)
    assert "no trades" in cmp.verdict.lower()
    assert cmp.beat_benchmark is False


def test_compare_near_zero_alpha_says_roughly_matched() -> None:
    df = _ohlcv([100.0, 101.0])
    bench = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        ohlcv=df,
    )
    m = _metrics(total_return_pct=bench.total_return_pct, sharpe=bench.sharpe_ratio)
    cmp = compare_to_benchmark(m, bench)
    assert "roughly matched" in cmp.verdict.lower()


def test_compare_calls_out_deeper_strategy_drawdown() -> None:
    df = _ohlcv([100.0, 110.0, 105.0])
    bench = compute_buy_and_hold(
        "BTC/USDT",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 4, tzinfo=UTC),
        ohlcv=df,
    )
    m = _metrics(total_return_pct=0.10, sharpe=0.5, max_dd=0.30)
    cmp = compare_to_benchmark(m, bench)
    assert "deeper" in cmp.verdict.lower()
