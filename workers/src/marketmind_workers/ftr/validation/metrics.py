"""Net-of-cost performance + frequency diagnostics for FTR runs."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from marketmind_workers.ftr.backtest.vector_engine import RunResult

_BARS_PER_YEAR = {"1m": 525_600.0, "1h": 8760.0, "4h": 2190.0, "6h": 1460.0, "1d": 365.0}


@dataclass(frozen=True)
class NetMetrics:
    net_total_return: float
    gross_total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    profit_factor: float
    expectancy: float  # mean net trade return (fraction)
    win_rate: float
    num_trades: int
    trades_per_day: float
    avg_holding_hours: float
    turnover_annual: float
    cost_paid_frac: float  # total cost drag as a fraction of start equity
    cost_over_gross_edge: float
    bars: int
    bars_per_year: float
    years: float
    skewness: float
    kurtosis: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "net_total_return": self.net_total_return,
            "gross_total_return": self.gross_total_return,
            "cagr": self.cagr,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "win_rate": self.win_rate,
            "num_trades": self.num_trades,
            "trades_per_day": self.trades_per_day,
            "avg_holding_hours": self.avg_holding_hours,
            "turnover_annual": self.turnover_annual,
            "cost_paid_frac": self.cost_paid_frac,
            "cost_over_gross_edge": self.cost_over_gross_edge,
            "bars": self.bars,
            "bars_per_year": self.bars_per_year,
            "years": self.years,
            "skewness": self.skewness,
            "kurtosis": self.kurtosis,
        }


def compute_net_metrics(
    result: RunResult,
    *,
    timeframe: str,
    round_trip_cost_frac: float | None = None,
) -> NetMetrics:
    """Headline metrics from a RunResult.

    ``cost_over_gross_edge``: total cost paid / gross edge, both in simple
    return terms. Gross edge <= 0 with positive costs reports inf (and fails
    G6 downstream) — a strategy with no gross edge has nothing to pay costs
    from.
    """
    bpy = _BARS_PER_YEAR[timeframe]
    bar_hours = 8760.0 / bpy
    rets = (
        result.bar_returns
        if result.bar_returns is not None
        else result.equity.pct_change().fillna(0.0)
    )
    arr = rets.to_numpy(dtype="float64")
    bars = len(arr)
    years = bars / bpy if bpy else 0.0

    mean = float(arr.mean()) if bars else 0.0
    std = float(arr.std(ddof=1)) if bars > 2 else 0.0
    sharpe = (mean / std * np.sqrt(bpy)) if std > 0 else 0.0

    eq = result.equity.to_numpy(dtype="float64")
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min()) if bars else 0.0

    net_total = result.net_total_return
    cagr = (1.0 + net_total) ** (1.0 / years) - 1.0 if years > 0.25 and net_total > -1 else 0.0

    trades = result.trades
    n_trades = len(trades)
    wins = [t for t in trades if t.net_return > 0]
    losses = [t for t in trades if t.net_return <= 0]
    gross_win = sum(t.net_return for t in wins)
    gross_loss = -sum(t.net_return for t in losses)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (np.inf if gross_win > 0 else 0.0)
    expectancy = float(np.mean([t.net_return for t in trades])) if trades else 0.0
    win_rate = len(wins) / n_trades if n_trades else 0.0

    days = years * 365.0
    trades_per_day = n_trades / days if days > 0 else 0.0
    avg_hold_hours = (
        float(np.mean([t.bars_held for t in trades])) * bar_hours if trades else 0.0
    )

    # Costs: per-trade (gross - net) or the portfolio engine's override.
    if result.cost_paid_override is not None:
        cost_paid = result.cost_paid_override
        gross_edge = result.gross_total_override or 0.0
    else:
        cost_paid = sum((t.gross_return - t.net_return) for t in trades)
        gross_edge = sum(t.gross_return for t in trades)
    if round_trip_cost_frac is not None and not trades:
        cost_paid = 0.0 if result.cost_paid_override is None else cost_paid
    cost_over_gross = cost_paid / gross_edge if gross_edge > 0 else float("inf")
    if n_trades == 0 and result.cost_paid_override is None:
        cost_over_gross = 0.0

    # Annualized turnover: 2 sides per round trip, full equity each time.
    turnover_annual = (2.0 * n_trades) / years if years > 0 else 0.0

    skew = float(pd.Series(arr).skew()) if bars > 10 else 0.0
    kurt = float(pd.Series(arr).kurt()) + 3.0 if bars > 10 else 3.0  # Pearson

    return NetMetrics(
        net_total_return=net_total,
        gross_total_return=result.gross_total_return,
        cagr=cagr,
        sharpe=float(sharpe),
        max_drawdown=max_dd,
        profit_factor=float(profit_factor),
        expectancy=expectancy,
        win_rate=win_rate,
        num_trades=n_trades,
        trades_per_day=trades_per_day,
        avg_holding_hours=avg_hold_hours,
        turnover_annual=turnover_annual,
        cost_paid_frac=float(cost_paid),
        cost_over_gross_edge=float(cost_over_gross),
        bars=bars,
        bars_per_year=bpy,
        years=years,
        skewness=skew,
        kurtosis=kurt,
    )
