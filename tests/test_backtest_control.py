"""A.3b control test — the iterative simulator's mechanics match vectorbt.

A static (non-Tier-3) strategy is run through BOTH the vectorbt path
and the iterative path. They are independent engines with their own
numerics (design doc §4.6), so the assertion is STRUCTURAL — identical
trade count and identical entry/exit timestamps — plus the headline
return within a tight tolerance. This proves the iterative engine's
fills, fees, and equity tracking are sound before any Tier-3 logic is
layered on. If this test fails, the simulator's foundation is wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import StrategySpec, Timeframe
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.metrics import compute_metrics

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 6, 1, tzinfo=UTC)

# Oscillates above 110 (entry) and below 105 (exit) twice — two clean
# round-trip trades, no position open at the final bar.
_CLOSES = [
    100.0, 102.0, 104.0, 106.0, 108.0, 112.0, 118.0, 124.0, 120.0, 114.0,
    108.0, 103.0, 100.0, 98.0, 100.0, 104.0, 108.0, 112.0, 118.0, 124.0,
    120.0, 112.0, 106.0, 102.0, 98.0, 96.0, 100.0, 104.0, 103.0, 101.0,
]


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(_START, periods=n, freq="4h")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": np.full(n, 1e6)},
        index=idx,
    )


def _control_spec() -> StrategySpec:
    price_close: dict[str, Any] = {"kind": "price", "field": "close"}
    spec, _warnings = validate_spec(
        {
            "schema_version": "1.0",
            "name": "Control Static Strategy",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {
                "condition": {
                    "type": "crossover",
                    "series": price_close,
                    "threshold": {"kind": "constant", "value": 110.0},
                    "direction": "above",
                },
                "order_type": "market",
            },
            "exit": {
                "exits": [
                    {
                        "type": "condition",
                        "condition": {
                            "type": "crossover",
                            "series": price_close,
                            "threshold": {"kind": "constant", "value": 105.0},
                            "direction": "below",
                        },
                    },
                ],
            },
            "position_sizing": {"mode": "fixed_quantity", "quantity": 0.5},
            "costs": {"commission_pct": 0.001, "slippage_pct": 0.0},
        },
    )
    return spec


def test_iterative_matches_vectorbt_on_a_static_strategy() -> None:
    spec = _control_spec()
    data = {Timeframe.H4: _ohlcv(_CLOSES)}

    # Non-Tier-3 spec: run_backtest routes it to the vectorbt path.
    vbt = run_backtest(spec, _START, _END, 10_000.0, data_override=data)
    # The iterative engine, called directly on the same static spec.
    itr = run_iterative_backtest(spec, data, _START, _END, 10_000.0)

    # Structural identity — the strong proof. Same trades, same bars.
    assert len(vbt.trades) == len(itr.trades) == 2
    assert [t.entry_time for t in vbt.trades] == [t.entry_time for t in itr.trades]
    assert [t.exit_time for t in vbt.trades] == [t.exit_time for t in itr.trades]

    # Headline metrics within a tight tolerance — the mechanics (fills,
    # fees, equity) are sound even though the float arithmetic differs.
    vm = compute_metrics(vbt, Timeframe.H4)
    im = compute_metrics(itr, Timeframe.H4)
    assert im.num_trades == vm.num_trades == 2
    assert abs(im.total_return_pct - vm.total_return_pct) <= 1e-3 * abs(vm.total_return_pct) + 1e-6
