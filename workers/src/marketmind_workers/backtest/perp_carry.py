"""Phase E.6 — single-asset PURE CARRY (funding-harvest) simulator.

The signal is the perp FUNDING RATE itself — not price/trend. Harvest the
funding flow from the crowded side: when funding is extreme (|z| >= entry_z
over a rolling window of the 8h funding observations) take the RECEIVING
side — SHORT when funding is high/positive (crowded longs pay shorts), LONG
when funding is low/negative (crowded shorts pay longs):

    direction = -sign(funding_at_entry)   # the receiving side

Exit when funding normalises (|z| <= exit_z), OR on an ATR stop on the PRICE
leg — the steamroller cap: collecting funding on a short while the asset rips
(or long while it dumps) can dwarf the receipts, so the price loss is bounded.

Self-contained, single-instrument; reuses the VERIFIED E.3 funding-on-mark
accounting (`perp_pairs.funding_cashflow`). Unlevered. Per-trade PnL is split
into funding_pnl (collected) vs price_pnl (the steamroller exposure) so the
decomposition headline is computable. Runs at 4h (funding's 8h stamps tile 4h
cleanly; reuses perp_trend.load_perp_trend_data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

import numpy as np
import pandas as pd
from marketmind_shared.schemas.strategy_spec.common import AssetClass

from marketmind_workers.backtest.fee_model import (
    _fallback_commission_for_class,  # pyright: ignore[reportPrivateUsage]
)
from marketmind_workers.backtest.perp_pairs import funding_cashflow
from marketmind_workers.backtest.perp_trend import (
    _atr,  # pyright: ignore[reportPrivateUsage]
    load_perp_trend_data,
)
from marketmind_workers.backtest.slippage_model import (
    _fallback_slippage_for_class,  # pyright: ignore[reportPrivateUsage]
)

load_perp_carry_data = load_perp_trend_data  # same 4h fixtures (OHLCV+mark+funding)


@dataclass(frozen=True)
class CarryTrade:
    entry_time: datetime
    exit_time: datetime
    direction: int                # +1 long (collect negative funding), -1 short (collect positive)
    price_pnl: float              # the steamroller exposure
    funding_pnl: float            # funding collected (should be > 0 if on the receiving side)
    cost: float
    net_pnl: float
    exit_reason: str              # "normalize" | "stop" | "end_of_data"


@dataclass
class CarryResult:
    asset: str
    trades: list[CarryTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    total_funding: float = 0.0
    total_price_pnl: float = 0.0
    total_cost: float = 0.0
    final_equity: float = 0.0
    initial_capital: float = 0.0


def funding_zscore(funding_bars: pd.Series, window: int) -> np.ndarray:
    """Rolling z-score of the funding RATE over its 8h observations, forward-
    filled to the bar grid. `funding_bars` is reindexed to bars (values at the
    8h stamps, NaN between). z is computed on the observations (not the ffill'd
    series, which would inflate the sample), then mapped to each bar via the
    most-recent stamp."""
    obs = funding_bars.dropna()
    mean = obs.rolling(window).mean()
    std = obs.rolling(window).std(ddof=1)
    z_obs = (obs - mean) / std.where(std != 0.0)
    return cast("pd.Series", z_obs.reindex(funding_bars.index).ffill()).to_numpy()


@dataclass
class _Pos:
    direction: int
    entry_bar: int
    signed_qty: float
    entry_fill: float
    stop: float
    funding_accrued: float = 0.0


def run_perp_carry_backtest(
    ohlcv: pd.DataFrame, funding: pd.Series, *, asset: str = "?",
    funding_window: int = 90, entry_z: float = 2.0, exit_z: float = 0.5,
    atr_period: int = 14, atr_mult: float = 3.0, percent: float = 1.0,
    asset_class: AssetClass = "crypto_perp", initial_capital: float = 10_000.0,
) -> CarryResult:
    idx = pd.DatetimeIndex(ohlcv.index)
    o = ohlcv["open"].to_numpy()
    h = ohlcv["high"].to_numpy()
    low = ohlcv["low"].to_numpy()
    c = ohlcv["close"].to_numpy()
    mk = ohlcv["mark_close"].to_numpy()
    fr = funding.to_numpy()
    fr_ffill = funding.ffill().to_numpy()        # active funding rate (sign source)
    z = funding_zscore(funding, funding_window)  # active funding z
    atr = _atr(h, low, c, atr_period)
    times = [t.to_pydatetime() for t in idx]
    n = len(c)
    cost_frac = _fallback_commission_for_class(asset_class) + _fallback_slippage_for_class(asset_class)

    res = CarryResult(asset=asset, initial_capital=initial_capital)
    cash = initial_capital
    pos: _Pos | None = None
    pending_entry: int | None = None
    pending_exit: str | None = None

    def _equity(bar: int) -> float:
        return cash if pos is None else cash + pos.signed_qty * (mk[bar] - pos.entry_fill)

    def _close(bar: int, fill: float, reason: str) -> None:
        nonlocal cash, pos
        assert pos is not None
        price_pnl = pos.signed_qty * (fill - pos.entry_fill)
        cost = abs(pos.signed_qty) * fill * cost_frac
        cash += price_pnl - cost
        res.total_cost += cost
        res.total_price_pnl += price_pnl
        res.trades.append(CarryTrade(
            entry_time=times[pos.entry_bar], exit_time=times[bar], direction=pos.direction,
            price_pnl=price_pnl, funding_pnl=pos.funding_accrued, cost=cost,
            net_pnl=price_pnl + pos.funding_accrued - cost, exit_reason=reason))
        pos = None

    for bar in range(n):
        # STEP 1 — next-open fills (LAST price).
        if pending_exit is not None and pos is not None:
            _close(bar, o[bar], pending_exit)
            pending_exit = None
        if pending_entry is not None and pos is None:
            d = pending_entry
            fill = o[bar]
            qty = d * (percent * cash) / fill
            stop = fill - d * atr_mult * atr[bar]
            cash -= abs(qty) * fill * cost_frac
            res.total_cost += abs(qty) * fill * cost_frac
            pos = _Pos(direction=d, entry_bar=bar, signed_qty=qty, entry_fill=fill, stop=stop)
            pending_entry = None

        # STEP 2 — funding accrual on MARK at an 8h stamp while held (the harvest).
        if pos is not None and fr[bar] == fr[bar]:
            cf = funding_cashflow(pos.signed_qty, mk[bar], fr[bar])
            cash += cf
            pos.funding_accrued += cf
            res.total_funding += cf

        # STEP 3 — intrabar ATR stop on the PRICE leg (steamroller cap). Fixed
        # from entry (a carry stop is a disaster brake, not a trailing trend stop).
        if pos is not None and pending_exit is None:
            hit = (low[bar] <= pos.stop) if pos.direction > 0 else (h[bar] >= pos.stop)
            if hit:
                fill = o[bar] if ((pos.direction > 0 and o[bar] <= pos.stop)
                                  or (pos.direction < 0 and o[bar] >= pos.stop)) else pos.stop
                _close(bar, fill, "stop")

        # STEP 4 — mark-to-market on MARK.
        res.equity_curve.append((times[bar], _equity(bar)))

        # STEP 5 — decide next-bar action from the funding regime.
        if bar >= n - 1:
            continue
        zb = z[bar]
        if zb != zb:  # warmup (no funding z yet)
            continue
        if pos is None and pending_entry is None:
            if abs(zb) >= entry_z:
                f = fr_ffill[bar]
                if f == f and f != 0.0:
                    pending_entry = -int(np.sign(f))  # receiving side of the crowded funding
        elif pos is not None and pending_exit is None and abs(zb) <= exit_z:
            pending_exit = "normalize"

    if pos is not None:
        bar = n - 1
        price_pnl = pos.signed_qty * (mk[bar] - pos.entry_fill)
        cash += price_pnl
        res.total_price_pnl += price_pnl
        res.trades.append(CarryTrade(
            entry_time=times[pos.entry_bar], exit_time=times[bar], direction=pos.direction,
            price_pnl=price_pnl, funding_pnl=pos.funding_accrued, cost=0.0,
            net_pnl=price_pnl + pos.funding_accrued, exit_reason="end_of_data"))
    res.final_equity = cash
    return res
