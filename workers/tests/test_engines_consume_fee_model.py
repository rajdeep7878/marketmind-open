"""Phase B.1 — engines consume FeeModel, not spec.costs.commission_pct.

The bit-identity regression in commit 3/5 (the existing suite) proves
that the FeeModel *default* matches v1's hardcoded commission_pct=0.001
for the seeded strategies. This test proves the opposite direction:
swapping in a non-default FeeModel actually changes engine output. If
this passes, the abstraction is real (not a constant in disguise); if it
fails, the engine isn't reaching the FeeModel at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.fee_model import FeeTier, StaticFeeModel
from marketmind_workers.backtest.iterative import run_iterative_backtest

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_TURTLE = _FIXTURES / "strategies" / "valid" / "11_turtle_system1.json"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"
_BARS = 1300


def _cumulative_return(trades: list[object]) -> float:
    """Geometric compound return across all settled trades."""
    out = 1.0
    for t in trades:
        out *= 1.0 + t.return_pct  # type: ignore[attr-defined]
    return out - 1.0


def _high_fee_model() -> StaticFeeModel:
    return StaticFeeModel(
        {
            "binance_spot": {
                "BTC/USDT": {
                    "taker": [FeeTier(volume_30d_usd_min=0.0, bps=50.0)],
                    "maker": [FeeTier(volume_30d_usd_min=0.0, bps=50.0)],
                },
            },
        },
    )


def test_iterative_engine_uses_fee_model(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = StrategySpec.model_validate(json.loads(_TURTLE.read_text()))
    data = pd.read_parquet(_PARQUET).iloc[:_BARS]
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)

    # Default 10 bps run.
    default_run = run_iterative_backtest(spec, {spec.primary_timeframe: data}, start, end)
    assert default_run.trades, "fixture must produce trades for the test to be meaningful"

    # Swap in a 50 bps FeeModel — 5× the default. Higher per-trade
    # commission must reach the engine's fee math; observable proof is
    # the FIRST trade's return changing (the first trade is allowed
    # regardless of any gate, so its return depends only on the fee
    # math, not on phantom-outcome-driven gating).
    monkeypatch.setattr(
        "marketmind_workers.backtest.iterative.default_fee_model",
        _high_fee_model,
    )
    high_run = run_iterative_backtest(spec, {spec.primary_timeframe: data}, start, end)

    # The first trade is gate-independent — same entry signal in both
    # runs; its return_pct difference is the fee delta.
    default_first = default_run.trades[0].return_pct
    high_first = high_run.trades[0].return_pct
    assert high_first < default_first, (
        f"first trade return_pct unchanged ({high_first} vs {default_first}) "
        "— the FeeModel isn't reaching the iterative engine's fee math"
    )

    # Secondary observation: for a prior_signal-gated strategy like
    # Turtle, higher fees also shift phantom-trade outcomes, which
    # propagates into the skip-after-winner gate and changes downstream
    # trade selection. Trade count therefore CAN diverge — that is the
    # gate doing its job, not a test artefact. We assert "some change
    # happened" rather than "trade count stable".
    assert default_run.trades != high_run.trades, (
        "default and high-fee runs produced identical trade ledgers — "
        "FeeModel swap had no observable effect"
    )
