"""Tests for the Monte Carlo permutation test.

We stub the market-data fetch with a hand-built OHLCV frame, then
stub `run_backtest` + `compute_metrics` to return scripted returns
depending on whether the close-price series is the original or a
permutation. This exercises the orchestration + statistics without
firing the real engine.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import (
    BacktestMeta,
    BacktestMetrics,
    BacktestRun,
    StrategySpec,
)
from marketmind_shared.schemas.strategy_spec.common import Direction, Timeframe
from marketmind_workers.overfitting import monte_carlo as mc_module
from marketmind_workers.overfitting.monte_carlo import run_monte_carlo


def _spec() -> StrategySpec:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "strategies"
        / "valid"
        / "01_golden_cross.json"
    )
    return StrategySpec.model_validate(json.loads(fixture.read_text()))


def _ohlcv(n: int = 365) -> pd.DataFrame:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(n)])
    rng = np.random.default_rng(123)
    # Trending up with daily noise — provides a non-trivial return series.
    drift = np.linspace(100.0, 200.0, num=n)
    noise = rng.normal(0, 2.0, size=n)
    closes = drift + noise
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _meta(start: datetime, end: datetime) -> BacktestMeta:
    return BacktestMeta(
        symbol="BTC/USDT",
        primary_timeframe=Timeframe.D1,
        filter_timeframe=None,
        start=start,
        end=end,
        initial_capital=10_000.0,
        direction=Direction.LONG,
        defaulted_costs=True,
        defaulted_position_sizing=True,
    )


def _metrics(return_pct: float, sharpe: float = 0.5) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=return_pct,
        cagr=return_pct,
        annualized_volatility=0.2,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe,
        max_drawdown_pct=0.1,
        max_drawdown_duration_days=5,
        calmar_ratio=0.0,
        num_trades=5,
        win_rate=0.5,
        profit_factor=1.2,
        profit_factor_capped=False,
        avg_win_pct=0.05,
        avg_loss_pct=-0.03,
        expectancy=0.01,
        largest_win_pct=0.1,
        largest_loss_pct=-0.07,
        longest_winning_streak=1,
        longest_losing_streak=1,
        avg_trade_duration_days=1.0,
        exposure_pct=0.5,
        bars_processed=10,
        bars_per_year=365.0,
    )


# ---- Tests ---------------------------------------------------------------


def test_real_edge_yields_small_p_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strategy returns +50% on the real data and -5% on every
    permutation → p_value should be 0 (or very close).
    """
    real_data = {Timeframe.D1: _ohlcv()}
    monkeypatch.setattr(mc_module, "get_market_data", lambda *_, **__: _ohlcv())

    def fake_run(
        spec: StrategySpec,
        start: datetime,
        end: datetime,
        _ic: float,
        *,
        data_override: dict[Timeframe, pd.DataFrame] | None = None,
        **_: object,
    ) -> BacktestRun:
        # data_override is real_data on the first call (the baseline)
        # because we pass it explicitly. Detect "real" by close-array
        # equality.
        is_real = data_override is not None and np.array_equal(
            data_override[Timeframe.D1]["close"].to_numpy(),
            real_data[Timeframe.D1]["close"].to_numpy(),
        )
        fake_run.is_real = is_real  # type: ignore[attr-defined]
        return BacktestRun(spec_name="t", meta=_meta(start, end), equity_curve=[], trades=[])

    monkeypatch.setattr(mc_module, "run_backtest", fake_run)

    def fake_metrics(_run: BacktestRun, _tf: Timeframe) -> BacktestMetrics:
        return _metrics(0.50 if fake_run.is_real else -0.05)  # type: ignore[attr-defined]

    monkeypatch.setattr(mc_module, "compute_metrics", fake_metrics)

    out = run_monte_carlo(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        n_permutations=20,
        seed=42,
    )
    assert out.real_return_pct == pytest.approx(0.50)
    assert out.p_value == 0.0  # real beats every permutation
    assert out.percentile_rank == 1.0


def test_no_edge_yields_high_p_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both real and synth runs return ~the same → p_value ~ 0.5."""
    monkeypatch.setattr(mc_module, "get_market_data", lambda *_, **__: _ohlcv())

    def fake_run(
        spec: StrategySpec,
        start: datetime,
        end: datetime,
        _ic: float,
        **_: object,
    ) -> BacktestRun:
        return BacktestRun(spec_name="t", meta=_meta(start, end), equity_curve=[], trades=[])

    rng = np.random.default_rng(7)
    call_count = [0]

    def fake_metrics(_run: BacktestRun, _tf: Timeframe) -> BacktestMetrics:
        call_count[0] += 1
        if call_count[0] == 1:
            return _metrics(0.0)  # real
        # Random synth returns around zero — sometimes above, sometimes below.
        return _metrics(float(rng.normal(0.0, 0.05)))

    monkeypatch.setattr(mc_module, "run_backtest", fake_run)
    monkeypatch.setattr(mc_module, "compute_metrics", fake_metrics)

    out = run_monte_carlo(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        n_permutations=30,
        seed=42,
    )
    # Real is near the centre of the distribution -> p in [0.30, 0.70]
    assert 0.30 <= out.p_value <= 0.70


def test_seed_reproducibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc_module, "get_market_data", lambda *_, **__: _ohlcv())

    captured_first_shuffles: list[float] = []

    def fake_run(
        spec: StrategySpec,
        start: datetime,
        end: datetime,
        _ic: float,
        *,
        data_override: dict[Timeframe, pd.DataFrame] | None = None,
        **_: object,
    ) -> BacktestRun:
        # Capture the first close of the first permutation only.
        if data_override is not None and len(captured_first_shuffles) < 3:
            captured_first_shuffles.append(
                float(data_override[Timeframe.D1]["close"].to_numpy()[5]),
            )
        return BacktestRun(spec_name="t", meta=_meta(start, end), equity_curve=[], trades=[])

    monkeypatch.setattr(mc_module, "run_backtest", fake_run)
    monkeypatch.setattr(mc_module, "compute_metrics", lambda _r, _tf: _metrics(0.1))

    # Two runs with same seed -> same first-permutation close.
    captured_first_shuffles.clear()
    _ = run_monte_carlo(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 6, 1, tzinfo=UTC),
        n_permutations=3,
        seed=999,
    )
    first_a = list(captured_first_shuffles)

    captured_first_shuffles.clear()
    _ = run_monte_carlo(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 6, 1, tzinfo=UTC),
        n_permutations=3,
        seed=999,
    )
    first_b = list(captured_first_shuffles)
    assert first_a == first_b
    assert len(first_a) > 0


def test_rejects_too_few_permutations() -> None:
    with pytest.raises(ValueError, match="n_permutations"):
        run_monte_carlo(
            _spec(),
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 12, 31, tzinfo=UTC),
            n_permutations=1,
        )
