"""Baselines reported alongside every verdict (mandate Stage 4).

- BTC buy-and-hold (same window, same costs: one round trip)
- equal-weight hold of the trend universe (monthly rebalance, with costs)
- repo slow-trend reference: Modern-Turtle-CLASS parameters (EMA 50/200
  confirmation + Donchian-55 breakout + 3*ATR chandelier) run through the
  FTR engine on BTC only. Labeled a PROXY — the actual seeded strategy
  lives in the trader DB and runs on the repo engine; this reproduces its
  parameter class on identical data/costs so the comparison is apples-to-
  apples within FTR.
- naive sign prediction WITHOUT the EV gate (for 3.1)
- matched-frequency random entry (validation.montecarlo)
"""

from __future__ import annotations

import pandas as pd

from marketmind_workers.ftr.backtest.costs import CostBreakdown
from marketmind_workers.ftr.backtest.vector_engine import (
    RunResult,
    run_portfolio_backtest,
    run_vector_backtest,
)
from marketmind_workers.ftr.data.ohlcv import dtindex
from marketmind_workers.ftr.strategies.specs import TrendPortfolioSpec, validate_ftr_spec
from marketmind_workers.ftr.strategies.trend_portfolio import compute_asset_signals


def buy_and_hold(ohlcv: pd.DataFrame, costs: CostBreakdown) -> RunResult:
    """Long from the first bar to the last; pays one round trip."""
    pos = pd.Series(1, index=ohlcv.index, dtype="int64")
    pos.iloc[-1] = 0
    return run_vector_backtest(ohlcv, pos, costs)


def equal_weight_hold(
    ohlcv_by_symbol: dict[str, pd.DataFrame],
    costs_by_symbol: dict[str, CostBreakdown],
    index: pd.DatetimeIndex,
) -> RunResult:
    """1/N across whatever is listed at each bar, monthly rebalance."""
    symbols = sorted(ohlcv_by_symbol)
    listed = pd.DataFrame(
        {s: ohlcv_by_symbol[s]["close"].reindex(index).notna() for s in symbols}, index=index
    )
    n_listed = listed.sum(axis=1).replace(0, 1)
    w_daily = listed.div(n_listed, axis=0)
    # hold weights constant within each month (rebalance at month start)
    month = pd.Series(index.to_period("M"), index=index)
    is_month_start = month.ne(month.shift(1))
    w = w_daily.where(is_month_start).ffill().fillna(0.0)
    return run_portfolio_backtest(ohlcv_by_symbol, w, costs_by_symbol)


def naive_sign_no_gate(
    ohlcv: pd.DataFrame,
    p_up: pd.Series,
    costs: CostBreakdown,
) -> RunResult:
    """Enter when p_up >= 0.5, exit when p_up < 0.5 — no EV gate, no cost
    awareness. Shows what the gate is saving (or costing)."""
    pos = (p_up >= 0.5).astype("int64")
    full = pos.reindex(dtindex(ohlcv)).fillna(0).astype("int64")
    return run_vector_backtest(ohlcv, full, costs)


def modern_turtle_proxy(
    btc_4h: pd.DataFrame,
    costs: CostBreakdown,
) -> RunResult:
    """Repo slow-trend reference, PROXY (see module docstring)."""
    spec = validate_ftr_spec(
        {
            "kind": "trend_4h_portfolio",
            "strategy_id": "modern-turtle-proxy",
            "venue_profile": costs.profile,
            "ema_fast": 50,
            "ema_slow": 200,
            "donchian_n": 55,
            "chandelier_atr_multiple": 3.0,
            "universe_size": 2,
        }
    )
    assert isinstance(spec, TrendPortfolioSpec)
    member = pd.Series(True, index=btc_4h.index)
    sig = compute_asset_signals(btc_4h, spec=spec, member_mask=member)
    return run_vector_backtest(btc_4h, sig.position, costs)
