"""Correctness tests for the ADX indicator (v1.1 ta-backed addition).

ADX wraps `ta.trend.ADXIndicator` — the reference test is a bit-for-bit
match against a direct `ta` call (our impl is a thin wrapper; the test
guards against the wrapping itself drifting).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from marketmind_workers.backtest import indicators as ind
from ta.trend import ADXIndicator


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


def test_adx_shape() -> None:
    df = _ohlcv([100.0 + i for i in range(60)])
    a = ind.adx(df, 14)
    assert isinstance(a, pd.Series)
    assert len(a) == 60
    assert not math.isnan(a.iloc[-1])


def test_adx_warmup_tracks_ta() -> None:
    df = _ohlcv([100.0 + i for i in range(60)])
    ours_nan = ind.adx(df, 14).isna()
    ref_nan = ADXIndicator(
        high=df["high"], low=df["low"], close=df["close"], window=14, fillna=False,
    ).adx().isna()
    assert (ours_nan == ref_nan).all()


def test_adx_value_range_is_0_to_100() -> None:
    df = _ohlcv([100.0] * 15 + [100.0 + 5.0 * i for i in range(60)])
    a = ind.adx(df, 14).dropna()
    assert (a >= 0.0).all()
    assert (a <= 100.0).all()


def test_adx_matches_ta_reference_bit_for_bit() -> None:
    rng = np.random.default_rng(42)
    closes = list(100.0 + np.cumsum(rng.normal(0.0, 2.0, size=200)))
    df = _ohlcv(closes, spread=1.0)
    ours = ind.adx(df, 14)
    ref = ADXIndicator(
        high=df["high"], low=df["low"], close=df["close"], window=14, fillna=False,
    ).adx()
    pd.testing.assert_series_equal(ours, ref, check_names=False)
