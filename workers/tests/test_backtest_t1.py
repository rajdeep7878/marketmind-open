"""A.3a — Tier-1 (bounded-window stateful) backtest verification.

T1 conditions — within_last_n_bars, rising, falling — depend on a fixed
N-bar lookback. They were already vectorised in v1 (rolling / shift);
A.3a formalises them with explicit multi-bar-lookback tests that assert
each fires on exactly the bars it should, against hand-built series.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import Timeframe
from marketmind_workers.backtest.translator import build_signals


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="4h")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": np.full(n, 1e6)},
        index=idx,
    )


def _spec(entry_condition: dict[str, Any]) -> Any:
    spec, _warnings = validate_spec(
        {
            "schema_version": "1.0",
            "name": "T1 Test",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {"condition": entry_condition, "order_type": "market"},
            "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
        },
    )
    return spec


_PRICE_CLOSE = {"kind": "price", "field": "close"}


def test_within_last_n_bars_fires_across_the_lookback_window() -> None:
    # A single crossover above 150 fires on exactly one bar; wrapping it
    # in within_last_n_bars(n=3) must extend the True to that bar + 2.
    closes = [100.0, 110.0, 120.0, 130.0, 140.0, 145.0, 155.0, 160.0, 165.0, 170.0, 175.0, 180.0]
    cross_cond = {
        "type": "crossover",
        "series": _PRICE_CLOSE,
        "threshold": {"kind": "constant", "value": 150.0},
        "direction": "above",
    }
    spec = _spec({"type": "within_last_n_bars", "condition": cross_cond, "n": 3})
    entries = build_signals(spec, {Timeframe.H4: _ohlcv(closes)}).entries

    fired = [i for i, v in enumerate(entries.to_numpy()) if v]
    # close crosses 150 between bar 5 (145) and bar 6 (155): the raw
    # crossover is True only at bar 6; within_last_n_bars(3) -> 6, 7, 8.
    assert fired == [6, 7, 8]


def test_within_last_n_bars_n1_equals_the_raw_condition() -> None:
    closes = [100.0, 110.0, 120.0, 130.0, 140.0, 145.0, 155.0, 160.0, 165.0, 170.0]
    cross_cond = {
        "type": "crossover",
        "series": _PRICE_CLOSE,
        "threshold": {"kind": "constant", "value": 150.0},
        "direction": "above",
    }
    spec = _spec({"type": "within_last_n_bars", "condition": cross_cond, "n": 1})
    entries = build_signals(spec, {Timeframe.H4: _ohlcv(closes)}).entries
    assert [i for i, v in enumerate(entries.to_numpy()) if v] == [6]


def test_rising_tracks_a_multi_bar_lookback() -> None:
    # Rise for 6 bars, then fall. rising(lookback=2): close[t] >= close[t-2].
    closes = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 105.0, 100.0, 95.0, 90.0]
    spec = _spec({"type": "rising", "series": _PRICE_CLOSE, "lookback": 2, "strict": True})
    entries = build_signals(spec, {Timeframe.H4: _ohlcv(closes)}).entries.to_numpy()
    # bars 0-1 have no t-2 (NaN -> False). bars 2..5 are strictly rising
    # vs t-2. bars 6+ are falling vs t-2.
    assert list(entries) == [
        False, False, True, True, True, True, False, False, False, False,
    ]


def test_falling_tracks_a_multi_bar_lookback() -> None:
    closes = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 95.0, 100.0, 105.0, 110.0]
    spec = _spec({"type": "falling", "series": _PRICE_CLOSE, "lookback": 2, "strict": True})
    entries = build_signals(spec, {Timeframe.H4: _ohlcv(closes)}).entries.to_numpy()
    assert list(entries) == [
        False, False, True, True, True, True, False, False, False, False,
    ]
