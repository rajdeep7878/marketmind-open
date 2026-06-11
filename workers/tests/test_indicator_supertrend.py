"""Correctness tests for the Supertrend indicator (v1.1 whitelist addition).

No library in the stack ships a Supertrend, so the reference is an
independent naive transcription of the canonical recursion (`_naive`)
plus behavioural assertions on flat-then-step fixtures. A Supertrend
only flips on a real break of its band — a gentle linear ramp does not
move it — so the fixtures step sharply.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from marketmind_workers.backtest import indicators as ind


def _ohlcv(closes: list[float], spread: float = 0.1) -> pd.DataFrame:
    """OHLCV frame from a close path; high/low are close ± spread."""
    n = len(closes)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="1D")
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _naive_supertrend(
    data: pd.DataFrame, atr_period: int, multiplier: float,
) -> tuple[list[float], list[float]]:
    """Independent, deliberately-slow reference — a plain transcription of
    the canonical Supertrend recursion. Reuses ind.atr (ATR is tested
    separately); only the Supertrend recursion is re-derived here.
    """
    atr_vals = ind.atr(data, atr_period).tolist()
    highs = data["high"].tolist()
    lows = data["low"].tolist()
    closes = data["close"].tolist()
    n = len(closes)
    fu = [math.nan] * n
    fl = [math.nan] * n
    value = [math.nan] * n
    direction = [math.nan] * n
    last: int | None = None
    for t in range(n):
        a = atr_vals[t]
        if a != a:  # NaN
            continue
        hl2 = (highs[t] + lows[t]) / 2.0
        basic_u = hl2 + multiplier * a
        basic_l = hl2 - multiplier * a
        if last is None:
            fu[t], fl[t] = basic_u, basic_l
            up = closes[t] > hl2
        else:
            fu[t] = (
                basic_u
                if basic_u < fu[last] or closes[last] > fu[last]
                else fu[last]
            )
            fl[t] = (
                basic_l
                if basic_l > fl[last] or closes[last] < fl[last]
                else fl[last]
            )
            up = closes[t] > fu[t] if direction[last] == -1.0 else closes[t] >= fl[t]
        direction[t] = 1.0 if up else -1.0
        value[t] = fl[t] if up else fu[t]
        last = t
    return value, direction


def test_supertrend_shape() -> None:
    df = _ohlcv([100.0 + i for i in range(60)])
    st = ind.supertrend(df, atr_period=10, multiplier=3.0)
    assert list(st.columns) == ["value", "direction"]
    assert len(st) == 60
    assert not math.isnan(st["direction"].iloc[-1])
    assert not math.isnan(st["value"].iloc[-1])


def test_supertrend_warmup_tracks_atr() -> None:
    # Supertrend is defined exactly on the bars where its ATR is defined —
    # whatever the `ta` library's ATR warmup turns out to be.
    df = _ohlcv([100.0 + i for i in range(60)])
    atr_nan = ind.atr(df, 10).isna()
    st = ind.supertrend(df, atr_period=10, multiplier=3.0)
    assert (st["direction"].isna() == atr_nan).all()
    assert (st["value"].isna() == atr_nan).all()


def test_supertrend_direction_is_plus_or_minus_one() -> None:
    df = _ohlcv([100.0, 102.0, 98.0, 105.0, 95.0] * 12)
    direction = ind.supertrend(df, atr_period=10, multiplier=3.0)["direction"]
    non_nan = direction.dropna()
    assert len(non_nan) > 0
    assert set(non_nan.unique()) <= {1.0, -1.0}


def test_supertrend_uptrend_is_bullish() -> None:
    # Flat warmup, then a sharp sustained rise — breaks the upper band.
    closes = [100.0] * 14 + [100.0 + 5.0 * (i + 1) for i in range(46)]
    st = ind.supertrend(_ohlcv(closes), atr_period=10, multiplier=3.0)
    assert st["direction"].iloc[-1] == 1.0
    # In an uptrend the line is the lower band — below price.
    assert st["value"].iloc[-1] < closes[-1]


def test_supertrend_downtrend_is_bearish() -> None:
    closes = [300.0] * 14 + [300.0 - 5.0 * (i + 1) for i in range(46)]
    st = ind.supertrend(_ohlcv(closes), atr_period=10, multiplier=3.0)
    assert st["direction"].iloc[-1] == -1.0
    # In a downtrend the line is the upper band — above price.
    assert st["value"].iloc[-1] > closes[-1]


def test_supertrend_direction_flips() -> None:
    # Rise then fall — direction must contain both states.
    closes = (
        [100.0] * 14
        + [100.0 + 5.0 * i for i in range(24)]
        + [220.0 - 5.0 * i for i in range(24)]
    )
    direction = ind.supertrend(_ohlcv(closes), atr_period=10, multiplier=3.0)["direction"]
    seen = set(direction.dropna().unique())
    assert seen == {1.0, -1.0}


def test_supertrend_matches_naive_reference() -> None:
    # Pseudo-random walk — the production impl must match the independent
    # naive transcription bar-for-bar.
    rng = np.random.default_rng(42)
    closes = list(100.0 + np.cumsum(rng.normal(0.0, 2.0, size=200)))
    df = _ohlcv(closes, spread=0.5)
    st = ind.supertrend(df, atr_period=14, multiplier=3.0)
    ref_value, ref_direction = _naive_supertrend(df, atr_period=14, multiplier=3.0)

    np.testing.assert_allclose(
        st["value"].to_numpy(), np.array(ref_value), rtol=1e-12, equal_nan=True,
    )
    np.testing.assert_array_equal(st["direction"].to_numpy(), np.array(ref_direction))
