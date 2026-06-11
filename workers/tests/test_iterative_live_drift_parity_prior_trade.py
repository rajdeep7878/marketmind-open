"""A.5c / A.6 drift-parity gate — `prior_trade` live execution.

Companion to test_iterative_live_drift_parity.py (which covers
`prior_signal` / Turtle System 1). Both go through the same
iterative_live.py + Tier3State + JSON round-trip path; this test closes
the test-coverage gap A.6 flagged for the prior_trade-specific branch
(skip-after-winner via completed-trade outcomes, no phantoms).

Test spec — "skip entry if the last completed trade was a winner":

    entry = crossover(EMA(10) above EMA(30)) AND NOT prior_trade(last_won, n=1)
    exit  = crossover(EMA(10) below EMA(30)) OR trailing_atr(14, 2.0)

A note on this specific predicate: `NOT prior_trade(last_won)` is
structurally sticky — once the predicate ever evaluates True (a win
just completed), it blocks every subsequent entry and there is no path
to flip it back without a new completed trade. That is *real* gating
semantics — the same shape Turtle's `prior_signal` rule has — but
without `prior_signal`'s phantom-resolution escape hatch. On the test
window (1300 4h-BTC bars) the strategy produces exactly one trade:
the first EMA cross fires, wins, the gate locks, and every subsequent
EMA cross is gate-blocked. That is still meaningful exercise:

  - The trade_history JSON round-trip is exercised (write + readback
    each cycle for the 1100+ cycles after the trade completes).
  - The `prior_trade(last_won)` predicate is *evaluated* at every
    candidate entry across the run — once before the trade (returns
    False → entry allowed) and then on every subsequent EMA-up-cross
    (returns True → entry blocked).
  - The drift-parity assertion validates that the live stepper computes
    that predicate identically to the iterative engine for both phases.

The sticky gate is the *whole point* of this test: it is a tight,
deterministic exercise of the prior_trade-specific code path.

This is the prior_trade analog of Turtle System 1's prior_signal: same
skip-after-winner intent, but gated on COMPLETED trades only (no phantom
resolution — completed-trade outcomes are deterministic the moment the
trade closes). The drift-parity check is the same — incremental vs
one-shot live, then live vs iterative — proving the live stepper's
prior_trade evaluation matches the engine after JSON round-trip.

Why this test exists despite prior_signal already being covered:
prior_signal exercises the phantom-resolution path; prior_trade exercises
the completed-trades path (a separate code path in
`Tier3State.trade_history` updates). Defence in depth — both T3 mechanics
get their own drift-parity gate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_shared.schemas.trader import Tier3State
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.iterative_live import run_live_cycle
from marketmind_workers.backtest.trade_history import classify_outcome

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"
_BARS = 1300  # mirrors the prior_signal test — exercises the full T3 cycle
_WARMUP = 50  # same as prior_signal — keeps ta ATR off sub-window data


def _round_trip(state: Tier3State) -> Tier3State:
    """JSON round-trip — the live trader's trader_strategy_state JSONB path."""
    return Tier3State.model_validate(json.loads(json.dumps(state.model_dump(mode="json"))))


def _ema_cross(fast: int, slow: int, direction: str) -> dict[str, Any]:
    return {
        "type": "crossover",
        "series": {"kind": "indicator", "name": "ema", "params": {"period": fast}},
        "direction": direction,
        "threshold": {"kind": "indicator", "name": "ema", "params": {"period": slow}},
    }


def _spec_with_gate() -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "name": "prior_trade drift parity (skip-after-winner)",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "and",
                "conditions": [
                    _ema_cross(10, 30, "above"),
                    {
                        "type": "not",
                        "condition": {
                            "type": "prior_trade",
                            "predicate": "last_won",
                            "n": 1,
                        },
                    },
                ],
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "condition", "condition": _ema_cross(10, 30, "below")},
                {
                    "type": "stop_loss",
                    "method": {"kind": "trailing_atr", "atr_period": 14, "mult": 2.0},
                },
            ],
        },
    }


@pytest.fixture(scope="module")
def spec() -> StrategySpec:
    return StrategySpec.model_validate(_spec_with_gate())


@pytest.fixture(scope="module")
def data() -> pd.DataFrame:
    return pd.read_parquet(_PARQUET).iloc[:_BARS]


@pytest.fixture(scope="module")
def incremental_state(spec: StrategySpec, data: pd.DataFrame) -> Tier3State:
    """Walk the live stepper bar-by-bar — one run_live_cycle per candle,
    Tier3State round-tripped through JSON between cycles (the real path).
    """
    tf = spec.primary_timeframe
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
    equality across trade_history. Zero divergence.
    """
    assert incremental_state == one_shot_state, (
        "the incremental live stepper diverged from a one-shot run on a "
        "prior_trade spec — the JSON round-trip / Tier3State persistence "
        "is not state-preserving for the prior_trade path"
    )
    trades = incremental_state.trade_history
    # Vacuity: at least one completed trade — enough to populate
    # trade_history so the prior_trade predicate has data to evaluate
    # against on every subsequent cycle. With this spec the first trade
    # wins and the sticky gate produces exactly one trade (see docstring).
    assert len(trades) >= 1, (
        "no completed trades — prior_trade has nothing to evaluate "
        "against, the prior_trade-specific path isn't exercised"
    )


def test_live_stepper_trades_match_iterative_backtest(
    spec: StrategySpec,
    data: pd.DataFrame,
    incremental_state: Tier3State,
) -> None:
    """Live stepper's settled trades == run_iterative_backtest's trades,
    bit-for-bit on entry bar, return_pct, and outcome.
    """
    bt = run_iterative_backtest(
        spec,
        {spec.primary_timeframe: data},
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2030, 1, 1, tzinfo=UTC),
    )
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
