"""Trader v1 — value-level helpers shared across api + workers.

This subpackage carries the Decimal / time utilities the trader's
hot path depends on. The Pydantic DTOs live alongside the rest of
the cross-service types at `marketmind_shared.schemas.trader` so
they compose naturally with the existing schema layout.

Convention:
- Every monetary value is `decimal.Decimal`. Conversions from
  ccxt / JSON / DB floats happen at the boundary via `to_decimal`.
- Every datetime is tz-aware UTC. `now_utc()` is the single
  canonical wall-clock read; strategy logic must never call it.
"""

from marketmind_shared.trader.money import (
    apply_slippage_buy,
    apply_slippage_sell,
    fee_for_fill,
    quantize_price,
    quantize_size,
    to_decimal,
)
from marketmind_shared.trader.time import (
    TimeError,
    candle_close_for,
    candle_open_for,
    next_candle_close,
    now_utc,
    require_utc,
    timeframe_seconds,
    utc_midnight_of,
    utc_monday_of,
)

__all__ = [
    "TimeError",
    "apply_slippage_buy",
    "apply_slippage_sell",
    "candle_close_for",
    "candle_open_for",
    "fee_for_fill",
    "next_candle_close",
    "now_utc",
    "quantize_price",
    "quantize_size",
    "require_utc",
    "timeframe_seconds",
    "to_decimal",
    "utc_midnight_of",
    "utc_monday_of",
]
