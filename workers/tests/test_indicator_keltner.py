"""Correctness tests for the Keltner Channels indicator (v1.1, ta-backed).

Wraps `ta.volatility.KeltnerChannel` with original_version=False (the
modern EMA-based variant). Reference test asserts bit-identical match.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from marketmind_workers.backtest import indicators as ind
from ta.volatility import KeltnerChannel


def _ohlcv(closes: list[float], spread: float = 0.5) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="1D")
    c = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": c,
            "high": c + spread,
            "low": c - spread,
            "close": c,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def test_keltner_shape() -> None:
    df = _ohlcv([100.0 + i for i in range(60)])
    k = ind.keltner(df, period=20, atr_period=10, multiplier=2.0)
    assert list(k.columns) == ["upper", "middle", "lower"]
    assert len(k) == 60
    assert not math.isnan(k["upper"].iloc[-1])


def test_keltner_band_ordering() -> None:
    # Upper >= middle >= lower at every non-NaN bar — by definition.
    df = _ohlcv([100.0 + i for i in range(60)])
    k = ind.keltner(df, period=20, atr_period=10, multiplier=2.0).dropna()
    assert (k["upper"] >= k["middle"]).all()
    assert (k["middle"] >= k["lower"]).all()


def test_keltner_matches_ta_reference_bit_for_bit() -> None:
    rng = np.random.default_rng(42)
    closes = list(100.0 + np.cumsum(rng.normal(0.0, 2.0, size=200)))
    df = _ohlcv(closes, spread=1.0)
    ours = ind.keltner(df, period=20, atr_period=10, multiplier=2.0)
    ref = KeltnerChannel(
        high=df["high"], low=df["low"], close=df["close"],
        window=20, window_atr=10, multiplier=2,  # type: ignore[arg-type]
        fillna=False, original_version=False,
    )
    pd.testing.assert_series_equal(
        ours["upper"], ref.keltner_channel_hband(), check_names=False,
    )
    pd.testing.assert_series_equal(
        ours["middle"], ref.keltner_channel_mband(), check_names=False,
    )
    pd.testing.assert_series_equal(
        ours["lower"], ref.keltner_channel_lband(), check_names=False,
    )
