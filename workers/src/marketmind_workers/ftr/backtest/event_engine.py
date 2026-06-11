"""FTR event-driven engine — final-candidate runs, Decimal accounting.

Walks bars one at a time, consuming a 0/1 position series (or replaying
precomputed fold predictions through ``decide_window`` upstream — the ML
strategy may precompute and replay deterministically, mandate Stage 4).

Same fill law as the vector engine: decision at bar t close, fill at bar
t+1 open, price worsened by per-side cost. Money is Decimal, quantized to
instrument precision at the accounting boundary; feature/model math stays
float (non-negotiable constraint 5).

Drift parity with the vector engine is a CI gate (test_ftr_drift_parity):
identical trade timestamps + net return within tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, getcontext

import pandas as pd

from marketmind_workers.ftr.backtest.costs import CostBreakdown
from marketmind_workers.ftr.backtest.vector_engine import RunResult, Trade
from marketmind_workers.ftr.data.ohlcv import dtindex

getcontext().prec = 28

# Spot BTC/USDT lot/tick defaults (used when ccxt market metadata is not
# loaded — backtests on cached parquet have no live exchange handle).
DEFAULT_QTY_STEP = Decimal("0.00001")
DEFAULT_PRICE_TICK = Decimal("0.01")


def quantize_qty(qty: Decimal, step: Decimal = DEFAULT_QTY_STEP) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass
class LedgerState:
    cash: Decimal
    qty: Decimal
    entry_fill: Decimal | None = None
    entry_ts: datetime | None = None
    entry_i: int | None = None


def run_event_backtest(
    ohlcv: pd.DataFrame,
    position: pd.Series,
    costs: CostBreakdown,
    *,
    initial_cash: Decimal = Decimal("10000"),
    qty_step: Decimal = DEFAULT_QTY_STEP,
) -> RunResult:
    """Bar-by-bar replay with a Decimal ledger. All-in/all-out long/flat."""
    idx = dtindex(ohlcv)
    if not position.index.equals(idx):
        position = position.reindex(idx).fillna(0).astype("int64")

    pos = position.to_numpy(dtype="int64")
    open_arr = ohlcv["open"].to_numpy(dtype="float64")
    n = len(pos)
    side_cost = Decimal(str(costs.per_side_bps)) * Decimal("0.0001")

    state = LedgerState(cash=initial_cash, qty=Decimal("0"))
    equity_vals: list[float] = []
    trades: list[Trade] = []

    def _mark(price: float) -> float:
        eq = state.cash + state.qty * Decimal(str(price))
        return float(eq)

    for i in range(n):
        open_px = Decimal(str(open_arr[i]))
        want = pos[i - 1] if i > 0 else 0  # decided at the previous close

        if want == 1 and state.qty == 0:
            fill_px = open_px * (Decimal(1) + side_cost)
            qty = quantize_qty(state.cash / fill_px, qty_step)
            if qty > 0:
                cost = qty * fill_px
                state.cash -= cost
                state.qty = qty
                state.entry_fill = fill_px
                ts = idx[i]
                assert isinstance(ts, pd.Timestamp)
                state.entry_ts = ts.to_pydatetime()
                state.entry_i = i
        elif want == 0 and state.qty > 0:
            fill_px = open_px * (Decimal(1) - side_cost)
            state.cash += state.qty * fill_px
            assert state.entry_fill is not None and state.entry_ts is not None
            assert state.entry_i is not None
            ts = idx[i]
            assert isinstance(ts, pd.Timestamp)
            entry_raw = open_arr[state.entry_i]
            trades.append(
                Trade(
                    entry_ts=state.entry_ts,
                    exit_ts=ts.to_pydatetime(),
                    entry_px=float(state.entry_fill),
                    exit_px=float(fill_px),
                    net_return=float(fill_px / state.entry_fill) - 1.0,
                    gross_return=open_arr[i] / entry_raw - 1.0,
                    bars_held=i - state.entry_i,
                )
            )
            state.qty = Decimal("0")
            state.entry_fill = None
            state.entry_ts = None
            state.entry_i = None

        # End-of-data close-out at the last bar's open (vector parity).
        if i == n - 1 and state.qty > 0:
            fill_px = open_px * (Decimal(1) - side_cost)
            state.cash += state.qty * fill_px
            assert state.entry_fill is not None and state.entry_ts is not None
            assert state.entry_i is not None
            ts = idx[i]
            assert isinstance(ts, pd.Timestamp)
            entry_raw = open_arr[state.entry_i]
            trades.append(
                Trade(
                    entry_ts=state.entry_ts,
                    exit_ts=ts.to_pydatetime(),
                    entry_px=float(state.entry_fill),
                    exit_px=float(fill_px),
                    net_return=float(fill_px / state.entry_fill) - 1.0,
                    gross_return=open_arr[i] / entry_raw - 1.0,
                    bars_held=i - state.entry_i,
                )
            )
            state.qty = Decimal("0")

        # Mark equity at the bar OPEN after any fills (open-to-open basis,
        # same valuation instants as the vector engine's equity).
        equity_vals.append(_mark(open_arr[i]))

    equity = pd.Series(equity_vals, index=idx) / float(initial_cash)
    bar_returns = equity.pct_change().fillna(0.0)
    return RunResult(equity=equity, trades=trades, bar_returns=bar_returns)
