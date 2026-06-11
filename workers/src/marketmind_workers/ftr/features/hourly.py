"""Hourly OHLCV feature pipeline (mandate Stage 2) — config-driven.

Single pipeline module: every feature at bar t uses data <= close of t,
built exclusively through ``ftr.features.shifting`` helpers and rolling
windows that END at t.

Weekday convention: pandas ``Monday=0 .. Sunday=6`` everywhere in FTR
(the repo's WeekdayFilter ISO convention is NOT used here — see the §3
footgun note in docs/INTEGRATION_PLAN.md).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, cast

import numpy as np
import pandas as pd

from marketmind_workers.ftr.data.ohlcv import dtindex
from marketmind_workers.ftr.features.shifting import lagged_log_return


def col(df: pd.DataFrame, name: str) -> pd.Series:
    """Typed single-column accessor (pyright-strict narrowing)."""
    s = df[name]
    assert isinstance(s, pd.Series)
    return s


@dataclass(frozen=True)
class HourlyFeatureConfig:
    return_lags: tuple[int, ...] = (1, 2, 3, 6, 12, 24, 48)
    vol_windows: tuple[int, ...] = (24, 72, 168)
    atr_window: int = 14
    rsi_window: int = 14
    macd: tuple[int, int, int] = (12, 26, 9)
    bollinger_window: int = 20
    bollinger_k: float = 2.0
    donchian_window: int = 55
    volume_z_windows: tuple[int, ...] = (24, 168)
    range_z_window: int = 168
    ema_fast: int = 50
    ema_slow: int = 200

    def config_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=list)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _ema(s: pd.Series, span: int) -> pd.Series:
    out = s.ewm(span=span, adjust=False).mean()
    assert isinstance(out, pd.Series)
    return out


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / window, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    assert isinstance(out, pd.Series)
    return out


def _atr(df: pd.DataFrame, window: int) -> pd.Series:
    high, low, close = col(df, "high"), col(df, "low"), col(df, "close")
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    assert isinstance(tr, pd.Series)
    out = tr.ewm(alpha=1.0 / window, adjust=False).mean()
    assert isinstance(out, pd.Series)
    return out


def _zscore(s: pd.Series, window: int) -> pd.Series:
    mean = s.rolling(window).mean()
    std = s.rolling(window).std()
    return (s - mean) / std.replace(0.0, np.nan)


def compute_hourly_features(
    df: pd.DataFrame, config: HourlyFeatureConfig | None = None
) -> pd.DataFrame:
    """Feature matrix for an hourly OHLCV frame. Index preserved; NaN warmup
    rows are retained (the split machinery drops them), never filled."""
    cfg = config or HourlyFeatureConfig()
    close = col(df, "close")
    out: dict[str, pd.Series] = {}

    for k in cfg.return_lags:
        out[f"logret_{k}"] = lagged_log_return(close, k)

    logret1 = lagged_log_return(close, 1)
    for w in cfg.vol_windows:
        rvol = logret1.rolling(w).std()
        assert isinstance(rvol, pd.Series)
        out[f"rvol_{w}"] = rvol

    atr = _atr(df, cfg.atr_window)
    out["atr_over_close"] = atr / close

    out["rsi"] = _rsi(close, cfg.rsi_window)

    f, s, sig = cfg.macd
    macd_line = _ema(close, f) - _ema(close, s)
    assert isinstance(macd_line, pd.Series)
    macd_hist = macd_line - _ema(macd_line, sig)
    assert isinstance(macd_hist, pd.Series)
    out["macd_hist"] = macd_hist

    bb_mid = close.rolling(cfg.bollinger_window).mean()
    bb_std = close.rolling(cfg.bollinger_window).std()
    upper = bb_mid + cfg.bollinger_k * bb_std
    lower = bb_mid - cfg.bollinger_k * bb_std
    width = upper - lower
    assert isinstance(width, pd.Series) and isinstance(lower, pd.Series)
    out["bb_pct_b"] = (close - lower) / width.replace(0.0, np.nan)

    don_hi = col(df, "high").rolling(cfg.donchian_window).max()
    don_lo = col(df, "low").rolling(cfg.donchian_window).min()
    don_width = don_hi - don_lo
    assert isinstance(don_width, pd.Series)
    out["donchian_pos"] = (close - don_lo) / don_width.replace(0.0, np.nan)

    for w in cfg.volume_z_windows:
        out[f"volume_z_{w}"] = _zscore(col(df, "volume"), w)
    out["range_z"] = _zscore(col(df, "high") - col(df, "low"), cfg.range_z_window)

    ema_fast = _ema(close, cfg.ema_fast)
    ema_slow = _ema(close, cfg.ema_slow)
    atr_safe = atr.replace(0.0, np.nan)
    out["dist_ema_fast_atr"] = (close - ema_fast) / atr_safe
    out["dist_ema_slow_atr"] = (close - ema_slow) / atr_safe
    out["regime_fast_above_slow"] = (ema_fast > ema_slow).astype("float64")

    # Cyclic time encodings. pandas weekday: Monday=0 .. Sunday=6.
    idx = dtindex(df)
    # cast: the bundled pandas stubs miss .hour/.weekday on DatetimeIndex
    idx_any = cast("Any", idx)
    hour = np.asarray(idx_any.hour, dtype="float64")
    weekday = np.asarray(idx_any.weekday, dtype="float64")
    out["hod_sin"] = pd.Series(np.sin(2 * np.pi * hour / 24.0), index=idx)
    out["hod_cos"] = pd.Series(np.cos(2 * np.pi * hour / 24.0), index=idx)
    out["dow_sin"] = pd.Series(np.sin(2 * np.pi * weekday / 7.0), index=idx)
    out["dow_cos"] = pd.Series(np.cos(2 * np.pi * weekday / 7.0), index=idx)

    return pd.DataFrame(out, index=idx)


def atr_h_bps(df: pd.DataFrame, horizon: int, *, atr_window: int = 14) -> pd.Series:
    """ATR-implied expected absolute move over H bars, in bps of close.

    Diffusion scaling: |move over H bars| ~ ATR(1-bar) * sqrt(H). Used by the
    deterministic E[|move|] estimate in the ML strategy's EV gate (the ``k``
    calibration multiplies this).
    """
    atr = _atr(df, atr_window)
    return (atr / col(df, "close")) * np.sqrt(float(horizon)) * 1e4
