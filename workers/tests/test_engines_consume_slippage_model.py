"""Phase B.2 — engines consume SlippageModel, not spec.costs.slippage_pct.

Direct sibling to ``test_engines_consume_fee_model.py`` (commit 0910c2d).
The bit-identity regression in B.2's commit 3 (the existing 1085-pass
suite) proves that the SlippageModel *default* matches v1's hardcoded
slippage_pct=0.0005 for the seeded strategies. This test proves the
opposite direction: swapping in a non-default SlippageModel actually
changes engine output.

Pattern: the gate-independent first-trade comparison.
  - The first trade in a prior_signal / prior_trade strategy is always
    allowed (no priors to gate against), so its return_pct depends only
    on the per-trade fill math (fee + slippage), not on any gating
    side-effect.
  - Asserting ``high_slippage_run.trades[0].return_pct <
    default_slippage_run.trades[0].return_pct`` isolates the SlippageModel
    delta from any downstream phantom-outcome-driven gating changes.
  - For prior_signal-gated specs like Turtle System 1, the elevated
    slippage *also* shifts phantom-trade outcomes, which propagates into
    the skip-after-winner gate and changes the downstream trade ledger
    — that's the gate working correctly, not a test artefact. We
    secondary-assert "some ledger change happened" rather than "trade
    count stable".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.slippage_model import SlippageTier, StaticSlippageModel

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_TURTLE = _FIXTURES / "strategies" / "valid" / "11_turtle_system1.json"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"
_BARS = 1300


def _high_slippage_model() -> StaticSlippageModel:
    # 20 bps — 4× the default. Brief specifies "e.g. 20 bps".
    return StaticSlippageModel(
        {
            "binance_spot": {
                "BTC/USDT": {
                    "taker": [SlippageTier(volume_30d_usd_min=0.0, bps=20.0)],
                    "maker": [SlippageTier(volume_30d_usd_min=0.0, bps=20.0)],
                },
            },
        },
    )


def test_iterative_engine_uses_slippage_model(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = StrategySpec.model_validate(json.loads(_TURTLE.read_text()))
    data = pd.read_parquet(_PARQUET).iloc[:_BARS]
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)

    # Default 5 bps slippage run.
    default_run = run_iterative_backtest(spec, {spec.primary_timeframe: data}, start, end)
    assert default_run.trades, "fixture must produce trades for the test to be meaningful"

    # Swap in a 20 bps SlippageModel — 4× the default. Higher per-fill
    # slippage must reach the engine's fill math; observable proof is
    # the FIRST trade's return changing (the first trade is allowed
    # regardless of any gate, so its return depends only on the fill
    # math, not on phantom-outcome-driven gating).
    monkeypatch.setattr(
        "marketmind_workers.backtest.iterative.default_slippage_model",
        _high_slippage_model,
    )
    high_run = run_iterative_backtest(spec, {spec.primary_timeframe: data}, start, end)

    # The first trade is gate-independent — same entry signal in both
    # runs; its return_pct difference is the slippage delta.
    default_first = default_run.trades[0].return_pct
    high_first = high_run.trades[0].return_pct
    assert high_first < default_first, (
        f"first trade return_pct unchanged ({high_first} vs {default_first}) "
        "— the SlippageModel isn't reaching the iterative engine's fill math"
    )

    # Secondary observation: for a prior_signal-gated strategy like
    # Turtle, higher slippage also shifts phantom-trade outcomes, which
    # propagates into the skip-after-winner gate and changes downstream
    # trade selection. Trade count therefore CAN diverge — that is the
    # gate doing its job, not a test artefact. We assert "some change
    # happened" rather than "trade count stable", matching the same
    # finding from B.1's test_engines_consume_fee_model.py.
    assert default_run.trades != high_run.trades, (
        "default and high-slippage runs produced identical trade ledgers "
        "— SlippageModel swap had no observable effect"
    )
