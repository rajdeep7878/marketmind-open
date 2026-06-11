"""Drift parity: vectorized vs event-driven engine, within repo tolerance
(structural trade equality + headline metric within 1e-3 relative)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from marketmind_workers.ftr.backtest.costs import cost_breakdown
from marketmind_workers.ftr.backtest.event_engine import run_event_backtest
from marketmind_workers.ftr.backtest.vector_engine import run_vector_backtest
from marketmind_workers.ftr.strategies.specs import TrendPortfolioSpec
from marketmind_workers.ftr.strategies.trend_portfolio import compute_asset_signals

from .ftr_helpers import synthetic_ohlcv

_REL_TOL = 1e-3


def _assert_parity(df: pd.DataFrame, position: pd.Series, profile: str) -> None:
    costs = cost_breakdown(profile, "BTC/USDT")
    v = run_vector_backtest(df, position, costs)
    e = run_event_backtest(df, position, costs)
    assert len(v.trades) == len(e.trades), "trade count diverged"
    for tv, te in zip(v.trades, e.trades, strict=True):
        assert tv.entry_ts == te.entry_ts and tv.exit_ts == te.exit_ts
    rel = abs(v.net_total_return - e.net_total_return) / (abs(e.net_total_return) + 1e-9)
    assert rel <= _REL_TOL, f"net return drift {rel:.2e} > {_REL_TOL}"


def test_parity_random_positions() -> None:
    """ML-style sparse position stream (precomputed decisions replayed)."""
    df = synthetic_ohlcv(n_bars=3000, seed=31, vol=0.006)
    rng = np.random.default_rng(13)
    raw = rng.uniform(size=3000)
    position = pd.Series((raw > 0.93).astype("int64"), index=df.index)
    # hold for a few bars after each entry signal
    position = position.rolling(8, min_periods=1).max().astype("int64")
    _assert_parity(df, position, "kraken_pro_uk_tier0")


def test_parity_trend_signals() -> None:
    """Trend state-machine positions through both engines."""
    df = synthetic_ohlcv(n_bars=4000, seed=37, drift=0.0003, vol=0.01, bar_hours=4)
    spec = TrendPortfolioSpec.model_validate(
        {
            "kind": "trend_4h_portfolio",
            "strategy_id": "parity-trend",
            "venue_profile": "coinbase_advanced_uk_tier0",
        }
    )
    member = pd.Series(data=True, index=df.index)
    sig = compute_asset_signals(df, spec=spec, member_mask=member)
    assert sig.entries.sum() > 0, "fixture produced no entries — adjust seed"
    _assert_parity(df, sig.position, "coinbase_advanced_uk_tier0")


def test_parity_always_in() -> None:
    df = synthetic_ohlcv(n_bars=1000, seed=41)
    position = pd.Series(1, index=df.index, dtype="int64")
    position.iloc[-1] = 0
    _assert_parity(df, position, "binance_spot_reference")
