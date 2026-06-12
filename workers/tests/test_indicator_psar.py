"""Correctness tests for PSAR (v1.1, ta-backed).

Wraps `ta.trend.PSARIndicator`. The `value` is bit-for-bit ta's `psar()`;
`direction` is derived from the canonical "SAR below price = up" semantic.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from marketmind_workers.backtest import indicators as ind
from ta.trend import PSARIndicator

# `ta`'s PSARIndicator triggers a pandas Series.__setitem__ FutureWarning
# (positional integer-key set) — out of our hands. Per the project-log Phase
# 2.2 hard-won pattern, scope the warning suppression to this test file.
pytestmark = pytest.mark.filterwarnings("ignore::FutureWarning")


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


def test_psar_shape() -> None:
    df = _ohlcv([100.0 + i for i in range(60)])
    p = ind.psar(df, step=0.02, max_step=0.2)
    assert list(p.columns) == ["value", "direction"]
    assert len(p) == 60
    assert not math.isnan(p["value"].iloc[-1])


def test_psar_direction_is_plus_or_minus_one() -> None:
    df = _ohlcv([100.0] * 14 + [100.0 + 5.0 * i for i in range(46)])
    direction = ind.psar(df, step=0.02, max_step=0.2)["direction"].dropna()
    assert len(direction) > 0
    assert set(direction.unique()) <= {1.0, -1.0}


def test_psar_uptrend_is_bullish() -> None:
    # A sustained rise — PSAR direction lands at +1.
    closes = [100.0] * 14 + [100.0 + 3.0 * i for i in range(46)]
    p = ind.psar(_ohlcv(closes), step=0.02, max_step=0.2)
    assert p["direction"].iloc[-1] == 1.0
    # The SAR is below price during an uptrend.
    assert p["value"].iloc[-1] < closes[-1]


def test_psar_downtrend_is_bearish() -> None:
    closes = [300.0] * 14 + [300.0 - 3.0 * i for i in range(46)]
    p = ind.psar(_ohlcv(closes), step=0.02, max_step=0.2)
    assert p["direction"].iloc[-1] == -1.0
    assert p["value"].iloc[-1] > closes[-1]


def test_psar_value_matches_ta_reference() -> None:
    rng = np.random.default_rng(42)
    closes = list(100.0 + np.cumsum(rng.normal(0.0, 2.0, size=200)))
    df = _ohlcv(closes, spread=1.0)
    ours = ind.psar(df, step=0.02, max_step=0.2)
    ref = PSARIndicator(
        high=df["high"], low=df["low"], close=df["close"],
        step=0.02, max_step=0.2, fillna=False,
    ).psar()
    pd.testing.assert_series_equal(ours["value"], ref, check_names=False)
