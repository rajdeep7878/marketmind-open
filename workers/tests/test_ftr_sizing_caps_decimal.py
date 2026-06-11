"""PaperBroker: caps enforced, Decimal ledger invariants, lot quantization."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from marketmind_workers.ftr.config.venue_profiles import get_profile
from marketmind_workers.ftr.trader.paper_broker import QTY_STEP, PaperBroker


def _broker(cash: str = "10000") -> PaperBroker:
    return PaperBroker(profile=get_profile("kraken_pro_uk_tier0"), cash=Decimal(cash))


def test_buy_quantizes_to_lot_and_conserves_cash() -> None:
    broker = _broker()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    fill = broker.execute_buy("BTC/USDT", ts, Decimal("50000"), Decimal("2000"))
    assert fill is not None
    # lot quantization: qty is an exact multiple of QTY_STEP
    assert fill.qty % QTY_STEP == 0
    # fill price worsened by half-spread + slippage (2 + 3 bps on kraken)
    assert fill.fill_price == Decimal("50000") * (Decimal(1) + Decimal("0.0005"))
    # Decimal ledger: cash + spend + fee == initial, exactly
    assert broker.cash + fill.qty * fill.fill_price + fill.fee_paid == Decimal("10000")


def test_roundtrip_ledger_exact() -> None:
    broker = _broker()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    buy = broker.execute_buy("BTC/USDT", ts, Decimal("50000"), Decimal("2000"))
    assert buy is not None
    sell = broker.execute_sell("BTC/USDT", ts, Decimal("50000"))
    assert sell is not None
    # flat round trip at the same reference price loses exactly the costs
    spread_slip = Decimal("0.0005")
    fee_rate = Decimal("0.0040")
    buy_px = Decimal("50000") * (1 + spread_slip)
    sell_px = Decimal("50000") * (1 - spread_slip)
    expected_cash = (
        Decimal("10000")
        - buy.qty * buy_px
        - buy.qty * buy_px * fee_rate
        + buy.qty * sell_px
        - buy.qty * sell_px * fee_rate
    )
    assert broker.cash == expected_cash
    assert broker.positions == {}


def test_long_flat_one_position_per_symbol() -> None:
    broker = _broker()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    assert broker.execute_buy("BTC/USDT", ts, Decimal("50000"), Decimal("2000")) is not None
    # second buy on the same symbol refused (long/flat)
    assert broker.execute_buy("BTC/USDT", ts, Decimal("50000"), Decimal("2000")) is None
    # selling with no position refused
    assert broker.execute_sell("ETH/USDT", ts, Decimal("3000")) is None


def test_cannot_spend_more_than_cash() -> None:
    broker = _broker(cash="100")
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    fill = broker.execute_buy("BTC/USDT", ts, Decimal("50000"), Decimal("100000"))
    if fill is not None:
        assert broker.cash >= 0
        assert fill.qty * fill.fill_price + fill.fee_paid <= Decimal("100")
