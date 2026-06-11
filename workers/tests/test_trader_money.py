"""Smoke tests for the Trader v1 Decimal money helpers."""

from __future__ import annotations

from decimal import Decimal

from marketmind_shared.trader.money import (
    apply_slippage_buy,
    apply_slippage_sell,
    fee_for_fill,
    quantize_price,
    quantize_size,
    to_decimal,
)


def test_to_decimal_from_float_avoids_binary_repr() -> None:
    # The contract: Decimal(0.1) leaks the binary float representation
    # (0.10000000000000000555...). Our helper round-trips through str
    # to drop it.
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(0.2) == Decimal("0.2")


def test_to_decimal_passes_decimal_through_identity() -> None:
    # No copy on the Decimal path — we deliberately return the same
    # object reference to make the no-op explicit at call sites.
    d = Decimal("3.14")
    assert to_decimal(d) is d


def test_to_decimal_from_int_and_str() -> None:
    assert to_decimal(42) == Decimal("42")
    assert to_decimal("3.14") == Decimal("3.14")


def test_quantize_price_uses_8dp_banker_rounding() -> None:
    # 8dp = our v1 default. Banker's rounding: 0.5 -> nearest even.
    assert quantize_price(Decimal("60000.000000005")) == Decimal("60000.00000000")
    assert quantize_price(Decimal("60000.000000015")) == Decimal("60000.00000002")


def test_quantize_size_rounds_down_never_up() -> None:
    # ROUND_DOWN — risk math must err smaller, never larger.
    assert quantize_size(Decimal("0.123456789")) == Decimal("0.12345678")
    assert quantize_size(Decimal("0.999999999")) == Decimal("0.99999999")


def test_apply_slippage_buy_adds_bps_to_open() -> None:
    # 100 + 0.1% (10 bps) = 100.10
    assert apply_slippage_buy(Decimal("100"), Decimal("10")) == Decimal("100.10000000")


def test_apply_slippage_sell_subtracts_bps_from_open() -> None:
    # 100 - 0.1% = 99.90
    assert apply_slippage_sell(Decimal("100"), Decimal("10")) == Decimal("99.90000000")


def test_apply_slippage_zero_bps_is_no_op() -> None:
    # Defensive: zero slippage should equal the open exactly (up to
    # quantisation).
    assert apply_slippage_buy(Decimal("100"), Decimal("0")) == Decimal("100.00000000")
    assert apply_slippage_sell(Decimal("100"), Decimal("0")) == Decimal("100.00000000")


def test_fee_for_fill_basic_arithmetic() -> None:
    # 100 * 1 * 10 / 10000 = 0.10
    assert fee_for_fill(Decimal("100"), Decimal("1"), Decimal("10")) == Decimal("0.10000000")
    # Half-size halves fee.
    assert fee_for_fill(Decimal("100"), Decimal("0.5"), Decimal("10")) == Decimal("0.05000000")


def test_fee_for_fill_zero_bps_yields_zero() -> None:
    assert fee_for_fill(Decimal("100"), Decimal("1"), Decimal("0")) == Decimal("0E-8")
