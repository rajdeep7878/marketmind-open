"""Correctness tests for the indicator engine.

Each indicator is tested against a small hand-computed fixture: where
the formula is unambiguous (SMA, WMA, returns, rolling stddev/highest/
lowest) we assert exact values; where the smoothing depends on a
library's choice of recursion (EMA, RSI, ATR, MACD, Stochastic) we
assert known boundary behaviour and shape.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from marketmind_workers.backtest import indicators as ind


def _ramp(n: int = 30) -> pd.DataFrame:
    """A monotonically-rising synthetic OHLCV series: close = 1..n."""
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="1D")
    close = np.arange(1, n + 1, dtype=float)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _from_closes(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Build an OHLCV frame from a list of closing prices."""
    n = len(closes)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="1D")
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": (np.asarray(volumes, dtype=float) if volumes is not None else np.ones(n)),
        },
        index=idx,
    )


# ---- SMA -------------------------------------------------------------------


def test_sma_basic() -> None:
    df = _from_closes([1, 2, 3, 4, 5])
    out = ind.sma(df, 3)
    # Period-3 SMA: first two NaN, then (1+2+3)/3, (2+3+4)/3, (3+4+5)/3
    assert math.isnan(out.iloc[0])
    assert math.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_sma_constant_series() -> None:
    df = _from_closes([5.0] * 10)
    out = ind.sma(df, 4)
    assert out.iloc[-1] == pytest.approx(5.0)


# ---- WMA -------------------------------------------------------------------


def test_wma_basic() -> None:
    df = _from_closes([1, 2, 3])
    # WMA(3) = (1*1 + 2*2 + 3*3) / (1+2+3) = (1+4+9)/6 = 14/6
    out = ind.wma(df, 3)
    assert out.iloc[-1] == pytest.approx(14 / 6)


def test_wma_weighting_skews_toward_latest() -> None:
    # If WMA weighted equally we'd get 5.0; the latest-heavy WMA must
    # be higher when latest >> oldest.
    df = _from_closes([1, 1, 9])
    wma3 = ind.wma(df, 3).iloc[-1]
    sma3 = ind.sma(df, 3).iloc[-1]
    assert wma3 > sma3


# ---- EMA -------------------------------------------------------------------


def test_ema_converges_to_constant() -> None:
    df = _from_closes([100.0] * 50)
    out = ind.ema(df, 10)
    # After plenty of warmup, EMA of a constant series IS the constant.
    assert out.iloc[-1] == pytest.approx(100.0)


def test_ema_responds_to_recent_data() -> None:
    df = _from_closes([1.0] * 20 + [10.0] * 20)
    out = ind.ema(df, 5)
    # Mid-series the value is between 1 and 10
    mid = out.iloc[25]
    assert 1.0 < mid < 10.0


# ---- RSI -------------------------------------------------------------------


def test_rsi_pure_uptrend_equals_100() -> None:
    df = _ramp(30)
    out = ind.rsi(df, 14)
    # No down days -> avg loss == 0 -> RSI = 100
    assert out.iloc[-1] == pytest.approx(100.0)


def test_rsi_pure_downtrend_equals_zero() -> None:
    df = _from_closes(list(range(30, 0, -1)))
    out = ind.rsi(df, 14)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_rsi_warmup_is_nan() -> None:
    df = _from_closes([1, 2, 3, 4])
    out = ind.rsi(df, 14)
    # First 14 bars don't have enough data; result is NaN.
    assert math.isnan(out.iloc[0])


# ---- MACD ------------------------------------------------------------------


def test_macd_returns_three_columns() -> None:
    df = _ramp(60)
    out = ind.macd(df, fast=12, slow=26, signal=9)
    assert set(out.columns) == {"line", "signal", "hist"}


def test_macd_hist_equals_line_minus_signal() -> None:
    df = _ramp(60)
    out = ind.macd(df, fast=12, slow=26, signal=9).iloc[-1]
    assert out["hist"] == pytest.approx(out["line"] - out["signal"], rel=1e-6)


# ---- Stochastic ------------------------------------------------------------


def test_stochastic_columns() -> None:
    df = _ramp(30)
    out = ind.stochastic(df, k=14, d=3, smooth=3)
    assert set(out.columns) == {"k", "d"}


def test_stochastic_pure_uptrend_caps_high() -> None:
    df = _ramp(30)
    k_last = ind.stochastic(df, k=14, d=3, smooth=3)["k"].iloc[-1]
    # _ramp has high = close + 0.5 / low = close - 0.5, so over the 14-bar
    # window highest_high == close[-1] + 0.5 and lowest_low == close[-14] - 0.5.
    # Raw %K = 100 * (close - lowest_low) / (highest_high - lowest_low)
    # which for our ramp evaluates to ~96.43, then `ta` smooths that with
    # an inner window. Either way, in a pure uptrend %K should be high.
    assert k_last > 90.0


# ---- ATR -------------------------------------------------------------------


def test_atr_zero_for_flat_series() -> None:
    df = _from_closes([5.0] * 20)
    # Override OHLC so high == low == close — true range is 0 every bar
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["open"] = df["close"]
    out = ind.atr(df, 14)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_atr_positive_for_volatile_series() -> None:
    df = _ramp(30)
    out = ind.atr(df, 14)
    assert out.iloc[-1] > 0


# ---- Bollinger -------------------------------------------------------------


def test_bollinger_middle_equals_sma() -> None:
    df = _ramp(50)
    bb = ind.bollinger(df, period=20, std_dev=2.0)
    sma20 = ind.sma(df, 20)
    assert bb["middle"].iloc[-1] == pytest.approx(sma20.iloc[-1], rel=1e-9)


def test_bollinger_upper_above_lower() -> None:
    df = _ramp(50)
    bb = ind.bollinger(df, period=20, std_dev=2.0).iloc[-1]
    assert bb["upper"] > bb["middle"] > bb["lower"]


# ---- Stddev / volume_sma / highest / lowest / returns ----------------------


def test_stddev_constant_series_is_zero() -> None:
    df = _from_closes([5.0] * 10)
    out = ind.stddev(df, 5)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_volume_sma_basic() -> None:
    df = _from_closes([1, 2, 3, 4], volumes=[10, 20, 30, 40])
    out = ind.volume_sma(df, 2)
    # last = (30+40)/2 = 35
    assert out.iloc[-1] == pytest.approx(35.0)


def test_highest_rolling_max_from_high() -> None:
    df = _from_closes([1, 5, 3, 8, 2])
    df["high"] = df["close"]  # source-of-truth for "highest"
    out = ind.highest(df, 3, source="high")
    # Period-3 rolling max: [NaN, NaN, max(1,5,3)=5, max(5,3,8)=8, max(3,8,2)=8]
    assert out.iloc[2] == pytest.approx(5.0)
    assert out.iloc[3] == pytest.approx(8.0)
    assert out.iloc[4] == pytest.approx(8.0)


def test_lowest_rolling_min_from_low() -> None:
    df = _from_closes([1, 5, 3, 8, 2])
    df["low"] = df["close"]
    out = ind.lowest(df, 3, source="low")
    assert out.iloc[2] == pytest.approx(1.0)
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(2.0)


def test_returns_1_period() -> None:
    df = _from_closes([100, 110, 99])
    out = ind.returns(df, 1)
    assert math.isnan(out.iloc[0])
    assert out.iloc[1] == pytest.approx(0.1)
    assert out.iloc[2] == pytest.approx(99 / 110 - 1)


# ---- OBV -------------------------------------------------------------------


def test_obv_simple_sequence() -> None:
    # OBV: cumulative volume signed by close-vs-prev-close. The `ta`
    # library seeds the first bar with the first volume (not 0) because
    # there's no prior bar to sign against — different libraries differ
    # by a constant offset here, so we assert the DELTAS between bars,
    # which are unambiguous.
    df = _from_closes([10, 11, 12, 11], volumes=[100, 200, 300, 400])
    out = ind.obv(df)
    # Bar 1: close UP -> delta = +volume[1] = +200
    # Bar 2: close UP -> delta = +volume[2] = +300
    # Bar 3: close DOWN -> delta = -volume[3] = -400
    assert out.iloc[1] - out.iloc[0] == pytest.approx(200.0)
    assert out.iloc[2] - out.iloc[1] == pytest.approx(300.0)
    assert out.iloc[3] - out.iloc[2] == pytest.approx(-400.0)


# ---- VWAP ------------------------------------------------------------------


def test_vwap_session_anchored_resets_at_utc_midnight() -> None:
    # Build a 2-day, 4-bar/day series. Session VWAP at the LAST bar of
    # each day depends only on that day's data.
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=8, freq="6h")
    closes = [100.0, 101.0, 102.0, 103.0, 50.0, 51.0, 52.0, 53.0]
    vols = [1.0] * 8
    df = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": vols,
        },
        index=idx,
    )
    out = ind.vwap(df, session_anchored=True)
    # First-day last bar: mean of [100,101,102,103] = 101.5
    assert out.iloc[3] == pytest.approx(101.5)
    # Second-day last bar: mean of [50,51,52,53] = 51.5 (NOT carrying over)
    assert out.iloc[7] == pytest.approx(51.5)


def test_vwap_non_session_anchored_is_cumulative() -> None:
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=4, freq="1D")
    closes = [10.0, 20.0, 30.0, 40.0]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * 4,
        },
        index=idx,
    )
    out = ind.vwap(df, session_anchored=False)
    # Cumulative mean over all bars
    assert out.iloc[-1] == pytest.approx(25.0)


# ---- Candle patterns -------------------------------------------------------


def test_doji_detects_near_equal_open_close() -> None:
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=3, freq="1D")
    df = pd.DataFrame(
        {
            # Bar 0: clear doji (open == close, tall wicks)
            # Bar 1: clearly bullish
            # Bar 2: another doji
            "open": [100.0, 100.0, 50.0],
            "high": [110.0, 105.0, 60.0],
            "low": [90.0, 99.0, 40.0],
            "close": [100.0, 105.0, 50.0],
            "volume": [1.0, 1.0, 1.0],
        },
        index=idx,
    )
    out = ind.doji(df)
    assert bool(out.iloc[0]) is True
    assert bool(out.iloc[1]) is False
    assert bool(out.iloc[2]) is True


def test_bullish_engulfing_detected() -> None:
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=2, freq="1D")
    # Prev: open=105, close=100 (bearish). Current: open=99, close=106
    # (bullish, body 99..106 engulfs prev body 100..105).
    df = pd.DataFrame(
        {
            "open": [105.0, 99.0],
            "high": [106.0, 107.0],
            "low": [99.0, 98.0],
            "close": [100.0, 106.0],
            "volume": [1.0, 1.0],
        },
        index=idx,
    )
    out = ind.bullish_engulfing(df)
    assert bool(out.iloc[1]) is True


def test_bearish_engulfing_detected() -> None:
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=2, freq="1D")
    df = pd.DataFrame(
        {
            "open": [100.0, 106.0],
            "high": [106.0, 107.0],
            "low": [99.0, 98.0],
            "close": [105.0, 99.0],
            "volume": [1.0, 1.0],
        },
        index=idx,
    )
    out = ind.bearish_engulfing(df)
    assert bool(out.iloc[1]) is True


def test_hammer_pattern() -> None:
    # Bullish hammer: tiny body near top, long lower wick
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=1, freq="1D")
    df = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],  # small upper wick (1)
            "low": [90.0],  # long lower wick (10)
            "close": [101.0],
            "volume": [1.0],
        },
        index=idx,
    )
    # body = |close - open| = 1, lower_wick = 10, upper_wick = 0
    # lower_wick (10) >= 2 * body (1) AND upper_wick (0) <= body (1) -> True
    out = ind.hammer(df)
    assert bool(out.iloc[0]) is True


def test_shooting_star_pattern() -> None:
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=1, freq="1D")
    df = pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],  # long upper wick (10)
            "low": [99.0],  # small lower wick (1)
            "close": [99.0],
            "volume": [1.0],
        },
        index=idx,
    )
    out = ind.shooting_star(df)
    assert bool(out.iloc[0]) is True
