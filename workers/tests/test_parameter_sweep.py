"""Tests for the parameter sweep.

Most tests stub out the engine and metrics so we exercise the
axis-detection, grid-pruning, mutation, and peakiness math without
firing real backtests. One end-to-end test builds a synthetic
"baseline is a peak" engine that returns a different value at the
baseline cell vs neighbors, and asserts the peakiness math catches it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from marketmind_shared.schemas import (
    BacktestMeta,
    BacktestMetrics,
    BacktestRun,
    StrategySpec,
    SweepAxisKind,
)
from marketmind_shared.schemas.strategy_spec.common import Direction, Timeframe
from marketmind_workers.overfitting import parameter_sweep as ps_module
from marketmind_workers.overfitting.parameter_sweep import run_parameter_sweep


def _spec(filename: str = "01_golden_cross.json") -> StrategySpec:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "strategies"
        / "valid"
        / filename
    )
    return StrategySpec.model_validate(json.loads(fixture.read_text()))


def _stub_run(
    spec: StrategySpec, start: datetime, end: datetime, *_: object, **__: object
) -> BacktestRun:
    return BacktestRun(
        spec_name=spec.name,
        meta=BacktestMeta(
            symbol=spec.instrument.symbol,
            primary_timeframe=Timeframe.D1,
            filter_timeframe=None,
            start=start,
            end=end,
            initial_capital=10_000.0,
            direction=Direction.LONG,
            defaulted_costs=True,
            defaulted_position_sizing=True,
        ),
        equity_curve=[],
        trades=[],
    )


def _metrics(return_pct: float) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=return_pct,
        cagr=return_pct,
        annualized_volatility=0.2,
        sharpe_ratio=return_pct,  # use return as Sharpe for simplicity
        sortino_ratio=return_pct,
        max_drawdown_pct=0.1,
        max_drawdown_duration_days=10,
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


# ---- Axis detection ------------------------------------------------------


def test_detects_two_indicator_period_axes_for_golden_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)
    monkeypatch.setattr(ps_module, "compute_metrics", lambda _r, _tf: _metrics(0.10))

    out = run_parameter_sweep(
        _spec("01_golden_cross.json"),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=200,
    )
    # Golden Cross fixture has SMA(50) + SMA(200) → two indicator
    # period axes, no stop_loss.
    kinds = sorted(a.kind for a in out.axes)
    assert kinds.count(SweepAxisKind.INDICATOR_PERIOD) == 2


def test_detects_stop_loss_axis_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture 02 (RSI mean reversion) has a percent stop_loss → must be detected."""
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)
    monkeypatch.setattr(ps_module, "compute_metrics", lambda _r, _tf: _metrics(0.10))

    out = run_parameter_sweep(
        _spec("02_rsi_mean_reversion.json"),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=200,
    )
    kinds = sorted(a.kind for a in out.axes)
    assert SweepAxisKind.STOP_LOSS_PCT in kinds


# ---- Grid budget ---------------------------------------------------------


def test_grid_prunes_to_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """With max_cells=10, the 5*5*5 = 125 grid must drop axes."""
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)
    monkeypatch.setattr(ps_module, "compute_metrics", lambda _r, _tf: _metrics(0.10))

    out = run_parameter_sweep(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=10,
    )
    # 10-cell budget -> 5 stop_loss values * 1 axis only (no room for second).
    # Could also be 2 axes of 2-3 values each. Either way: <= 10.
    assert out.n_combinations <= 10
    assert out.skipped_reason is not None


# ---- Robust strategy → flat peakiness -----------------------------------


def test_robust_strategy_has_low_peakiness(monkeypatch: pytest.MonkeyPatch) -> None:
    """All cells return ≈ 0.50 → baseline is on a flat plateau → peakiness ≈ 0."""
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)
    monkeypatch.setattr(ps_module, "compute_metrics", lambda _r, _tf: _metrics(0.50))

    out = run_parameter_sweep(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=50,
    )
    assert out.n_combinations > 0
    assert out.peakiness_score < 0.05
    assert out.best_in_grid_return == pytest.approx(0.50)
    assert out.worst_in_grid_return == pytest.approx(0.50)


# ---- Overfit strategy → high peakiness ----------------------------------


def test_overfit_strategy_has_high_peakiness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline cell returns 1.00, every other cell returns 0.10 → peakiness ≈ 0.9."""
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)

    # We need to detect baseline-ness from the SPEC the engine sees. The
    # spec mutator changes values on each cell — the baseline cell is the
    # one where the spec equals the original. We diff against the original
    # spec dump.
    orig_dump = json.loads(_spec().model_dump_json())

    def metrics_by_spec_state(run: BacktestRun, _tf: Timeframe) -> BacktestMetrics:
        # `run.spec_name` matches both baseline and neighbors (we don't
        # rename). The real distinguisher: peek inside the latest spec
        # via a side-channel. We use `run.meta` start/end which are equal
        # across cells — so that doesn't work. Instead we proxy via the
        # MUTATING module's recently-validated spec, captured below.
        latest_spec = latest_holder[0]
        latest_dump = json.loads(latest_spec.model_dump_json())
        if latest_dump == orig_dump:
            return _metrics(1.00)
        return _metrics(0.10)

    latest_holder: list[StrategySpec] = []

    original_validate = StrategySpec.model_validate

    def capture_validate(payload: object) -> StrategySpec:
        s = original_validate(payload)
        latest_holder.clear()
        latest_holder.append(s)
        return s

    monkeypatch.setattr(StrategySpec, "model_validate", capture_validate)
    monkeypatch.setattr(ps_module, "compute_metrics", metrics_by_spec_state)

    out = run_parameter_sweep(
        _spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=50,
    )
    assert out.n_combinations > 5
    assert out.baseline_return_pct == pytest.approx(1.00)
    # Baseline far above neighbors -> peakiness near 1.
    assert out.peakiness_score > 0.7


# ---- Bad arg guard -------------------------------------------------------


def test_rejects_zero_max_cells() -> None:
    with pytest.raises(ValueError, match="max_cells"):
        run_parameter_sweep(
            _spec(),
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 12, 31, tzinfo=UTC),
            max_cells=0,
        )


# ---- A.4 §5.1: stateful conditions inherit sweepability ------------------


def test_detect_axes_finds_indicator_periods_inside_regime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1 verification: `_detect_axes` is a blind recursive walk of the
    whole spec dict, so an indicator period inside a regime_state's
    enter_when / exit_when triggers is swept like any other — a T2
    stateful condition inherits parameter-sweepability for free, with no
    parameter-sweep code change for A.4.
    """
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)
    monkeypatch.setattr(ps_module, "compute_metrics", lambda _r, _tf: _metrics(0.10))

    out = run_parameter_sweep(
        _spec("09_regime_state_supertrend.json"),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=500,
    )
    baselines = {
        a.baseline_value for a in out.axes if a.kind is SweepAxisKind.INDICATOR_PERIOD
    }
    # EMA(200) lives ONLY inside the regime_state's enter/exit triggers —
    # its detection proves the walk recurses into the v2 condition.
    assert 200.0 in baselines
    # EMA(20)/EMA(50) (the sibling crossover) are detected too.
    assert {20.0, 50.0} <= baselines


def test_detect_axes_finds_indicator_period_inside_ratchet_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1 verification: the same blind walk reaches an indicator nested
    inside a ratchet.source — fixture 10 re-rooted on an EMA(30).
    """
    monkeypatch.setattr(ps_module, "run_backtest", _stub_run)
    monkeypatch.setattr(ps_module, "compute_metrics", lambda _r, _tf: _metrics(0.10))

    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "strategies"
        / "valid"
        / "10_ratchet_trailing.json"
    )
    spec_dict = json.loads(fixture.read_text())
    # Re-root the trailing-exit ratchet on an EMA(30) so an indicator
    # period lives strictly inside the ratchet subtree.
    ratchet = spec_dict["exit"]["exits"][0]["condition"]["right"]["expression"]
    ratchet["source"] = {"kind": "indicator", "name": "ema", "params": {"period": 30}}
    spec = StrategySpec.model_validate(spec_dict)

    out = run_parameter_sweep(
        spec,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 12, 31, tzinfo=UTC),
        max_cells=500,
    )
    baselines = {
        a.baseline_value for a in out.axes if a.kind is SweepAxisKind.INDICATOR_PERIOD
    }
    assert 30.0 in baselines
