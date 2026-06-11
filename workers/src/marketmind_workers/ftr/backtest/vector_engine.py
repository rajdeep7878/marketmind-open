"""FTR vectorized engine — used for sweeps; float arithmetic for speed.

Fill law (identical to the repo's engines and to the FTR event engine):
a position decided at bar t's CLOSE is filled at bar t+1's OPEN, worsened
by per-side cost (fee + half-spread + slippage) from the active venue
profile. No same-bar fills; the last bar cannot produce a fill. Latency
assumption: one full bar between decision and fill — documented, explicit,
pessimistic for fast markets.

The event engine (Decimal ledger) is the authority for final candidate
runs; drift parity between the two is a Stage-7 gate
(test_ftr_drift_parity).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from marketmind_workers.ftr.backtest.costs import CostBreakdown
from marketmind_workers.ftr.data.ohlcv import dtindex


@dataclass(frozen=True)
class Trade:
    entry_ts: datetime
    exit_ts: datetime
    entry_px: float  # cost-worsened fill
    exit_px: float  # cost-worsened fill
    net_return: float  # fractional, after costs
    gross_return: float  # fractional, before costs
    bars_held: int


@dataclass
class RunResult:
    """One single-asset (or portfolio) backtest run."""

    equity: pd.Series  # net equity curve (start = 1.0), per bar
    trades: list[Trade] = field(default_factory=list)
    bar_returns: pd.Series | None = None  # net per-bar strategy returns
    # Portfolio runs have no per-trade ledger; they report gross return and
    # total cost drag directly through these overrides.
    gross_total_override: float | None = None
    cost_paid_override: float | None = None

    @property
    def net_total_return(self) -> float:
        return float(self.equity.iloc[-1] - 1.0) if len(self.equity) else 0.0

    @property
    def gross_total_return(self) -> float:
        if self.gross_total_override is not None:
            return self.gross_total_override
        g = 1.0
        for t in self.trades:
            g *= 1.0 + t.gross_return
        return g - 1.0


def run_vector_backtest(
    ohlcv: pd.DataFrame,
    position: pd.Series,
    costs: CostBreakdown,
) -> RunResult:
    """Simulate a 0/1 position series over OHLCV with next-bar-open fills.

    ``position[t]`` is the desired exposure decided at bar t's close.
    Returns are computed open-to-open while in position; entry/exit bars
    each pay per-side cost once.
    """
    idx = dtindex(ohlcv)
    if not position.index.equals(idx):
        position = position.reindex(idx).fillna(0).astype("int64")

    pos = position.to_numpy(dtype="int64")
    open_arr = ohlcv["open"].to_numpy(dtype="float64")
    n = len(pos)
    side_cost = costs.per_side_bps * 1e-4

    # held[t] = exposure DURING bar t (decided at t-1's close).
    held = np.zeros(n, dtype="int64")
    held[1:] = pos[:-1]

    # Per-bar gross returns while held: open[t] -> open[t+1].
    bar_ret = np.zeros(n, dtype="float64")
    valid = np.arange(n - 1)
    bar_ret[valid] = open_arr[valid + 1] / open_arr[valid] - 1.0

    net_ret = np.where(held == 1, bar_ret, 0.0).astype("float64")

    # Exact cost arithmetic (parity-tight with the Decimal event engine):
    # entry buys at open*(1+c) => the entry bar's return divides by (1+c);
    # exit sells at open*(1-c) => the flip bar multiplies by (1-c).
    flips = np.diff(held, prepend=np.zeros(1, dtype="int64"))
    entry_bars = np.nonzero(flips == 1)[0]
    exit_bars = np.nonzero(flips == -1)[0]
    net_ret[entry_bars] = (1.0 + net_ret[entry_bars]) / (1.0 + side_cost) - 1.0
    net_ret[exit_bars] = (1.0 + net_ret[exit_bars]) * (1.0 - side_cost) - 1.0

    # Open position at end of data: closed at the last bar's open with cost
    # (end_of_data exit, mirroring the repo iterative engine's convention).
    if held[-1] == 1:
        net_ret[-1] = (1.0 + net_ret[-1]) * (1.0 - side_cost) - 1.0

    equity = pd.Series(np.cumprod(1.0 + net_ret), index=idx)

    # Trade ledger.
    def _close_trade(open_i: int, i: int) -> Trade:
        entry_fill = open_arr[open_i] * (1.0 + side_cost)
        exit_fill = open_arr[i] * (1.0 - side_cost)
        gross = open_arr[i] / open_arr[open_i] - 1.0
        net = exit_fill / entry_fill - 1.0
        ts_in, ts_out = idx[open_i], idx[i]
        assert isinstance(ts_in, pd.Timestamp) and isinstance(ts_out, pd.Timestamp)
        return Trade(
            entry_ts=ts_in.to_pydatetime(),
            exit_ts=ts_out.to_pydatetime(),
            entry_px=entry_fill,
            exit_px=exit_fill,
            net_return=net,
            gross_return=gross,
            bars_held=i - open_i,
        )

    trades: list[Trade] = []
    open_i: int | None = None
    for i in range(n):
        if flips[i] == 1:
            open_i = int(i)
        elif flips[i] == -1 and open_i is not None:
            trades.append(_close_trade(open_i, int(i)))
            open_i = None
    if open_i is not None:  # end-of-data close at the last bar's open
        trades.append(_close_trade(open_i, n - 1))

    return RunResult(equity=equity, trades=trades, bar_returns=pd.Series(net_ret, index=idx))


def run_portfolio_backtest(
    ohlcv_by_symbol: dict[str, pd.DataFrame],
    weights: pd.DataFrame,
    costs_by_symbol: dict[str, CostBreakdown],
) -> RunResult:
    """Multi-asset weighted portfolio with next-bar-open fills.

    ``weights.loc[t, sym]`` is the target weight decided at bar t's close,
    applied during bar t+1 (open-to-open). Costs are charged per side on
    TURNOVER: |w_applied(t) - w_applied(t-1)| x per-side cost.
    """
    symbols = list(weights.columns)
    idx = dtindex(weights)

    open_to_open: dict[str, np.ndarray] = {}
    for sym in symbols:
        opens = ohlcv_by_symbol[sym]["open"].reindex(idx).to_numpy(dtype="float64")
        r = np.full(len(idx), 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            r[:-1] = opens[1:] / opens[:-1] - 1.0
        open_to_open[sym] = np.nan_to_num(r, nan=0.0)

    w_target = weights.fillna(0.0).to_numpy(dtype="float64")
    _, m = w_target.shape
    # weight applied DURING bar t = target decided at t-1's close
    w_applied = np.zeros_like(w_target)
    w_applied[1:] = w_target[:-1]

    rets = np.column_stack([open_to_open[s] for s in symbols])
    side_costs = np.array([costs_by_symbol[s].per_side_bps * 1e-4 for s in symbols])

    turnover = np.abs(np.diff(w_applied, axis=0, prepend=np.zeros((1, m))))
    cost_drag = turnover @ side_costs

    gross_ret = (w_applied * rets).sum(axis=1)
    port_ret = gross_ret - cost_drag
    equity = pd.Series(np.cumprod(1.0 + port_ret), index=idx)
    return RunResult(
        equity=equity,
        trades=[],
        bar_returns=pd.Series(port_ret, index=idx),
        gross_total_override=float(np.prod(1.0 + gross_ret) - 1.0),
        cost_paid_override=float(cost_drag.sum()),
    )
