"""ADX through the translator + the build_signals path + sweep detection."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
from marketmind_shared.schemas.strategy_spec import Timeframe, validate_spec
from marketmind_workers.backtest.translator import SignalSet, build_signals
from marketmind_workers.overfitting.parameter_sweep import (
    _detect_axes,  # pyright: ignore[reportPrivateUsage]
)

# Volatile then flat — ADX rises then falls.
_CLOSES: list[float] = [100.0 + 5.0 * i for i in range(60)] + [400.0] * 60


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


def _spec_adx_above_25() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "ADX translator test",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "1d",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "compare",
                "left": {"kind": "indicator", "name": "adx", "params": {"period": 14}},
                "op": ">",
                "right": {"kind": "constant", "value": 25.0},
            },
            "order_type": "market",
        },
        "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
    }


def test_translator_adx_builds_signals() -> None:
    spec, _ = validate_spec(_spec_adx_above_25())
    signals = build_signals(spec, _ohlcv())
    assert isinstance(signals, SignalSet)
    assert signals.entries.dtype == bool
    # The trending run pushes ADX above 25 in the middle of the series.
    assert signals.entries.sum() >= 1


def test_detect_axes_sweeps_adx_period() -> None:
    axes = _detect_axes(_spec_adx_above_25())
    swept_paths = [p for axis in axes for p in axis.target_paths]
    # adx.period IS now detected as a sweep axis (v1.1 per-indicator widening).
    assert any("adx" in p or ("params" in p and "period" in p) for p in swept_paths)
