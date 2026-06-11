"""Data QA validator: gaps reported never filled, duplicates removed and
logged, outliers flagged never deleted, naive timestamps rejected."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from marketmind_workers.ftr.data.quality import validate_ohlcv

from .ftr_helpers import synthetic_ohlcv


def test_gap_reported_never_filled() -> None:
    df = synthetic_ohlcv(n_bars=500, seed=51)
    gapped = pd.concat([df.iloc[:200], df.iloc[230:]])  # 30-bar hole
    clean, report = validate_ohlcv(
        gapped, exchange="binance", symbol="BTC/USDT", timeframe="1h"
    )
    assert len(report.gaps) == 1
    assert report.gaps[0].missing_bars == 30
    # the frame is NOT filled — row count unchanged
    assert len(clean) == len(gapped)


def test_duplicates_removed_and_counted() -> None:
    df = synthetic_ohlcv(n_bars=300, seed=52)
    duped = pd.concat([df, df.iloc[100:110]]).sort_index()
    clean, report = validate_ohlcv(duped, exchange="binance", symbol="BTC/USDT", timeframe="1h")
    assert report.duplicates_removed == 10
    assert len(clean) == 300


def test_outlier_flagged_not_deleted() -> None:
    df = synthetic_ohlcv(n_bars=600, seed=53, vol=0.002)
    df.iloc[400, df.columns.get_loc("close")] *= 1.5  # absurd 50% candle
    clean, report = validate_ohlcv(df, exchange="binance", symbol="BTC/USDT", timeframe="1h")
    assert len(report.outlier_ts) >= 1
    assert len(clean) == 600  # never deleted


def test_naive_timestamps_rejected() -> None:
    df = synthetic_ohlcv(n_bars=50)
    naive = df.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="tz-aware"):
        validate_ohlcv(naive, exchange="binance", symbol="BTC/USDT", timeframe="1h")


def test_cross_venue_divergence_flagged() -> None:
    df = synthetic_ohlcv(n_bars=400, seed=54)
    cross = df["close"] * 1.01  # sustained 100 bps divergence
    _, report = validate_ohlcv(
        df, exchange="binance", symbol="BTC/USDT", timeframe="1h", cross_venue_close=cross
    )
    assert len(report.cross_venue_flags) > 0


def test_misalignment_detected() -> None:
    df = synthetic_ohlcv(n_bars=100, seed=55)
    shifted = df.copy()
    shifted.index = shifted.index + pd.Timedelta(minutes=7)
    _, report = validate_ohlcv(
        shifted, exchange="binance", symbol="BTC/USDT", timeframe="1h"
    )
    assert report.misaligned_bars == 100
    assert not report.passed


def test_outliers_use_log_returns_not_levels() -> None:
    """A monotone exponential trend has constant log returns — no flags."""
    idx = pd.date_range("2026-01-01", periods=400, freq="1h", tz="UTC")
    close = 100.0 * np.exp(0.001 * np.arange(400))
    df = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1.0},
        index=idx,
    )
    _, report = validate_ohlcv(df, exchange="binance", symbol="BTC/USDT", timeframe="1h")
    assert report.outlier_ts == []
