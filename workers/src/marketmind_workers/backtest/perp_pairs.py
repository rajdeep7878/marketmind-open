"""Phase E.3 — multi-leg perpetual-pair backtest engine (funding-aware).

A SELF-CONTAINED simulator for a market-neutral two-leg perp-pair spread
strategy (e.g. BTC/ETH log-spread mean-reversion). It does NOT touch the
single-leg vbt/iterative path — a multi-leg spec (``spec.legs`` +
``spec.spread`` set) is dispatched here; every existing single-leg spec is
untouched, so the live trader and the whole corpus are byte-identical.

LEG CONVENTION (from schemas/.../legs.py): leg A = ``spec.instrument``
(direction = ``spec.direction``), leg B = ``spec.legs[0].instrument``
(direction = ``spec.legs[0].direction``). For a dollar-neutral BTC/ETH
pair the canonical "long-spread" position is A long + B short.

================  HONESTY (the backtester must never lie)  ================
Perps are NOT spot. The two ways a perp backtest silently fabricates edge:

  1. FUNDING SIGN / BASIS. Funding is charged every 8h on the MARK price,
     not last. A LONG position PAYS funding when the rate is positive
     (longs pay shorts); a SHORT RECEIVES. The single correct formula,
     valid for both legs and both rate signs:

         funding_cashflow_leg = - signed_qty * mark_price * funding_rate

     signed_qty is + for a long leg, - for a short leg. Worked cases:
       long  (+1), rate +1bp, mark 100 -> -(+1)(100)(0.0001) = -0.01 (pays)
       short (-1), rate +1bp, mark 100 -> -(-1)(100)(0.0001) = +0.01 (recvs)
       long  (+1), rate -1bp           -> +0.01 (receives negative funding)
     Both legs of the pair accrue independently (BTC funding != ETH funding,
     BTC mark != ETH mark) and are summed.

  2. MARK vs LAST. FILLS happen at LAST price (the tradeable book, next-bar
     open). UNREALIZED PnL (the equity mark-to-market) and FUNDING NOTIONAL
     use MARK. The E.2 fixtures store both distinctly; we never conflate.

No leverage: each leg's notional is sized so leg_A + leg_B == percent*equity
(gross <= equity), keeping liquidation dormant (the paper-research rule).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
from marketmind_shared.schemas.strategy_spec import (
    Direction,
    FixedPercentEquitySizing,
    StrategySpec,
)
from marketmind_shared.schemas.strategy_spec.common import AssetClass

# Per-asset-class cost fallbacks live module-private in the fee/slippage
# models; the perp engine resolves each leg's cost by its own asset_class.
# Same-package use, mirroring iterative.py's reuse of translator internals.
from marketmind_workers.backtest.fee_model import (
    _fallback_commission_for_class,  # pyright: ignore[reportPrivateUsage]
)
from marketmind_workers.backtest.slippage_model import (
    _fallback_slippage_for_class,  # pyright: ignore[reportPrivateUsage]
)

# <repo>/workers/src/marketmind_workers/backtest/perp_pairs.py -> parents[4] = <repo>
_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "market"


# --------------------------------------------------------------------------- #
#  The funding formula — isolated so a verifier can attack it directly.
# --------------------------------------------------------------------------- #
def funding_cashflow(signed_qty: float, mark_price: float, funding_rate: float) -> float:
    """Cash a leg pays (-) or receives (+) at one 8h funding stamp.

    ``signed_qty`` > 0 long, < 0 short. Charged on MARK, not last. The sign
    is the whole ballgame: a long leg pays positive funding, a short receives.
    """
    return -signed_qty * mark_price * funding_rate


# --------------------------------------------------------------------------- #
#  Spread primitives (cross-asset). Reused by the simulator + exposed for E.4.
# --------------------------------------------------------------------------- #
def build_spread(a_close: pd.Series, b_close: pd.Series, method: str) -> pd.Series:
    """The A-vs-B spread series. 'log' = log(A) - log(B) (the E.4 choice);
    'ratio' = A / B. Inputs must share an index (the E.2 legs do)."""
    if method == "log":
        vals = np.log(a_close.to_numpy()) - np.log(b_close.to_numpy())
    elif method == "ratio":
        vals = a_close.to_numpy() / b_close.to_numpy()
    else:
        raise ValueError(f"unknown spread method {method!r}")
    return pd.Series(vals, index=a_close.index)


def spread_zscore(spread: pd.Series, period: int) -> pd.Series:
    """Rolling z-score of the spread: (x - mean) / std, sample std (ddof=1,
    matching the single-leg ZScoreCondition convention). NaN where std==0 or
    during warmup."""
    mean = spread.rolling(period).mean()
    std = spread.rolling(period).std(ddof=1)
    safe = std.where(std != 0.0)
    return (spread - mean) / safe


def rolling_correlation(a_close: pd.Series, b_close: pd.Series, period: int) -> pd.Series:
    """Rolling Pearson correlation of the two legs' LOG RETURNS over `period`.
    The regime/health filter — a drop signals decoupling (the pair tail risk)."""
    a_ret = pd.Series(np.log(a_close.to_numpy()), index=a_close.index).diff()
    b_ret = pd.Series(np.log(b_close.to_numpy()), index=b_close.index).diff()
    return cast("pd.Series", a_ret.rolling(period).corr(b_ret))


# --------------------------------------------------------------------------- #
#  Leg data + result structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LegData:
    symbol: str
    direction: Direction          # canonical long-spread side
    weight: float
    last: pd.DataFrame            # open/high/low/close/volume (LAST price)
    mark_close: pd.Series         # MARK price per bar
    funding: pd.Series            # funding_rate at 8h stamps (subset of the bar index)


@dataclass(frozen=True)
class PairTrade:
    entry_time: datetime
    exit_time: datetime
    side: int                     # +1 long-spread, -1 short-spread
    price_pnl: float              # realized PnL from both legs' price moves
    funding_pnl: float            # net funding accrued while held (both legs)
    cost: float                   # entry+exit fees+slippage, both legs
    net_pnl: float                # price_pnl + funding_pnl - cost
    n_funding_events: int
    exit_reason: str              # "reversion" | "stop" | "end_of_data"


@dataclass
class FundingLedgerRow:
    """One funding accrual, kept for empirical hand-verification."""
    timestamp: datetime
    leg_symbol: str
    signed_qty: float
    mark_price: float
    funding_rate: float
    cashflow: float


@dataclass
class PerpPairResult:
    spec_name: str
    trades: list[PairTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    funding_ledger: list[FundingLedgerRow] = field(default_factory=list)
    total_funding: float = 0.0
    total_cost: float = 0.0
    final_equity: float = 0.0
    initial_capital: float = 0.0


# --------------------------------------------------------------------------- #
#  Fixture loader
# --------------------------------------------------------------------------- #
def _leg_paths(symbol: str, fixture_dir: Path) -> tuple[Path, Path]:
    base = symbol.split("/")[0].lower()
    tag = f"binance_{base}_usdt_perp"
    return fixture_dir / f"{tag}_1h.parquet", fixture_dir / f"{tag}_funding.parquet"


def load_perp_pair_data(spec: StrategySpec, *, fixture_dir: Path | None = None) -> list[LegData]:
    """Load + index-align both legs' E.2 fixtures (last OHLCV + mark + 8h
    funding). Returns [leg A, leg B]. The E.2 fixtures share a bar grid by
    construction, so the spread is computable bar-by-bar.
    """
    if spec.legs is None or spec.spread is None:
        raise ValueError("load_perp_pair_data requires a multi-leg spec (legs + spread)")
    fdir = fixture_dir if fixture_dir is not None else _FIXTURE_DIR
    leg_specs = [
        (spec.instrument.symbol, spec.direction, 1.0),
        *((leg.instrument.symbol, leg.direction, leg.weight) for leg in spec.legs),
    ]
    frames: list[tuple[str, Direction, float, pd.DataFrame, pd.Series]] = []
    grid: pd.Index | None = None
    for symbol, direction, weight in leg_specs:
        p_ohlcv, p_fund = _leg_paths(symbol, fdir)
        ohlcv = pd.read_parquet(p_ohlcv).rename_axis(None)
        funding = cast("pd.Series", pd.read_parquet(p_fund).rename_axis(None)["funding_rate"])
        grid = ohlcv.index if grid is None else grid.intersection(ohlcv.index)
        frames.append((symbol, direction, weight, ohlcv, funding))
    assert grid is not None
    return [
        LegData(
            symbol=symbol, direction=direction, weight=weight,
            last=cast("pd.DataFrame", ohlcv.loc[grid, ["open", "high", "low", "close", "volume"]]),
            mark_close=cast("pd.Series", ohlcv.loc[grid, "mark_close"]),
            funding=funding,
        )
        for symbol, direction, weight, ohlcv, funding in frames
    ]


# --------------------------------------------------------------------------- #
#  The simulator
# --------------------------------------------------------------------------- #
def _dir_sign(direction: Direction) -> int:
    return 1 if direction is Direction.LONG else -1


@dataclass
class _Pos:
    side: int                     # +1 long-spread / -1 short-spread
    entry_bar: int
    signed_qty: list[float]       # per leg, signed (+ long, - short)
    entry_fill: list[float]       # per leg, LAST price
    n_funding: int = 0
    funding_accrued: float = 0.0


def run_perp_pair_backtest(
    spec: StrategySpec,
    legs: list[LegData],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    initial_capital: float = 10_000.0,
) -> PerpPairResult:
    """Simulate the market-neutral perp-pair spread. Signal: z-score of the
    spread (leg A vs leg B). Enter when |z| >= entry_z (corr gate permitting),
    flatten when |z| <= exit_z. Fills next-bar-open on LAST; funding accrues
    on MARK at each 8h stamp held; cost on both legs each side.
    """
    if spec.spread is None or spec.legs is None:
        raise ValueError("run_perp_pair_backtest requires a multi-leg spec")
    cfg = spec.spread
    leg_a, leg_b = legs[0], legs[1]

    # window-clamp to the shared grid
    idx = pd.DatetimeIndex(leg_a.last.index)
    lo = idx[0] if start is None else max(idx[0], pd.Timestamp(start))
    hi = idx[-1] if end is None else min(idx[-1], pd.Timestamp(end))
    mask = (idx >= lo) & (idx <= hi)
    idx = idx[mask]

    a_close = leg_a.last["close"].loc[idx]
    b_close = leg_b.last["close"].loc[idx]
    a_open = leg_a.last["open"].loc[idx].to_numpy()
    b_open = leg_b.last["open"].loc[idx].to_numpy()
    a_mark = leg_a.mark_close.loc[idx].to_numpy()
    b_mark = leg_b.mark_close.loc[idx].to_numpy()

    spread = build_spread(a_close, b_close, cfg.method)
    z = spread_zscore(spread, cfg.zscore_period).to_numpy()
    corr = (
        rolling_correlation(a_close, b_close, cfg.corr_period).to_numpy()
        if cfg.corr_period is not None
        else None
    )

    # funding rate per leg aligned to the bar grid (NaN where no stamp)
    a_fund = leg_a.funding.reindex(idx).to_numpy()
    b_fund = leg_b.funding.reindex(idx).to_numpy()

    # per-leg per-side cost fraction (fee + slippage), resolved by EACH leg's
    # own asset_class (both crypto_perp for a perp pair).
    leg_acs: list[AssetClass] = [
        spec.instrument.asset_class,
        spec.legs[0].instrument.asset_class,
    ]
    cost_frac = [
        _fallback_commission_for_class(ac) + _fallback_slippage_for_class(ac)
        for ac in leg_acs
    ]
    # gross notional fraction of equity per entry (leg_A + leg_B == gross, so
    # gross <= equity keeps the pair UNLEVERED — liquidation dormant).
    sizing = spec.position_sizing
    gross_frac = sizing.percent if isinstance(sizing, FixedPercentEquitySizing) else 1.0
    dsign = [_dir_sign(leg_a.direction), _dir_sign(leg_b.direction)]
    weights = [1.0, leg_b.weight]
    opens = [a_open, b_open]
    marks = [a_mark, b_mark]
    funds = [a_fund, b_fund]
    times = [t.to_pydatetime() for t in idx]

    res = PerpPairResult(spec_name=spec.name, initial_capital=initial_capital)
    cash = initial_capital
    pos: _Pos | None = None
    pending_entry_side: int | None = None
    pending_exit_reason: str | None = None
    n = len(idx)

    def _equity(bar: int) -> float:
        if pos is None:
            return cash
        unreal = sum(
            pos.signed_qty[k] * (marks[k][bar] - pos.entry_fill[k]) for k in (0, 1)
        )
        return cash + unreal

    for bar in range(n):
        # STEP 1 — execute pending fills at THIS bar's open (LAST price).
        if pending_exit_reason is not None and pos is not None:
            price_pnl = sum(
                pos.signed_qty[k] * (opens[k][bar] - pos.entry_fill[k]) for k in (0, 1)
            )
            cost = sum(
                abs(pos.signed_qty[k]) * opens[k][bar] * cost_frac[k] for k in (0, 1)
            )
            cash += price_pnl - cost
            res.total_cost += cost
            res.trades.append(PairTrade(
                entry_time=times[pos.entry_bar], exit_time=times[bar], side=pos.side,
                price_pnl=price_pnl, funding_pnl=pos.funding_accrued, cost=cost,
                net_pnl=price_pnl + pos.funding_accrued - cost,
                n_funding_events=pos.n_funding, exit_reason=pending_exit_reason))
            pos = None
            pending_exit_reason = None
        if pending_entry_side is not None and pos is None:
            side = pending_entry_side
            gross = gross_frac * cash
            # leg_A_notional + leg_B_notional == gross, leg_B = weight * leg_A
            a_notional = gross / (1.0 + weights[1])
            notionals = [a_notional, weights[1] * a_notional]
            signed_qty = [side * dsign[k] * (notionals[k] / opens[k][bar]) for k in (0, 1)]
            entry_fill = [opens[k][bar] for k in (0, 1)]
            entry_cost = sum(abs(signed_qty[k]) * entry_fill[k] * cost_frac[k] for k in (0, 1))
            cash -= entry_cost
            res.total_cost += entry_cost
            pos = _Pos(side=side, entry_bar=bar, signed_qty=signed_qty, entry_fill=entry_fill)
            pending_entry_side = None

        # STEP 2 — funding accrual on MARK at an 8h stamp while held.
        if pos is not None:
            for k in (0, 1):
                rate = funds[k][bar]
                if rate == rate:  # not NaN -> this bar carries a funding stamp
                    cf = funding_cashflow(pos.signed_qty[k], marks[k][bar], rate)
                    cash += cf
                    pos.funding_accrued += cf
                    pos.n_funding += 1
                    res.total_funding += cf
                    res.funding_ledger.append(FundingLedgerRow(
                        timestamp=times[bar], leg_symbol=legs[k].symbol,
                        signed_qty=pos.signed_qty[k], mark_price=marks[k][bar],
                        funding_rate=rate, cashflow=cf))

        # STEP 3 — mark-to-market equity on MARK.
        res.equity_curve.append((times[bar], _equity(bar)))

        # STEP 4 — decide next-bar action from the signal (no last-bar fill).
        if bar >= n - 1:
            continue
        zb = z[bar]
        if pos is None and pending_entry_side is None:
            if zb == zb and abs(zb) >= cfg.entry_z:
                gate_ok = True
                if corr is not None and cfg.corr_min is not None:
                    cb = corr[bar]
                    gate_ok = cb == cb and cb >= cfg.corr_min
                if gate_ok:
                    pending_entry_side = -int(math.copysign(1, zb))  # z<0 -> long-spread
        elif pos is not None and pending_exit_reason is None and zb == zb:
            if abs(zb) <= cfg.exit_z:
                pending_exit_reason = "reversion"
            elif cfg.stop_z is not None and abs(zb) >= cfg.stop_z:
                pending_exit_reason = "stop"

    # flatten any open position at the last bar's close-equivalent (mark)
    if pos is not None:
        bar = n - 1
        price_pnl = sum(pos.signed_qty[k] * (marks[k][bar] - pos.entry_fill[k]) for k in (0, 1))
        cash += price_pnl
        res.trades.append(PairTrade(
            entry_time=times[pos.entry_bar], exit_time=times[bar], side=pos.side,
            price_pnl=price_pnl, funding_pnl=pos.funding_accrued, cost=0.0,
            net_pnl=price_pnl + pos.funding_accrued, n_funding_events=pos.n_funding,
            exit_reason="end_of_data"))

    res.final_equity = cash
    return res
