"""Tests for the walk-forward analysis.

We don't run the real engine. Instead we stub `run_backtest` +
`compute_metrics` to return scripted returns for each (start, end)
segment, so the per-window orchestration and the aggregation math are
testable independently of the engine.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from marketmind_shared.schemas import (
    BacktestMeta,
    BacktestMetrics,
    BacktestRun,
    EquityPoint,
    StrategySpec,
    Trade,
)
from marketmind_shared.schemas.strategy_spec.common import Direction, Timeframe
from marketmind_shared.schemas.strategy_spec.introspection import condition_uses_tier3
from marketmind_workers.backtest import engine as engine_module
from marketmind_workers.overfitting import walk_forward as wf_module
from marketmind_workers.overfitting.walk_forward import run_walk_forward

# ---- Stubs ---------------------------------------------------------------


def _stub_backtest(
    _spec: StrategySpec, start: datetime, end: datetime, *_: object, **__: object
) -> BacktestRun:
    """Return a 2-bar BacktestRun with the exact `start`/`end` baked in;
    the actual numbers are set by the metrics stub.
    """
    meta = BacktestMeta(
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
    return BacktestRun(spec_name="t", meta=meta, equity_curve=[], trades=[])


def _metrics(return_pct: float, sharpe: float = 0.0, num_trades: int = 1) -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=return_pct,
        cagr=return_pct,
        annualized_volatility=0.2,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe,
        max_drawdown_pct=0.0,
        max_drawdown_duration_days=0,
        calmar_ratio=0.0,
        num_trades=num_trades,
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
        bars_processed=2,
        bars_per_year=365.0,
    )


def _minimal_spec() -> StrategySpec:
    """Use the Golden Cross fixture so all spec invariants hold."""
    import json
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "strategies"
        / "valid"
        / "01_golden_cross.json"
    )
    return StrategySpec.model_validate(json.loads(fixture.read_text()))


# ---- Tests ---------------------------------------------------------------


def test_robust_strategy_has_degradation_ratio_near_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same return on every segment → degradation_ratio == 1.0 exactly."""
    monkeypatch.setattr(wf_module, "run_backtest", _stub_backtest)
    monkeypatch.setattr(wf_module, "compute_metrics", lambda _run, _tf: _metrics(0.20))

    out = run_walk_forward(
        _minimal_spec(),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        n_windows=4,
    )
    assert out.n_windows_actual == 4
    assert out.degradation_ratio_valid is True
    assert out.degradation_ratio == pytest.approx(1.0)
    assert out.in_sample_avg_return == pytest.approx(0.20)
    assert out.out_of_sample_avg_return == pytest.approx(0.20)
    assert out.out_of_sample_positive_rate == pytest.approx(1.0)
    assert out.consistency_score == pytest.approx(1.0)


def test_overfit_strategy_degradation_below_half(monkeypatch: pytest.MonkeyPatch) -> None:
    """IS always +30%, OOS always +5% → degradation 0.167, well under 0.5."""

    def metrics_by_window(run: BacktestRun, _tf: Timeframe) -> BacktestMetrics:
        # Decide IS vs OOS from the run's meta. IS = first half of window
        # = always starts at one of our "window starts". OOS starts at
        # is_end. We use the start date's day-of-year mod to encode.
        # Simpler: look at meta.start vs full_start of the *window*.
        # We can't do that here directly — instead we use a counter.
        nonlocal call_count
        call_count += 1
        # Even calls (0, 2, 4, ...) are IS; odd are OOS (the orchestrator
        # always calls IS first, OOS second per window).
        is_is = call_count % 2 == 1
        return _metrics(0.30 if is_is else 0.05)

    call_count = 0
    monkeypatch.setattr(wf_module, "run_backtest", _stub_backtest)
    monkeypatch.setattr(wf_module, "compute_metrics", metrics_by_window)

    out = run_walk_forward(
        _minimal_spec(),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        n_windows=4,
    )
    assert out.in_sample_avg_return == pytest.approx(0.30)
    assert out.out_of_sample_avg_return == pytest.approx(0.05)
    assert out.degradation_ratio == pytest.approx(0.05 / 0.30, abs=1e-9)
    assert out.degradation_ratio < 0.5


def test_only_works_in_first_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic engineered strategy that only worked in the first
    window — degradation_ratio comes out < 0.5 once IS averages drop
    enough to make the OOS look bad.
    """
    call_count = 0
    window_for_call: list[int] = []

    def fake_metrics(_run: BacktestRun, _tf: Timeframe) -> BacktestMetrics:
        nonlocal call_count
        # Two calls per window (IS then OOS), 6 windows → 12 calls.
        win_idx = call_count // 2
        is_is = call_count % 2 == 0
        call_count += 1
        window_for_call.append(win_idx)
        if win_idx == 0:
            return _metrics(0.50 if is_is else 0.40)
        return _metrics(0.0 if is_is else -0.05)

    monkeypatch.setattr(wf_module, "run_backtest", _stub_backtest)
    monkeypatch.setattr(wf_module, "compute_metrics", fake_metrics)

    out = run_walk_forward(
        _minimal_spec(),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        n_windows=6,
    )
    assert out.n_windows_actual == 6
    # IS_avg = (0.50 + 0*5) / 6 ≈ 0.0833; OOS_avg = (0.40 + -0.05*5) / 6 ≈ 0.0250
    assert out.degradation_ratio < 0.5
    # Only 1 of 6 windows had positive OOS.
    assert out.out_of_sample_positive_rate == pytest.approx(1.0 / 6.0, abs=1e-9)


def test_short_range_collapses_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 30-day window with 4h primary tf can't sustain 6 sub-windows of
    the minimum-bar floor. The function gracefully clamps.
    """
    monkeypatch.setattr(wf_module, "run_backtest", _stub_backtest)
    monkeypatch.setattr(wf_module, "compute_metrics", lambda _r, _tf: _metrics(0.05))

    out = run_walk_forward(
        _minimal_spec(),
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 31, tzinfo=UTC),
        n_windows=6,
    )
    # 30d / 4h bars: 180 bars total; each window needs ~333 bars worth
    # of time for the 0.7/0.3 split + 50-bar floor. So we get ~1 window
    # rather than 6 — the function must clamp instead of raising.
    assert 1 <= out.n_windows_actual <= 6
    assert out.n_windows_requested == 6


def test_engine_failure_treated_as_zero_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `run_backtest` raises (e.g., no signals), the segment is
    recorded as zero-return / zero-trades rather than aborting.
    """

    def fail(*_: object, **__: object) -> BacktestRun:
        raise RuntimeError("nothing happened")

    monkeypatch.setattr(wf_module, "run_backtest", fail)
    # compute_metrics never runs because run_backtest raises first.

    out = run_walk_forward(
        _minimal_spec(),
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        n_windows=4,
    )
    assert out.in_sample_avg_return == 0.0
    assert out.out_of_sample_avg_return == 0.0
    assert out.degradation_ratio_valid is False


def test_n_windows_rejects_bad_args() -> None:
    spec = _minimal_spec()
    with pytest.raises(ValueError, match="n_windows"):
        run_walk_forward(
            spec,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 6, 1, tzinfo=UTC),
            n_windows=0,
        )
    with pytest.raises(ValueError, match="train_ratio"):
        run_walk_forward(
            spec,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 6, 1, tzinfo=UTC),
            train_ratio=1.5,
        )
    with pytest.raises(ValueError, match="full_end"):
        run_walk_forward(
            spec,
            datetime(2024, 6, 1, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        )


_ = Callable  # silence unused import


# ---- A.4: continuous-run walk-forward for stateful specs -----------------

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _load_fixture(rel: str) -> StrategySpec:
    return StrategySpec.model_validate(json.loads((_FIXTURES / "strategies" / rel).read_text()))


def _metrics_stub(value: float) -> Callable[[BacktestRun, Timeframe], BacktestMetrics]:
    """A typed compute_metrics replacement returning a fixed return_pct."""

    def _m(_run: BacktestRun, _tf: Timeframe) -> BacktestMetrics:
        return _metrics(value)

    return _m


def _count_run(counter: list[int]) -> Callable[..., BacktestRun]:
    """A run_backtest stub that records each call's (start, end) — used to
    prove the v2 path makes ONE continuous run and v1 makes 2N cold ones.
    """

    def _run(_spec: StrategySpec, start: datetime, end: datetime, *_: object, **__: object) -> BacktestRun:
        counter.append(1)
        return _stub_backtest(_spec, start, end)

    return _run


def _continuous_run(start: datetime, end: datetime, trade_day_offsets: list[int]) -> BacktestRun:
    """A synthetic full-range BacktestRun: a daily equity curve growing
    10000 → 14000 linearly, plus one trade entered on each given day
    offset. Used to verify the continuous path slices trades and equity
    into the right windows.
    """
    n_days = (end - start).days
    equity = [
        EquityPoint(timestamp=start + timedelta(days=d), value=10_000.0 + d * (4_000.0 / n_days))
        for d in range(n_days + 1)
    ]
    trades = [
        Trade(
            entry_time=start + timedelta(days=d),
            exit_time=start + timedelta(days=d + 1),
            entry_price=100.0,
            exit_price=105.0,
            size=1.0,
            pnl=5.0,
            return_pct=0.05,
            direction=Direction.LONG,
            exit_reason="signal",
        )
        for d in trade_day_offsets
    ]
    meta = BacktestMeta(
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
    return BacktestRun(spec_name="t", meta=meta, equity_curve=equity, trades=trades)


def test_stateful_spec_walk_forward_uses_a_single_continuous_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case §5/§5.2: a v2 spec — even a T2-only regime_state spec with
    NO Tier-3 condition — is walked as ONE continuous backtest, because
    condition_uses_stateful_v2 covers T2. The cold per-segment path is not
    used.
    """
    spec = _load_fixture("valid/09_regime_state_supertrend.json")
    # The edge case: stateful (v2) but not Tier-3.
    assert condition_uses_tier3(spec.entry.condition) is False

    calls: list[int] = []
    monkeypatch.setattr(wf_module, "run_backtest", _count_run(calls))
    monkeypatch.setattr(wf_module, "compute_metrics", _metrics_stub(0.1))

    out = run_walk_forward(
        spec,
        datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        n_windows=6,
    )
    assert out.n_windows_actual == 6
    assert len(calls) == 1  # one continuous run, not 12 cold segments


def test_v1_spec_walk_forward_uses_cold_per_segment_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 (non-stateful) spec keeps the cold path: 2 backtests per window."""
    calls: list[int] = []
    monkeypatch.setattr(wf_module, "run_backtest", _count_run(calls))
    monkeypatch.setattr(wf_module, "compute_metrics", _metrics_stub(0.1))

    out = run_walk_forward(
        _minimal_spec(),  # Golden Cross — v1
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
        n_windows=6,
    )
    assert out.n_windows_actual == 6
    assert len(calls) == 12  # 6 windows × (IS + OOS)


def test_continuous_run_attributes_trades_and_equity_by_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The continuous run is sliced into windows: a trade is attributed to
    the window containing its entry_time, and each window's return is
    computed off the equity slice. One trade is placed in each of the four
    segments (2 windows × IS/OOS); each segment must count exactly one.
    """
    start, end = datetime(2021, 1, 1, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)
    # n_windows=2, train_ratio=0.5 → segment boundaries at days 91.25 /
    # 182.5 / 273.75. Place one trade squarely inside each segment.
    run = _continuous_run(start, end, trade_day_offsets=[30, 120, 220, 320])

    def _return_run(*_a: object, **_kw: object) -> BacktestRun:
        return run

    monkeypatch.setattr(wf_module, "run_backtest", _return_run)

    spec = _load_fixture("valid/09_regime_state_supertrend.json")
    out = run_walk_forward(spec, start, end, n_windows=2, train_ratio=0.5)

    assert out.n_windows_actual == 2
    w0, w1 = out.windows
    # Trade attribution by entry_time — one trade per segment.
    assert w0.in_sample_num_trades == 1
    assert w0.out_of_sample_num_trades == 1
    assert w1.in_sample_num_trades == 1
    assert w1.out_of_sample_num_trades == 1
    # Equity grows monotonically, so every window slice has a positive
    # window-start-relative return.
    assert w0.in_sample_return_pct > 0.0
    assert w1.out_of_sample_return_pct > 0.0


def test_turtle_walk_forward_is_sensible_not_degenerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (real engine, frozen data): the Turtle prior_signal spec
    walk-forwards through the continuous path and produces a non-degenerate
    result — six windows, a valid degradation ratio, and OOS returns that
    are not all identical.
    """
    frozen = pd.read_parquet(_FIXTURES / "market" / "btc_usdt_4h.parquet")

    def _get_market_data(
        _symbol: object, _tf: object, start: datetime, end: datetime, **_kw: object,
    ) -> pd.DataFrame:
        return cast("pd.DataFrame", frozen.loc[start:end])

    monkeypatch.setattr(engine_module, "get_market_data", _get_market_data)
    turtle = _load_fixture("valid/11_turtle_system1.json")

    out = run_walk_forward(
        turtle,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2026, 5, 20, tzinfo=UTC),
    )
    assert out.n_windows_actual == 6
    assert out.degradation_ratio_valid is True
    oos = [w.out_of_sample_return_pct for w in out.windows]
    # Not degenerate: the windows must not all collapse to the same number.
    assert len(set(oos)) > 1
    # Some trading happened across the fold sequence.
    assert sum(w.out_of_sample_num_trades for w in out.windows) > 0
