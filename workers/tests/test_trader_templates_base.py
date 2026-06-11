"""Tests for the shared template base: TemplateParams, helpers."""

from __future__ import annotations

from decimal import Decimal

from marketmind_shared.schemas.trader import SignalKind
from marketmind_workers.trader.templates.base import atr_stop_for_long, hold


def test_atr_stop_for_long_subtracts_atr_times_multiple() -> None:
    # 100 - 2 * 5 = 90, quantised to 8dp.
    stop = atr_stop_for_long(Decimal("100"), Decimal("5"), Decimal("2"))
    assert stop == Decimal("90.00000000")


def test_atr_stop_for_long_handles_fractional_atr() -> None:
    # 100 - 2.5 * 1.234 = 100 - 3.085 = 96.915
    stop = atr_stop_for_long(Decimal("100"), Decimal("1.234"), Decimal("2.5"))
    assert stop == Decimal("96.91500000")


def test_hold_factory_produces_hold_signal() -> None:
    se = hold("test reason", {"x": 1.0}, Decimal("100"))
    assert se.kind is SignalKind.HOLD
    assert se.reason == "test reason"
    assert se.indicators == {"x": 1.0}
    assert se.proposed_entry_price == Decimal("100")
    # Deliberately meaningless placeholder: any caller misuse surfaces.
    assert se.proposed_stop_price == Decimal(0)


def test_hold_factory_accepts_empty_indicators() -> None:
    se = hold("warmup", {}, Decimal("50"))
    assert se.kind is SignalKind.HOLD
    assert se.indicators == {}
