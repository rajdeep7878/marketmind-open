"""Hand-verified tests for compute_metrics.

Each test builds a tiny BacktestRun, calls compute_metrics, and
asserts to a numeric tolerance against values computed by hand.
Edge cases covered explicitly: zero trades, all wins, all losses,
single trade, profit-factor saturation.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from marketmind_shared.schemas import (
    BacktestMeta,
    BacktestRun,
    EquityPoint,
    Trade,
)
from marketmind_shared.schemas.strategy_spec.common import Direction, Timeframe
from marketmind_workers.backtest.metrics import bars_per_year, compute_metrics

# ---- Builders --------------------------------------------------------------


def _meta(tf: Timeframe = Timeframe.D1) -> BacktestMeta:
    return BacktestMeta(
        symbol="BTC/USDT",
        primary_timeframe=tf,
        filter_timeframe=None,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 12, 31, tzinfo=UTC),
        initial_capital=10_000.0,
        direction=Direction.LONG,
        defaulted_costs=True,
        defaulted_position_sizing=True,
    )


def _curve(values: list[float], *, step: timedelta = timedelta(days=1)) -> list[EquityPoint]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [EquityPoint(timestamp=start + step * i, value=v) for i, v in enumerate(values)]


def _trade(
    *,
    entry_offset_days: int,
    duration_days: int,
    entry_price: float,
    exit_price: float,
    pnl: float,
    return_pct: float,
) -> Trade:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return Trade(
        entry_time=start + timedelta(days=entry_offset_days),
        exit_time=start + timedelta(days=entry_offset_days + duration_days),
        entry_price=entry_price,
        exit_price=exit_price,
        size=1.0,
        pnl=pnl,
        return_pct=return_pct,
        direction=Direction.LONG,
        exit_reason="signal",
    )


def _run(curve: list[EquityPoint], trades: list[Trade]) -> BacktestRun:
    return BacktestRun(
        spec_name="t",
        meta=_meta(),
        equity_curve=curve,
        trades=trades,
    )


# ---- bars_per_year ---------------------------------------------------------


def test_bars_per_year_daily() -> None:
    assert bars_per_year(Timeframe.D1) == 365.0


def test_bars_per_year_4h_is_6_per_day() -> None:
    # 6 bars/day * 365 days = 2190
    assert bars_per_year(Timeframe.H4) == 2190.0


def test_bars_per_year_1h_is_24_per_day() -> None:
    assert bars_per_year(Timeframe.H1) == 8760.0


# ---- Return / CAGR / volatility / Sharpe / Sortino -----------------------


def test_total_return_simple() -> None:
    run = _run(_curve([10_000, 12_000]), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.total_return_pct == pytest.approx(0.20)


def test_cagr_one_year_flat() -> None:
    # 365 daily points, doubling exactly over 1 year.
    values = [10_000.0 * (1.0 + i / 364.0) for i in range(365)]
    run = _run(_curve(values), [])
    m = compute_metrics(run, Timeframe.D1)
    # final/initial = 2.0; years ≈ 1.0; CAGR ≈ 100%
    assert m.cagr == pytest.approx(1.0, abs=0.05)


def test_volatility_zero_for_constant_curve() -> None:
    run = _run(_curve([10_000.0] * 100), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.annualized_volatility == pytest.approx(0.0)
    assert m.sharpe_ratio == pytest.approx(0.0)


def test_sharpe_positive_for_uptrend() -> None:
    # Smooth uptrend has low vol -> high Sharpe (positive).
    values = [10_000.0 * (1.0 + 0.001 * i) for i in range(365)]
    run = _run(_curve(values), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.sharpe_ratio > 1.0


def test_sortino_higher_than_sharpe_when_mostly_upside() -> None:
    # A curve with mostly-up steps and a few small drawdowns: total
    # volatility is bigger than downside-only volatility, so Sortino
    # should come out higher than Sharpe.
    values: list[float] = [10_000.0]
    for i in range(1, 200):
        # 90% of bars up 1%, 10% down 0.5% — asymmetric vol.
        bump = 1.01 if i % 10 != 0 else 0.995
        values.append(values[-1] * bump)
    run = _run(_curve(values), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.sortino_ratio > m.sharpe_ratio


# ---- Drawdown -------------------------------------------------------------


def test_max_drawdown_zero_for_monotonic_curve() -> None:
    run = _run(_curve([10_000.0 + i * 100 for i in range(50)]), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.max_drawdown_pct == pytest.approx(0.0)
    assert m.max_drawdown_duration_days == 0


def test_max_drawdown_basic_pattern() -> None:
    # Peak 100, trough 80, recover to 110 -> max DD = 20%
    run = _run(_curve([100, 100, 100, 80, 90, 110, 110]), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.max_drawdown_pct == pytest.approx(0.20)


def test_max_drawdown_duration_peak_to_recovery() -> None:
    # Peak on day 2 (idx=2), trough on day 5 (idx=5), recover on day 8.
    # Duration = day 8 - day 2 = 6 days.
    values = [100, 100, 110, 105, 95, 85, 95, 100, 115]
    run = _run(_curve(values), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.max_drawdown_duration_days == 6


def test_max_drawdown_duration_unrecovered() -> None:
    # Peak then drop, never recover before end.
    values = [100, 100, 110, 105, 95, 85]
    run = _run(_curve(values), [])
    m = compute_metrics(run, Timeframe.D1)
    # Peak at idx 2, end at idx 5. Duration = 3 days.
    assert m.max_drawdown_duration_days == 3


def test_calmar_ratio_zero_when_no_drawdown() -> None:
    run = _run(_curve([10_000.0 + i * 100 for i in range(50)]), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.calmar_ratio == pytest.approx(0.0)


# ---- Trade stats ----------------------------------------------------------


def test_zero_trades_returns_zeros() -> None:
    run = _run(_curve([10_000.0, 10_000.0]), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.num_trades == 0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.profit_factor_capped is False
    assert m.avg_win_pct == 0.0
    assert m.avg_loss_pct == 0.0
    assert m.expectancy == 0.0
    assert m.longest_winning_streak == 0
    assert m.longest_losing_streak == 0
    assert m.avg_trade_duration_days == 0.0


def test_win_rate_and_avg_returns() -> None:
    trades = [
        _trade(
            entry_offset_days=0,
            duration_days=5,
            entry_price=100,
            exit_price=110,
            pnl=100,
            return_pct=0.10,
        ),  # win
        _trade(
            entry_offset_days=10,
            duration_days=5,
            entry_price=110,
            exit_price=99,
            pnl=-110,
            return_pct=-0.10,
        ),  # loss
        _trade(
            entry_offset_days=20,
            duration_days=10,
            entry_price=99,
            exit_price=119,
            pnl=200,
            return_pct=0.20,
        ),  # win
    ]
    run = _run(_curve([10_000.0, 10_100.0, 9_990.0, 10_190.0]), trades)
    m = compute_metrics(run, Timeframe.D1)
    assert m.num_trades == 3
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.avg_win_pct == pytest.approx(0.15)
    assert m.avg_loss_pct == pytest.approx(-0.10)
    assert m.expectancy == pytest.approx((0.10 - 0.10 + 0.20) / 3)
    assert m.largest_win_pct == pytest.approx(0.20)
    assert m.largest_loss_pct == pytest.approx(-0.10)


def test_profit_factor_basic() -> None:
    trades = [
        _trade(
            entry_offset_days=0,
            duration_days=1,
            entry_price=1,
            exit_price=2,
            pnl=300,
            return_pct=0.30,
        ),
        _trade(
            entry_offset_days=2,
            duration_days=1,
            entry_price=1,
            exit_price=1,
            pnl=-100,
            return_pct=-0.10,
        ),
        _trade(
            entry_offset_days=4,
            duration_days=1,
            entry_price=1,
            exit_price=2,
            pnl=200,
            return_pct=0.20,
        ),
    ]
    run = _run(_curve([10_000.0, 10_400.0]), trades)
    m = compute_metrics(run, Timeframe.D1)
    # Gross profit = 300 + 200 = 500. Gross loss = 100. PF = 5.0
    assert m.profit_factor == pytest.approx(5.0)
    assert m.profit_factor_capped is False


def test_profit_factor_saturates_when_no_losses() -> None:
    trades = [
        _trade(
            entry_offset_days=0,
            duration_days=1,
            entry_price=1,
            exit_price=2,
            pnl=100,
            return_pct=0.10,
        ),
        _trade(
            entry_offset_days=2,
            duration_days=1,
            entry_price=1,
            exit_price=2,
            pnl=200,
            return_pct=0.20,
        ),
    ]
    run = _run(_curve([10_000.0, 10_300.0]), trades)
    m = compute_metrics(run, Timeframe.D1)
    assert m.profit_factor == pytest.approx(1.0e6)
    assert m.profit_factor_capped is True


def test_single_losing_trade() -> None:
    trades = [
        _trade(
            entry_offset_days=0,
            duration_days=2,
            entry_price=100,
            exit_price=80,
            pnl=-200,
            return_pct=-0.20,
        ),
    ]
    run = _run(_curve([10_000.0, 9_800.0]), trades)
    m = compute_metrics(run, Timeframe.D1)
    assert m.num_trades == 1
    assert m.win_rate == 0.0
    # Zero profit, non-zero loss -> profit_factor = 0.0
    assert m.profit_factor == pytest.approx(0.0)
    assert m.profit_factor_capped is False
    assert m.longest_winning_streak == 0
    assert m.longest_losing_streak == 1


def test_streaks() -> None:
    # W W L W L L L W W W L  -> longest_win=3, longest_loss=3
    pattern = [0.1, 0.1, -0.1, 0.1, -0.1, -0.1, -0.1, 0.1, 0.1, 0.1, -0.1]
    trades = [
        _trade(
            entry_offset_days=i * 2,
            duration_days=1,
            entry_price=1,
            exit_price=2 if r > 0 else 1,
            pnl=100 if r > 0 else -100,
            return_pct=r,
        )
        for i, r in enumerate(pattern)
    ]
    run = _run(_curve([10_000.0, 10_100.0]), trades)
    m = compute_metrics(run, Timeframe.D1)
    assert m.longest_winning_streak == 3
    assert m.longest_losing_streak == 3


def test_exposure_pct_fraction_of_time_in_position() -> None:
    # Run from day 0 to day 100; one 25-day trade -> exposure = 0.25.
    curve = [
        EquityPoint(timestamp=datetime(2024, 1, 1, tzinfo=UTC), value=10_000.0),
        EquityPoint(
            timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=100),
            value=10_000.0,
        ),
    ]
    trades = [
        _trade(
            entry_offset_days=10,
            duration_days=25,
            entry_price=100,
            exit_price=110,
            pnl=100,
            return_pct=0.10,
        ),
    ]
    run = BacktestRun(spec_name="t", meta=_meta(), equity_curve=curve, trades=trades)
    m = compute_metrics(run, Timeframe.D1)
    assert m.exposure_pct == pytest.approx(0.25)


# ---- Annualization is timeframe-correct ----------------------------------


def test_sharpe_uses_per_timeframe_annualization() -> None:
    """The same per-bar return series must yield a HIGHER annualised
    Sharpe at a higher-frequency timeframe (more bars per year -> more
    annualisation).
    """
    # Build a series with the same per-bar return shape, but tag it
    # as either 4h or 1d. Total length must match a year's worth at
    # the LOWER frequency so both runs have comparable bar counts.
    n = 200
    values = [10_000.0 * (1.0 + 0.0005 * i) for i in range(n)]
    run_4h = BacktestRun(
        spec_name="t",
        meta=_meta(Timeframe.H4),
        equity_curve=[
            EquityPoint(
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=4 * i),
                value=v,
            )
            for i, v in enumerate(values)
        ],
        trades=[],
    )
    run_d1 = BacktestRun(
        spec_name="t",
        meta=_meta(Timeframe.D1),
        equity_curve=[
            EquityPoint(
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i),
                value=v,
            )
            for i, v in enumerate(values)
        ],
        trades=[],
    )
    m4 = compute_metrics(run_4h, Timeframe.H4)
    md = compute_metrics(run_d1, Timeframe.D1)
    # Same per-bar return shape; same Sharpe formula. 4h has 6x more
    # bars per year, so sqrt(6) ≈ 2.45x annualisation factor on the
    # vol denominator BUT also 6x on the mean numerator: net 6/sqrt(6)
    # = sqrt(6) ≈ 2.45x higher annualised Sharpe.
    assert math.isclose(m4.sharpe_ratio / md.sharpe_ratio, math.sqrt(6), rel_tol=0.05)


def test_bars_processed_equals_curve_length() -> None:
    run = _run(_curve([10_000.0] * 42), [])
    m = compute_metrics(run, Timeframe.D1)
    assert m.bars_processed == 42
