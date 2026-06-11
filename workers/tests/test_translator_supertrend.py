"""Supertrend through the translator + the build_signals path.

Verifies the IndicatorName.SUPERTREND dispatch (both components) and the
parameter-sweep interaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from marketmind_shared.schemas.strategy_spec import Timeframe, validate_spec
from marketmind_workers.backtest.translator import SignalSet, build_signals
from marketmind_workers.overfitting.parameter_sweep import (
    _detect_axes,  # pyright: ignore[reportPrivateUsage]
)

# Flat warmup then a sustained rise — Supertrend turns and stays bullish.
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


def _supertrend_indicator(component: str) -> dict[str, Any]:
    return {
        "kind": "indicator",
        "name": "supertrend",
        "params": {"atr_period": 10, "multiplier": 3.0},
        "component": component,
    }


def _spec(entry_condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "Supertrend translator test",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "1d",
        "direction": "long",
        "entry": {"condition": entry_condition, "order_type": "market"},
        "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
    }


def test_translator_supertrend_direction_component() -> None:
    # Entry while Supertrend direction is bullish (+1).
    spec_dict = _spec(
        {
            "type": "compare",
            "left": _supertrend_indicator("direction"),
            "op": ">",
            "right": {"kind": "constant", "value": 0.0},
        },
    )
    spec, _ = validate_spec(spec_dict)
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool
    assert signals.entries.sum() >= 1  # the rise turns Supertrend bullish


def test_translator_supertrend_value_component() -> None:
    # Entry while close is above the Supertrend line.
    spec_dict = _spec(
        {
            "type": "compare",
            "left": {"kind": "price", "field": "close"},
            "op": ">",
            "right": _supertrend_indicator("value"),
        },
    )
    spec, _ = validate_spec(spec_dict)
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool
    assert signals.entries.sum() >= 1


def test_detect_axes_does_not_sweep_supertrend_params() -> None:
    # Finding: _detect_axes sweeps only sma/ema/rsi/wma `period` (plus
    # stop/TP percents and RSI thresholds). Supertrend's atr_period /
    # multiplier are NOT auto-swept — consistent with atr / bollinger /
    # macd / highest / lowest also being un-swept. The stop-loss percent
    # IS still detected, so the sweep functions for the spec at large.
    spec_dict = _spec(
        {
            "type": "compare",
            "left": _supertrend_indicator("direction"),
            "op": ">",
            "right": {"kind": "constant", "value": 0.0},
        },
    )
    axes = _detect_axes(spec_dict)
    swept_paths = [p for axis in axes for p in axis.target_paths]
    assert all("atr_period" not in p and "multiplier" not in p for p in swept_paths)
    # The 5% stop-loss is still a detected sweep axis.
    assert any("method" in p and "value" in p for p in swept_paths)
