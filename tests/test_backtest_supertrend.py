"""A.3a Supertrend integration test — a regime_state (Tier-2) strategy
backtests end-to-end through the vectorbt engine.

Supertrend-style trend regimes were previously inexpressible: the v1
schema had no way to latch a direction across bars. Fixture 09 models
one with a regime_state condition — long while a close>EMA200 regime is
latched on AND EMA20 crosses above EMA50. This test runs that spec
through the real engine on the frozen BTC/USDT 4h dataset and asserts a
clean, non-empty backtest, proving the Tier-2 path works end to end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from marketmind_shared.schemas import SignalDiagnosticsFailureMode
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.metrics import compute_metrics

_FIXTURES = Path(__file__).parent / "fixtures"
_SPEC = _FIXTURES / "strategies" / "valid" / "09_regime_state_supertrend.json"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"


def test_supertrend_regime_strategy_backtests_end_to_end() -> None:
    spec = StrategySpec.model_validate(json.loads(_SPEC.read_text()))
    assert spec.schema_version == "2.0"

    df = pd.read_parquet(_PARQUET)
    run = run_backtest(
        spec,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2026, 5, 20, tzinfo=UTC),
        10_000.0,
        data_override={spec.primary_timeframe: df},
    )
    metrics = compute_metrics(run, spec.primary_timeframe)

    # The regime_state path must produce real signals and real trades —
    # not a silent zero-trade result (the v1.1 failure mode).
    assert run.entry_diagnostics.failure_mode is SignalDiagnosticsFailureMode.NONE
    assert run.entry_diagnostics.true_count > 0
    assert metrics.num_trades > 0
    assert len(run.equity_curve) > 0
    # win_rate is a well-defined fraction once trades exist.
    assert 0.0 <= metrics.win_rate <= 1.0
