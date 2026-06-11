"""PSAR through the translator + the build_signals path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import Timeframe, validate_spec
from marketmind_workers.backtest.translator import SignalSet, build_signals
from marketmind_workers.overfitting.parameter_sweep import (
    _detect_axes,  # pyright: ignore[reportPrivateUsage]
)

# ta's PSARIndicator triggers a pandas FutureWarning — out of our hands.
pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")

_CLOSES: list[float] = [100.0] * 30 + [100.0 + 3.0 * i for i in range(120)]


def _ohlcv() -> dict[Timeframe, pd.DataFrame]:
    n = len(_CLOSES)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="1D")
    c = np.array(_CLOSES, dtype=float)
    df = pd.DataFrame(
        {
            "open": c - 0.1,
            "high": c + 0.5,
            "low": c - 0.5,
            "close": c,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    return {Timeframe.D1: df}


def _spec_psar(component: str, op: str, threshold: dict[str, Any] | None = None) -> dict[str, Any]:
    if component == "direction":
        right = threshold or {"kind": "constant", "value": 0.0}
        left = {"kind": "indicator", "name": "psar",
                "params": {"step": 0.02, "max_step": 0.2}, "component": "direction"}
    else:
        left = {"kind": "price", "field": "close"}
        right = {"kind": "indicator", "name": "psar",
                 "params": {"step": 0.02, "max_step": 0.2}, "component": "value"}
    return {
        "schema_version": "1.0",
        "name": "PSAR translator test",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "1d",
        "direction": "long",
        "entry": {
            "condition": {"type": "compare", "left": left, "op": op, "right": right},
            "order_type": "market",
        },
        "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
    }


def test_translator_psar_direction_component() -> None:
    spec, _ = validate_spec(_spec_psar("direction", ">"))
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool
    assert signals.entries.sum() >= 1


def test_translator_psar_value_component() -> None:
    spec, _ = validate_spec(_spec_psar("value", ">"))
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool
    assert signals.entries.sum() >= 1


def test_detect_axes_does_not_sweep_psar_params() -> None:
    # PSAR's step / max_step are NOT period-like; the narrow per-indicator
    # widening does NOT detect them — consistent with Supertrend's
    # multiplier and Bollinger's std_dev.
    axes = _detect_axes(_spec_psar("direction", ">"))
    swept_paths = [p for axis in axes for p in axis.target_paths]
    assert all("step" not in p and "max_step" not in p for p in swept_paths)
