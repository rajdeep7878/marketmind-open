"""PaperBroker — the SOLE Broker implementation in FTR.

Fills are simulated at the next 1m close, price worsened by half-spread +
slippage from the configured venue profile; fees charged per profile;
Decimal ledger throughout. There is no other Broker class and no interface
through which one could be injected (test_ftr_paper_only_no_private_api).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

import structlog

from marketmind_workers.ftr.config.venue_profiles import VenueProfile
from marketmind_workers.ftr.trader.execution_mode import EXECUTION_MODE, ExecutionMode

logger = structlog.get_logger(__name__)

QTY_STEP = Decimal("0.00001")


@dataclass(frozen=True)
class PaperFill:
    ts_utc: datetime
    symbol: str
    side: str  # 'buy' | 'sell'
    qty: Decimal
    reference_price: Decimal  # the 1m close used as reference
    fill_price: Decimal  # worsened
    fee_paid: Decimal
    venue_profile: str


@dataclass
class Position:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    opened_at: datetime


@dataclass
class PaperBroker:
    """Decimal-ledger paper broker. Long/flat, spot-only by construction."""

    profile: VenueProfile
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[PaperFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        assert EXECUTION_MODE is ExecutionMode.PAPER, "FTR has no live mode."

    # -- pricing -----------------------------------------------------------

    def _worsen(self, reference: Decimal, side: str, symbol: str) -> Decimal:
        bps = Decimal(str(self.profile.half_spread_bps.for_symbol(symbol))) + Decimal(
            str(self.profile.slippage_bps)
        )
        frac = bps * Decimal("0.0001")
        return reference * (Decimal(1) + frac) if side == "buy" else reference * (
            Decimal(1) - frac
        )

    def _fee(self, notional: Decimal) -> Decimal:
        return notional * Decimal(str(self.profile.taker_fee_bps)) * Decimal("0.0001")

    # -- order execution (next-1m-close fills, called by the runner) --------

    def execute_buy(
        self, symbol: str, ts: datetime, reference_close: Decimal, target_notional: Decimal
    ) -> PaperFill | None:
        if symbol in self.positions:
            return None  # long/flat: one position per symbol
        fill_px = self._worsen(reference_close, "buy", symbol)
        spend = min(target_notional, self.cash)
        qty = ((spend / fill_px) / QTY_STEP).to_integral_value(rounding=ROUND_DOWN) * QTY_STEP
        if qty <= 0:
            return None
        notional = qty * fill_px
        fee = self._fee(notional)
        if notional + fee > self.cash:
            qty -= QTY_STEP
            if qty <= 0:
                return None
            notional = qty * fill_px
            fee = self._fee(notional)
        self.cash -= notional + fee
        self.positions[symbol] = Position(
            symbol=symbol, qty=qty, avg_entry_price=fill_px, opened_at=ts
        )
        fill = PaperFill(
            ts_utc=ts,
            symbol=symbol,
            side="buy",
            qty=qty,
            reference_price=reference_close,
            fill_price=fill_px,
            fee_paid=fee,
            venue_profile=self.profile.name,
        )
        self.fills.append(fill)
        logger.info(
            "ftr_paper_fill", side="buy", symbol=symbol, qty=str(qty), px=str(fill_px)
        )
        return fill

    def execute_sell(
        self, symbol: str, ts: datetime, reference_close: Decimal
    ) -> PaperFill | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        fill_px = self._worsen(reference_close, "sell", symbol)
        notional = pos.qty * fill_px
        fee = self._fee(notional)
        self.cash += notional - fee
        fill = PaperFill(
            ts_utc=ts,
            symbol=symbol,
            side="sell",
            qty=pos.qty,
            reference_price=reference_close,
            fill_price=fill_px,
            fee_paid=fee,
            venue_profile=self.profile.name,
        )
        self.fills.append(fill)
        del self.positions[symbol]
        logger.info(
            "ftr_paper_fill", side="sell", symbol=symbol, qty=str(fill.qty), px=str(fill_px)
        )
        return fill

    # -- valuation ----------------------------------------------------------

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        value = self.cash
        for sym, pos in self.positions.items():
            value += pos.qty * marks.get(sym, pos.avg_entry_price)
        return value

    def gross_exposure_pct(self, marks: dict[str, Decimal]) -> float:
        eq = self.equity(marks)
        if eq <= 0:
            return 0.0
        held = sum(
            pos.qty * marks.get(sym, pos.avg_entry_price)
            for sym, pos in self.positions.items()
        )
        return float(held / eq)
