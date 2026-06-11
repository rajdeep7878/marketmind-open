"""A.5a — the generic SpecTemplate.

SpecTemplate runs an extracted v2 StrategySpec through the shared
backtest evaluators (`translator.build_signals`). These tests cover:
T1 (bounded-window) and T2 (regime_state) condition evaluation, the
BUY / EXIT / HOLD decision, the stop/take-profit price computation, and
the construction-time rejection of specs A.5a does not support
(Tier-3, short, multi-timeframe, stopless).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_shared.schemas.trader import (
    PaperPosition,
    PositionStatus,
    SignalKind,
    StrategyState,
    TemplateName,
)
from marketmind_workers.backtest.translator import build_signals, build_signals_stateful
from marketmind_workers.trader.templates import build_template
from marketmind_workers.trader.templates.spec_template import (
    SpecTemplate,
    spec_template_rejection_reason,
)
from pydantic import ValidationError

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

# ---- payload helpers -------------------------------------------------------

_PRICE_CLOSE: dict[str, Any] = {"kind": "price", "field": "close"}


def _crossover(value: float, direction: str) -> dict[str, Any]:
    return {
        "type": "crossover",
        "series": _PRICE_CLOSE,
        "threshold": {"kind": "constant", "value": value},
        "direction": direction,
    }


def _t1_spec_dict(*, direction: str = "long", with_stop: bool = True) -> dict[str, Any]:
    """A T1 (bounded-window) spec: enter when a >100 breakout occurred
    within the last 3 bars; exit on a <90 cross; 5% stop.
    """
    exits: list[dict[str, Any]] = [
        {"type": "condition", "condition": _crossover(90.0, "below")},
    ]
    if with_stop:
        exits.append({"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}})
    return {
        "schema_version": "1.0",
        "name": "SpecTemplate T1 Test",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "4h",
        "direction": direction,
        "entry": {
            "condition": {
                "type": "within_last_n_bars",
                "condition": _crossover(100.0, "above"),
                "n": 3,
            },
            "order_type": "market",
        },
        "exit": {"exits": exits},
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
    }


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="4h")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": np.full(n, 1e6)},
        index=idx,
    )


def _spec_template(spec_dict: dict[str, Any]) -> SpecTemplate:
    tmpl = build_template(TemplateName.SPEC, {"spec": spec_dict})
    assert isinstance(tmpl, SpecTemplate)
    return tmpl


def _open_position(stop_price: Decimal = Decimal("95")) -> PaperPosition:
    return PaperPosition(
        id=uuid4(),
        strategy_version_id=uuid4(),
        symbol="BTC/USDT",
        entry_order_id=uuid4(),
        entry_price=Decimal("100"),
        entry_ts=datetime(2024, 1, 1, tzinfo=UTC),
        size=Decimal("0.5"),
        stop_price=stop_price,
        status=PositionStatus.OPEN,
    )


# ---- T1 evaluation: BUY / EXIT / HOLD --------------------------------------


def test_t1_spec_emits_buy_when_entry_condition_fires() -> None:
    tmpl = _spec_template(_t1_spec_dict())
    # >100 breakout at bar 9 (95 -> 101); within_last_n_bars(3) is still
    # true at the final bar, and the strategy is flat -> BUY.
    df = _ohlcv([80, 80, 80, 80, 80, 80, 80, 80, 95, 101, 100, 100])
    ev = tmpl.evaluate(df, None)
    assert ev.kind is SignalKind.BUY
    assert ev.proposed_stop_price > Decimal(0)
    # 5% stop off the latest close (100).
    assert ev.proposed_stop_price == Decimal("95.00")


def test_t1_spec_holds_when_flat_and_no_entry() -> None:
    tmpl = _spec_template(_t1_spec_dict())
    df = _ohlcv([80.0] * 12)  # never breaks 100
    ev = tmpl.evaluate(df, None)
    assert ev.kind is SignalKind.HOLD


def test_t1_spec_emits_exit_when_exit_condition_fires() -> None:
    tmpl = _spec_template(_t1_spec_dict())
    # Close crosses below 90 on the final bar; with a position open -> EXIT.
    df = _ohlcv([100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 92, 88])
    ev = tmpl.evaluate(df, _open_position())
    assert ev.kind is SignalKind.EXIT
    assert ev.proposed_stop_price == Decimal("95")  # carries the position's stop


def test_spec_template_decision_matches_build_signals() -> None:
    """One source of truth: the SpecTemplate's BUY/HOLD decision is exactly
    `build_signals`' last-bar entry signal — it reuses that code, it does
    not re-implement condition evaluation.
    """
    spec_dict = _t1_spec_dict()
    spec = StrategySpec.model_validate(spec_dict)
    df = _ohlcv([80, 80, 80, 80, 80, 80, 80, 80, 95, 101, 100, 100])
    signal_set = build_signals(spec, {spec.primary_timeframe: df})
    tmpl = _spec_template(spec_dict)
    ev = tmpl.evaluate(df, None)
    assert (ev.kind is SignalKind.BUY) == bool(signal_set.entries.iloc[-1])


# ---- T2 evaluation: the Supertrend regime spec -----------------------------


def test_t2_regime_spec_evaluates_and_matches_build_signals() -> None:
    """Fixture 09 (regime_state Supertrend) runs through the SpecTemplate
    on the frozen BTC/USDT 4h data, and the last-bar decision matches
    build_signals — T2 condition evaluation is reused, not re-implemented.
    """
    spec_dict = json.loads(
        (_FIXTURES / "strategies" / "valid" / "09_regime_state_supertrend.json").read_text(),
    )
    spec = StrategySpec.model_validate(spec_dict)
    df = pd.read_parquet(_FIXTURES / "market" / "btc_usdt_4h.parquet")

    tmpl = _spec_template(spec_dict)
    ev = tmpl.evaluate(df, None)
    signal_set = build_signals(spec, {spec.primary_timeframe: df})

    assert ev.kind in (SignalKind.BUY, SignalKind.HOLD)
    assert (ev.kind is SignalKind.BUY) == bool(signal_set.entries.iloc[-1])


def test_min_bars_needed_covers_indicator_warmup() -> None:
    # Fixture 09 uses EMA(200); min_bars_needed must clear that warmup.
    spec_dict = json.loads(
        (_FIXTURES / "strategies" / "valid" / "09_regime_state_supertrend.json").read_text(),
    )
    tmpl = _spec_template(spec_dict)
    assert tmpl.min_bars_needed() > 200


# ---- construction-time rejection (A.5a scope) ------------------------------


def _prior_signal_spec() -> dict[str, Any]:
    """A Tier-3 spec — entry gated by prior_signal."""
    d = _t1_spec_dict()
    d["schema_version"] = "2.0"
    d["entry"]["condition"] = {
        "type": "and",
        "conditions": [
            _crossover(100.0, "above"),
            {"type": "not", "condition": {
                "type": "prior_signal", "predicate": "last_would_have_won"}},
        ],
    }
    return d


def test_accepts_tier3_spec() -> None:
    """A.6: Tier-3 specs are no longer rejected — they build and route
    through the live shadow-simulation stepper.
    """
    assert spec_template_rejection_reason(
        StrategySpec.model_validate(_prior_signal_spec()),
    ) is None
    tmpl = build_template(TemplateName.SPEC, {"spec": _prior_signal_spec()})
    assert isinstance(tmpl, SpecTemplate)
    assert tmpl.is_tier3
    assert tmpl.is_stateful  # Tier-3 is a stateful condition


def test_turtle_evaluate_stateful_runs_and_advances() -> None:
    """A.6: a Tier-3 spec (Turtle) evaluates through evaluate_stateful —
    cold start, then seeded — and the Tier3 checkpoint advances.
    """
    spec_dict = json.loads(
        (_FIXTURES / "strategies" / "valid" / "11_turtle_system1.json").read_text(),
    )
    tmpl = _spec_template(spec_dict)
    assert tmpl.is_tier3
    df = pd.read_parquet(_FIXTURES / "market" / "btc_usdt_4h.parquet")

    # Cold start over the first 600 bars.
    ev1, state1 = tmpl.evaluate_stateful(df.iloc[:600], None, None)
    assert ev1.kind in (SignalKind.BUY, SignalKind.EXIT, SignalKind.HOLD)
    assert state1.tier3 is not None
    assert state1.tier3.last_bar == 599
    assert state1.regimes == []  # a Tier-3 spec carries no Tier-2 state

    # One more candle, seeded from the checkpoint — the state advances.
    ev2, state2 = tmpl.evaluate_stateful(df.iloc[:601], None, state1)
    assert ev2.kind in (SignalKind.BUY, SignalKind.EXIT, SignalKind.HOLD)
    assert state2.tier3 is not None
    assert state2.tier3.last_bar == 600
    # Turtle prints signals over 600 bars — the checkpoint is non-trivial.
    assert len(state2.tier3.signal_history) > 0


def test_rejects_short_spec() -> None:
    short = _t1_spec_dict(direction="short")
    reason = spec_template_rejection_reason(StrategySpec.model_validate(short))
    assert reason is not None
    assert "long-only" in reason
    with pytest.raises(ValidationError, match="long-only"):
        build_template(TemplateName.SPEC, {"spec": short})


def test_rejects_multi_timeframe_spec() -> None:
    mtf = _t1_spec_dict()
    mtf["filter_timeframe"] = "1d"
    reason = spec_template_rejection_reason(StrategySpec.model_validate(mtf))
    assert reason is not None
    assert "multi-timeframe" in reason


def test_rejects_stopless_spec() -> None:
    stopless = _t1_spec_dict(with_stop=False)
    reason = spec_template_rejection_reason(StrategySpec.model_validate(stopless))
    assert reason is not None
    assert "stop_loss" in reason


def test_accepts_a_supported_t1_t2_spec() -> None:
    # The positive case: a supported spec has no rejection reason.
    assert spec_template_rejection_reason(StrategySpec.model_validate(_t1_spec_dict())) is None
    spec09 = json.loads(
        (_FIXTURES / "strategies" / "valid" / "09_regime_state_supertrend.json").read_text(),
    )
    assert spec_template_rejection_reason(StrategySpec.model_validate(spec09)) is None


# ---- time exit -------------------------------------------------------------


def test_supertrend_regime_state_evolves_consistently_with_one_shot() -> None:
    """A.5b state evolution: walking the Supertrend regime spec bar-by-bar
    with a seeded window — the live trader's path — reproduces the
    one-shot vectorised backtest's entry signal exactly, and the regime
    latch genuinely flips across the walk. This is the §6B.3 / §6.6
    drift-parity property: persisted state makes the live regime
    full-history-exact.
    """
    spec = StrategySpec.model_validate(
        json.loads(
            (_FIXTURES / "strategies" / "valid" / "09_regime_state_supertrend.json").read_text(),
        ),
    )
    df = pd.read_parquet(_FIXTURES / "market" / "btc_usdt_4h.parquet")
    tf = spec.primary_timeframe
    one_shot = build_signals(spec, {tf: df}).entries

    tmpl = _spec_template(spec.model_dump(mode="json"))
    window = tmpl.min_bars_needed()

    start, end = 4000, 5200
    # Seed the chain with the correct state as of bar start-1.
    _, state = build_signals_stateful(spec, {tf: df.iloc[:start]}, None)
    latches: list[bool] = []
    for bar in range(start, end):
        win = df.iloc[bar + 1 - window : bar + 1]
        signal_set, state = build_signals_stateful(spec, {tf: win}, state)
        assert bool(signal_set.entries.iloc[-1]) == bool(one_shot.iloc[bar]), (
            f"bar {bar}: the seeded incremental entry diverged from the "
            "one-shot backtest — the live regime is not full-history-exact"
        )
        latches.append(state.regimes[0].latched)

    assert {True, False} <= set(latches), (
        "the regime latch did not flip across the walk — the test must "
        "exercise a real state transition to be meaningful"
    )


def _json_round_trip(state: StrategyState) -> StrategyState:
    """Serialize a StrategyState to JSON and back — the exact path it
    takes through the trader_strategy_state JSONB column between cycles.
    """
    return StrategyState.model_validate(json.loads(json.dumps(state.model_dump(mode="json"))))


def test_drift_parity_supertrend_live_path_matches_backtest() -> None:
    """The §6.6 drift-parity gate. Walking `SpecTemplate.evaluate_stateful`
    bar-by-bar — round-tripping `StrategyState` through JSON each bar, as
    it would pass through the `trader_strategy_state` JSONB column —
    reproduces the one-shot vectorised backtest's entry decision at every
    bar. Zero drift between the live trader path and the backtest.

    If this test fails after a future change, drift has been introduced —
    Mechanism A's seeding (design doc §6B) no longer holds.
    """
    spec_dict = json.loads(
        (_FIXTURES / "strategies" / "valid" / "09_regime_state_supertrend.json").read_text(),
    )
    spec = StrategySpec.model_validate(spec_dict)
    df = pd.read_parquet(_FIXTURES / "market" / "btc_usdt_4h.parquet")
    tf = spec.primary_timeframe
    one_shot = build_signals(spec, {tf: df}).entries

    tmpl = _spec_template(spec_dict)
    window = tmpl.min_bars_needed()
    start, end = 4000, 5200

    # Seed the chain with the correct state as of bar start-1, then
    # round-trip it — the live trader reads its seed back from JSONB too.
    _, seed = build_signals_stateful(spec, {tf: df.iloc[:start]}, None)
    state = _json_round_trip(seed)
    latches: list[bool] = []
    for bar in range(start, end):
        win = df.iloc[bar + 1 - window : bar + 1]
        evaluation, next_state = tmpl.evaluate_stateful(win, None, state)
        state = _json_round_trip(next_state)  # the JSONB persistence round-trip
        assert (evaluation.kind is SignalKind.BUY) == bool(one_shot.iloc[bar]), (
            f"bar {bar}: the live SpecTemplate path diverged from the one-shot "
            "backtest — drift has been introduced (design doc §6.6)"
        )
        latches.append(state.regimes[0].latched)

    assert {True, False} <= set(latches), (
        "the regime latch did not flip across the walk — the gate must "
        "exercise a real state transition"
    )


def test_spec_emits_exit_on_time_exit() -> None:
    """A `time` exit fires once `max_bars_held` candles have printed since
    the position's entry — even with no exit *condition* triggered.
    """
    spec_dict = _t1_spec_dict()
    spec_dict["exit"]["exits"].append({"type": "time", "max_bars_held": 4})
    tmpl = _spec_template(spec_dict)
    # 12 bars from 2024-01-01; the position entered on the first bar, so
    # 11 bars are held — well past max_bars_held=4. Closes stay at 100, so
    # the <90 exit condition never fires: the EXIT is the time exit.
    df = _ohlcv([100.0] * 12)
    ev = tmpl.evaluate(df, _open_position())
    assert ev.kind is SignalKind.EXIT
    assert "time exit" in ev.reason
