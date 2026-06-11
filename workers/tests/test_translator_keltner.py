"""Keltner Channels through the translator + the build_signals path."""

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


def _spec_keltner_breakout(component: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "Keltner translator test",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "1d",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "compare",
                "left": {"kind": "price", "field": "close"},
                "op": ">",
                "right": {
                    "kind": "indicator",
                    "name": "keltner",
                    "params": {"period": 20, "atr_period": 10, "multiplier": 2.0},
                    "component": component,
                },
            },
            "order_type": "market",
        },
        "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
    }


def test_translator_keltner_upper_component() -> None:
    spec, _ = validate_spec(_spec_keltner_breakout("upper"))
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool
    assert signals.entries.sum() >= 1  # the rise breaks the upper band


def test_translator_keltner_middle_component() -> None:
    spec, _ = validate_spec(_spec_keltner_breakout("middle"))
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool


def test_detect_axes_sweeps_keltner_period() -> None:
    axes = _detect_axes(_spec_keltner_breakout("upper"))
    swept_paths = [p for axis in axes for p in axis.target_paths]
    # keltner.period IS detected (v1.1 per-indicator widening).
    assert any("params" in p and "period" in p for p in swept_paths)
    # atr_period / multiplier remain un-swept by the narrow widening.
    assert not any("atr_period" in p for p in swept_paths)
    assert not any("multiplier" in p for p in swept_paths)
