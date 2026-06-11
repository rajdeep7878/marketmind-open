"""Phase E.5b — single-asset perpetual TREND simulator (long AND short).

Ports the proven confirmation-layered trend shape that seeds on BTC 4H
(Hunt 18/19/20, triple-EMA cascade, Sharpe 0.66-1.08) to a perp that can go
LONG in a confirmed uptrend and SHORT in a confirmed downtrend (flat
otherwise), so the strategy fires in downtrends too. Self-contained,
single-instrument — NOT the multi-leg pair engine; it reuses the VERIFIED
funding-on-mark accounting (`perp_pairs.funding_cashflow`) for honesty.

SIGNAL (4H): triple-EMA cascade on close.
  regime = +1  if ema_fast > ema_mid > ema_slow   (confirmed uptrend -> LONG)
         = -1  if ema_fast < ema_mid < ema_slow   (confirmed downtrend -> SHORT)
         =  0  otherwise (no confirmed trend -> FLAT)
Enter on a confirmed regime while flat; EXIT on regime-flip (the cascade no
longer confirms the held side) OR an ATR stop (atr_mult x ATR from entry).
Fills next-bar-open on LAST price; funding accrues on MARK at each 8h stamp
held, sign-correct per side; single-leg perp cost both sides. Unlevered
(notional = percent x equity).

HONESTY (per E.3): fills on LAST, funding + mark-to-market on MARK (never
conflated); funding_cashflow = -signed_qty*mark*rate (long pays positive
funding, short receives), reused verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
from marketmind_shared.schemas.strategy_spec.common import AssetClass

from marketmind_workers.backtest.fee_model import (
    _fallback_commission_for_class,  # pyright: ignore[reportPrivateUsage]
)
from marketmind_workers.backtest.perp_pairs import funding_cashflow
from marketmind_workers.backtest.slippage_model import (
    _fallback_slippage_for_class,  # pyright: ignore[reportPrivateUsage]
)

_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "market"


@dataclass(frozen=True)
class TrendTrade:
    entry_time: datetime
    exit_time: datetime
    direction: int                # +1 long, -1 short
    price_pnl: float
    funding_pnl: float
    cost: float
    net_pnl: float
    exit_reason: str              # "flip" | "stop" | "end_of_data"


@dataclass
class TrendFundingRow:
    timestamp: datetime
    signed_qty: float
    mark_price: float
    funding_rate: float
    cashflow: float


@dataclass
class TrendResult:
    asset: str
    trades: list[TrendTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    funding_ledger: list[TrendFundingRow] = field(default_factory=list)
    total_funding: float = 0.0
    total_cost: float = 0.0
    final_equity: float = 0.0
    initial_capital: float = 0.0


def load_perp_trend_data(asset: str, *, timeframe: str = "4h",
                         fixture_dir: Path | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """Load the 1h perp fixture for `asset` and resample to `timeframe` (4h).
    Returns (ohlcv_with_mark_close, funding_series_reindexed_to_bars). The 8h
    funding stamps (00/08/16 UTC) land on 4h bars; intervening 4h bars get NaN
    funding (no accrual)."""
    fdir = fixture_dir if fixture_dir is not None else _FIXTURE_DIR
    base = asset.split("/")[0].lower()
    o1h = pd.read_parquet(fdir / f"binance_{base}_usdt_perp_1h.parquet").rename_axis(None)
    fund = cast("pd.Series", pd.read_parquet(fdir / f"binance_{base}_usdt_perp_funding.parquet")
                .rename_axis(None)["funding_rate"])
    agg = cast("pd.DataFrame", o1h.resample(timeframe, label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), mark_close=("mark_close", "last")))
    agg = cast("pd.DataFrame", agg[agg["close"].notna()])
    funding = fund.reindex(pd.DatetimeIndex(agg.index))
    return agg, funding


def _ema(close: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(close).ewm(span=period, adjust=False).mean().to_numpy()


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    # Wilder smoothing
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()


@dataclass
class _Pos:
    direction: int                # +1 / -1
    entry_bar: int
    signed_qty: float
    entry_fill: float
    stop: float
    funding_accrued: float = 0.0


def run_perp_trend_backtest(
    ohlcv: pd.DataFrame, funding: pd.Series, *, asset: str = "?",
    ema_fast: int = 10, ema_mid: int = 30, ema_slow: int = 60,
    atr_period: int = 14, atr_mult: float = 3.0, percent: float = 1.0,
    allow_short: bool = True,
    asset_class: AssetClass = "crypto_perp", initial_capital: float = 10_000.0,
) -> TrendResult:
    idx = pd.DatetimeIndex(ohlcv.index)
    o = ohlcv["open"].to_numpy()
    h = ohlcv["high"].to_numpy()
    low = ohlcv["low"].to_numpy()
    c = ohlcv["close"].to_numpy()
    mk = ohlcv["mark_close"].to_numpy()
    fr = funding.to_numpy()
    times = [t.to_pydatetime() for t in idx]
    n = len(c)

    ef, em, es = _ema(c, ema_fast), _ema(c, ema_mid), _ema(c, ema_slow)
    atr = _atr(h, low, c, atr_period)
    regime = np.where((ef > em) & (em > es), 1, np.where((ef < em) & (em < es), -1, 0))
    warmup = ema_slow + atr_period  # no signal until the slow EMA + ATR settle

    cost_frac = _fallback_commission_for_class(asset_class) + _fallback_slippage_for_class(asset_class)
    res = TrendResult(asset=asset, initial_capital=initial_capital)
    cash = initial_capital
    pos: _Pos | None = None
    pending_entry: int | None = None       # desired direction for next-bar entry
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
        res.trades.append(TrendTrade(
            entry_time=times[pos.entry_bar], exit_time=times[bar], direction=pos.direction,
            price_pnl=price_pnl, funding_pnl=pos.funding_accrued, cost=cost,
            net_pnl=price_pnl + pos.funding_accrued - cost, exit_reason=reason))
        pos = None

    for bar in range(n):
        # STEP 1 — next-open fills (LAST price) for decisions made last bar.
        if pending_exit is not None and pos is not None:
            _close(bar, o[bar], pending_exit)
            pending_exit = None
        if pending_entry is not None and pos is None:
            d = pending_entry
            fill = o[bar]
            qty = d * (percent * cash) / fill          # unlevered: notional = percent*equity
            stop = fill - d * atr_mult * atr[bar]      # long: below; short: above
            cash -= abs(qty) * fill * cost_frac
            res.total_cost += abs(qty) * fill * cost_frac
            pos = _Pos(direction=d, entry_bar=bar, signed_qty=qty, entry_fill=fill, stop=stop)
            pending_entry = None

        # STEP 2 — funding accrual on MARK at an 8h stamp while held.
        if pos is not None and fr[bar] == fr[bar]:  # not NaN
            cf = funding_cashflow(pos.signed_qty, mk[bar], fr[bar])
            cash += cf
            pos.funding_accrued += cf
            res.total_funding += cf
            res.funding_ledger.append(TrendFundingRow(
                timestamp=times[bar], signed_qty=pos.signed_qty, mark_price=mk[bar],
                funding_rate=fr[bar], cashflow=cf))

        # STEP 3 — intrabar TRAILING ATR stop (fill at the stop, or the gap).
        if pos is not None and pending_exit is None:
            hit = (low[bar] <= pos.stop) if pos.direction > 0 else (h[bar] >= pos.stop)
            if hit:
                fill = o[bar] if ((pos.direction > 0 and o[bar] <= pos.stop)
                                  or (pos.direction < 0 and o[bar] >= pos.stop)) else pos.stop
                _close(bar, fill, "stop")
            else:
                # ratchet the stop in the favourable direction (trend-follow):
                # never loosen it. Uses the bar's close, applied from NEXT bar.
                trail = c[bar] - pos.direction * atr_mult * atr[bar]
                pos.stop = max(pos.stop, trail) if pos.direction > 0 else min(pos.stop, trail)

        # STEP 4 — mark-to-market on MARK.
        res.equity_curve.append((times[bar], _equity(bar)))

        # STEP 5 — decide next-bar action from the (warmed-up) regime. HOLD
        # through a neutral cascade (rg==0); exit only on a CONFIRMED reversal
        # (rg == -direction) or the trailing stop (STEP 3) — faithful trend-follow.
        if bar < warmup or bar >= n - 1:
            continue
        rg = int(regime[bar])
        if pos is None and pending_entry is None:
            # enter only on a FRESH alignment (the bar the cascade flips into a
            # confirmed regime) — not every aligned bar, else a stop-out inside a
            # standing trend immediately re-enters (whipsaw).
            if rg != 0 and rg != int(regime[bar - 1]) and (allow_short or rg == 1):
                pending_entry = rg
        elif pos is not None and pending_exit is None and rg == -pos.direction:
            pending_exit = "flip"

    if pos is not None:
        bar = n - 1
        price_pnl = pos.signed_qty * (mk[bar] - pos.entry_fill)
        cash += price_pnl
        res.trades.append(TrendTrade(
            entry_time=times[pos.entry_bar], exit_time=times[bar], direction=pos.direction,
            price_pnl=price_pnl, funding_pnl=pos.funding_accrued, cost=0.0,
            net_pnl=price_pnl + pos.funding_accrued, exit_reason="end_of_data"))
    res.final_equity = cash
    return res
