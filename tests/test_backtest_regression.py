"""Phase A.3a regression gate — the two production strategies must
backtest bit-identically before and after the stateful-engine changes.

Golden Cross (extraction f1c1df78) and BB Breakout (extraction 3cfad373)
are the two specs running in production paper trading. The reference
metrics in fixtures/strategies/regression/*_reference.json were captured
from the pre-A.3a (v1) engine. If A.3a changes either strategy's numbers,
the validation that approved them for paper is invalidated — that is a
stop-and-report blocker, not a test to relax.

Determinism: a frozen BTC/USDT 4h dataset
(fixtures/market/btc_usdt_4h.parquet) is fed through run_backtest's
`data_override` hook — no network, no live data, no clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.metrics import compute_metrics

_FIXTURES = Path(__file__).parent / "fixtures"
_REG = _FIXTURES / "strategies" / "regression"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"

# The five metrics the A.3a brief pins as the regression contract.
_METRIC_KEYS = ("total_return_pct", "sharpe_ratio", "max_drawdown_pct", "num_trades", "win_rate")


@pytest.mark.parametrize("strategy", ["golden_cross", "bb_breakout"])
def test_v1_strategy_backtest_is_bit_identical(strategy: str) -> None:
    spec = StrategySpec.model_validate(json.loads((_REG / f"{strategy}.json").read_text()))
    reference: dict[str, float] = json.loads((_REG / f"{strategy}_reference.json").read_text())

    df = pd.read_parquet(_PARQUET)
    # data_override uses the whole frame; start/end only populate meta.
    run = run_backtest(
        spec,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2026, 5, 20, tzinfo=UTC),
        10_000.0,
        data_override={spec.primary_timeframe: df},
    )
    metrics = compute_metrics(run, spec.primary_timeframe)
    actual = {key: getattr(metrics, key) for key in _METRIC_KEYS}

    assert actual == reference, (
        f"{strategy} backtest metrics drifted from the v1 baseline.\n"
        f"  reference: {reference}\n"
        f"  actual:    {actual}\n"
        "This is the A.3a regression gate — a non-negotiable stop-and-report "
        "blocker. The two production paper-trading strategies must not change."
    )
