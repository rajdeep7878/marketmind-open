"""Shared synthetic-data helpers for FTR tests.

Synthetic OHLCV is FIXTURE data (seeded GBM) — used only to exercise code
paths, never for performance claims.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd


def synthetic_ohlcv(
    *,
    n_bars: int = 2000,
    bar_hours: int = 1,
    start: datetime | None = None,
    seed: int = 7,
    drift: float = 0.0,
    vol: float = 0.01,
) -> pd.DataFrame:
    """Seeded GBM OHLCV frame with UTC tz-aware open-time index."""
    rng = np.random.default_rng(seed)
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    idx = pd.date_range(start, periods=n_bars, freq=f"{bar_hours}h", tz="UTC")
    rets = rng.normal(drift, vol, n_bars)
    close = 50_000.0 * np.exp(np.cumsum(rets))
    open_ = np.empty(n_bars)
    open_[0] = 50_000.0
    open_[1:] = close[:-1]
    spread = np.abs(rng.normal(0, vol / 2, n_bars))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = rng.lognormal(3.0, 1.0, n_bars)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
