"""A.6 drift-parity gate — the live Tier-3 stepper vs the iterative engine.

THE non-negotiable A.6 acceptance test (design doc §6C). It walks Turtle
System 1 — a `prior_signal` spec with phantom outcomes — through:

  (a) `iterative.run_iterative_backtest` — one-shot, full history;
  (b) `iterative_live.run_live_cycle` — bar-by-bar, with `Tier3State`
      round-tripped through JSON each cycle (the live trader's
      `trader_strategy_state` persistence path).

and asserts zero divergence. A failure here means the B3 sibling stepper
has drifted from the iterative engine it mirrors.

Two comparisons:
  * incremental vs one-shot — both the live stepper, so a *full* equality
    (signals, trades, resolved phantom outcomes). Proves the incremental
    checkpoint / JSON round-trip introduces no divergence.
  * the live stepper's settled trades vs `run_iterative_backtest`'s trades
    — bit-for-bit. Anchors the stepper's logic to the engine; Turtle's
    entries are prior_signal-gated, so matching trades proves the live
    phantom-gating matches the engine. The backtest's end-of-data close is
    excluded — that boundary bar is the live trader's moving edge (§6C).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_shared.schemas.trader import Tier3State
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.iterative_live import run_live_cycle
from marketmind_workers.backtest.trade_history import classify_outcome

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_TURTLE = _FIXTURES / "strategies" / "valid" / "11_turtle_system1.json"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"
_BARS = 1300  # design doc §6C: a 1200+ bar window exercising all T3 mechanics
# The first live cycle catches up [0.._WARMUP] in one call; one-bar cycles
# (the incremental machinery under test) then run from _WARMUP+1. This
# mirrors production — the signal engine only calls run_live_cycle once a
# version has cleared its min_bars warmup (a fresh strategy's first cycle
# likewise catches up to the latest candle). It also keeps the `ta` ATR
# off the sub-window data it cannot compute.
_WARMUP = 50


def _round_trip(state: Tier3State) -> Tier3State:
    """JSON round-trip — the live trader's trader_strategy_state JSONB path."""
    return Tier3State.model_validate(json.loads(json.dumps(state.model_dump(mode="json"))))


@pytest.fixture(scope="module")
def spec() -> StrategySpec:
    return StrategySpec.model_validate(json.loads(_TURTLE.read_text()))


@pytest.fixture(scope="module")
def data() -> pd.DataFrame:
    return pd.read_parquet(_PARQUET).iloc[:_BARS]


@pytest.fixture(scope="module")
def incremental_state(spec: StrategySpec, data: pd.DataFrame) -> Tier3State:
    """Walk the live stepper bar-by-bar — one run_live_cycle per candle,
    Tier3State round-tripped through JSON between cycles (the real path).
    """
    tf = spec.primary_timeframe
    # First cycle catches up [0.._WARMUP]; then one bar per cycle.
    first, _ = run_live_cycle(spec, {tf: data.iloc[: _WARMUP + 1]}, None, last_bar=-1)
    state = _round_trip(first)
    for k in range(_WARMUP + 1, len(data)):
        new_state, _decision = run_live_cycle(
            spec, {tf: data.iloc[: k + 1]}, state, last_bar=k - 1,
        )
        state = _round_trip(new_state)
    return state


@pytest.fixture(scope="module")
def one_shot_state(spec: StrategySpec, data: pd.DataFrame) -> Tier3State:
    state, _ = run_live_cycle(spec, {spec.primary_timeframe: data}, None, last_bar=-1)
    return state


def test_live_stepper_incremental_equals_one_shot(
    incremental_state: Tier3State,
    one_shot_state: Tier3State,
) -> None:
    """Bar-by-bar (with JSON round-trips) == one-shot — full Tier3State
    equality, including every resolved phantom outcome. Zero divergence.
    """
    assert incremental_state == one_shot_state, (
        "the incremental live stepper diverged from a one-shot run — the "
        "checkpoint / round-trip machinery is not state-preserving"
    )
    # Non-vacuous: Turtle must have fired entries, skipped entries, and
    # resolved phantom outcomes for the gate to mean anything.
    signals = incremental_state.signal_history
    assert any(s.fired for s in signals), "no fired signals — vacuous"
    assert any(not s.fired for s in signals), "no skipped signals — vacuous"
    assert any(
        not s.fired and s.outcome is not None for s in signals
    ), "no resolved phantom outcomes — vacuous"


def test_live_stepper_trades_match_iterative_backtest(
    spec: StrategySpec,
    data: pd.DataFrame,
    incremental_state: Tier3State,
) -> None:
    """The live stepper's settled trades == run_iterative_backtest's trades,
    bit-for-bit on entry bar, return_pct and win/loss outcome.
    """
    bt = run_iterative_backtest(
        spec,
        {spec.primary_timeframe: data},
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2030, 1, 1, tzinfo=UTC),
    )
    # The backtest force-closes an open position at end-of-data; the live
    # stepper leaves it open (its moving edge). Compare settled trades only.
    bt_settled = [t for t in bt.trades if t.exit_reason != "end_of_data"]
    live_trades = incremental_state.trade_history
    assert len(live_trades) == len(bt_settled), (
        f"trade count diverged — live {len(live_trades)} vs backtest "
        f"{len(bt_settled)}"
    )
    assert live_trades, "no trades — the drift-parity gate is vacuous"

    index = data.index
    for i, (lv, bt_t) in enumerate(zip(live_trades, bt_settled, strict=True)):
        assert lv.entry_index == index.get_loc(bt_t.entry_time), (
            f"trade {i}: entry bar diverged"
        )
        assert lv.return_pct == bt_t.return_pct, (
            f"trade {i}: return_pct diverged — live {lv.return_pct} vs "
            f"backtest {bt_t.return_pct}"
        )
        assert lv.outcome == classify_outcome(bt_t.return_pct).value, (
            f"trade {i}: win/loss outcome diverged"
        )
