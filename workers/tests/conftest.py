from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest


def _set_test_env() -> None:
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


_set_test_env()


@pytest.fixture
def make_candles() -> Callable[[list[float]], pd.DataFrame]:
    """Build a synthetic 4h-bar OHLCV DataFrame from a list of close prices.

    Used by every trader-template snapshot test. Each bar:
      - open  = previous close (first bar opens at its own close)
      - high  = max(open, close) * 1.001
      - low   = min(open, close) * 0.999
      - volume = 1000 (constant)
      - open_ts = 2026-01-01 UTC + i * 4h (sequential bars)

    Returned DataFrame is tz-aware UTC, indexed by bar OPEN time —
    matches the contract documented in
    `marketmind_workers.trader.templates.base.StrategyTemplate.evaluate`.
    """

    def _make(closes: list[float]) -> pd.DataFrame:
        if not closes:
            raise ValueError("closes must not be empty")
        n = len(closes)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        tf = timedelta(hours=4)
        opens = [closes[0], *closes[:-1]]
        highs = [max(o, c) * 1.001 for o, c in zip(opens, closes, strict=True)]
        lows = [min(o, c) * 0.999 for o, c in zip(opens, closes, strict=True)]
        return pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": [1000.0] * n,
            },
            index=pd.DatetimeIndex(
                [start + i * tf for i in range(n)],
                name="timestamp",
            ),
        )

    return _make
