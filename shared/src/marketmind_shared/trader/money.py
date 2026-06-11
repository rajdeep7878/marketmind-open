"""Decimal money / price / size helpers.

Invariant: every monetary value in the trader's hot path is
`decimal.Decimal` — cash, equity, prices, sizes, fees, PnL.
Conversions from external sources (ccxt floats, JSON numbers,
JSONB JSON numbers) happen at the boundary via `to_decimal`.

Precision:
- v1 uses a single global default (8dp for both price and size).
  8 decimal places covers every Binance USDT-quoted pair's tick
  size with headroom.
- A future refactor can pull per-symbol precision from ccxt's
  `markets` metadata; the current call sites all import these
  helpers by name so swapping in per-symbol quantisation later
  is a one-file change.

Rounding policy:
- Prices: ROUND_HALF_EVEN ("banker's rounding"). Symmetric, no
  systematic bias.
- Sizes: ROUND_DOWN. Rounding a position size up by half a tick
  could breach the per-trade risk cap. Erring smaller keeps risk
  math conservative.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Final

# 8dp quantum. Covers Binance USDT pair tick sizes with headroom.
# Both columns happen to share precision in v1; keeping them as
# distinct constants documents the intent in case they diverge later.
_PRICE_QUANT: Final[Decimal] = Decimal("0.00000001")
_SIZE_QUANT: Final[Decimal] = Decimal("0.00000001")


def to_decimal(value: int | float | str | Decimal) -> Decimal:
    """Convert any numeric source to Decimal without float contamination.

    Float inputs are first converted via `str(value)` so the lossy
    binary float representation doesn't leak through.
    `Decimal(0.1)` gives
    `0.1000000000000000055511151231257827021181583404541015625` — bad.
    `Decimal(str(0.1))` gives `Decimal('0.1')` — what we want.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        return Decimal(value)
    # float path: round-trip through str to drop binary representation
    return Decimal(str(value))


def quantize_price(value: Decimal) -> Decimal:
    """Round a price to v1's price precision using banker's rounding."""
    return value.quantize(_PRICE_QUANT, rounding=ROUND_HALF_EVEN)


def quantize_size(value: Decimal) -> Decimal:
    """Round a size DOWN to v1's size precision.

    ROUND_DOWN, not ROUND_HALF_EVEN: a half-tick round-up on size
    could push the position past the per-trade risk cap. Risk math
    is conservative; trading slightly smaller is always acceptable.
    """
    return value.quantize(_SIZE_QUANT, rounding=ROUND_DOWN)


def apply_slippage_buy(open_price: Decimal, slippage_bps: Decimal) -> Decimal:
    """Buy fill price = open * (1 + slippage_bps / 10000).

    Buying suffers the worse of two prices: fill above the open by
    slippage_bps. Quantised on the way out so downstream PnL math
    works with stable precision.
    """
    factor = Decimal(1) + (slippage_bps / Decimal(10_000))
    return quantize_price(open_price * factor)


def apply_slippage_sell(open_price: Decimal, slippage_bps: Decimal) -> Decimal:
    """Sell fill price = open * (1 - slippage_bps / 10000).

    Selling suffers the worse of two prices: fill below the open by
    slippage_bps. Symmetric counterpart to apply_slippage_buy.
    """
    factor = Decimal(1) - (slippage_bps / Decimal(10_000))
    return quantize_price(open_price * factor)


def fee_for_fill(fill_price: Decimal, size: Decimal, fee_bps: Decimal) -> Decimal:
    """Fee on a single fill: fill_price * size * fee_bps / 10000.

    Returned in the quote currency, quantised at price precision.
    The executor subtracts this from cash on a BUY and adds the
    net to cash on a SELL.
    """
    notional = fill_price * size
    return quantize_price(notional * fee_bps / Decimal(10_000))


__all__ = [
    "apply_slippage_buy",
    "apply_slippage_sell",
    "fee_for_fill",
    "quantize_price",
    "quantize_size",
    "to_decimal",
]
