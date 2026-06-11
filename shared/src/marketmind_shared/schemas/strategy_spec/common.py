"""Shared primitives: Timeframe + ordering, Direction, Instrument, OrderType.

Timeframe is a StrEnum with an explicit rank function so we can enforce
"filter timeframe must be higher than primary" deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base for every spec model — forbids unknown fields, freezes instances.

    Frozen + extra=forbid catches both typos and silently-accepted nonsense
    in extracted specs. Spec values are inherently descriptive, not mutable.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


# Ordinal rank for ordering. Single source of truth — see also Phase 0 ADR-0004.
_TIMEFRAME_RANK: Final[Mapping[Timeframe, int]] = {
    Timeframe.M1: 0,
    Timeframe.M5: 1,
    Timeframe.M15: 2,
    Timeframe.M30: 3,
    Timeframe.H1: 4,
    Timeframe.H4: 5,
    Timeframe.D1: 6,
}


def timeframe_rank(tf: Timeframe) -> int:
    """Strict ordering for timeframes. Higher rank = larger bar."""
    return _TIMEFRAME_RANK[tf]


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


# Phase C C.1.1 (2026-05-26): asset_class enumerates the supported
# venues. v1 / v2-A / v2-B / v1.2 specs all default to `crypto_spot`
# (no explicit field needed), preserving bit-identity for the entire
# pre-Phase-C corpus. Per design doc §C.1, we use Pydantic's `Literal`
# rather than a StrEnum class — the values are the public surface and
# StrEnum adds no value when there's no runtime dispatching off the
# enum type. Engine + trader dispatchers (C.1.3 / C.1.4) match on the
# string values directly.
AssetClass = Literal[
    "crypto_spot",
    "fx_spot",
    "metals_spot",
    "equity_etf",
    "equity_single",
    # Phase E.3 (2026-06-06): USDT-margined crypto PERPETUAL swaps (Binance
    # USDM). Additive — every pre-E.3 spec defaults to "crypto_spot", so the
    # whole existing corpus is byte-identical. Perps differ from spot in that
    # they accrue 8h funding (on MARK price) and distinguish mark from last;
    # the engine handles those via the funding fixtures, not this enum.
    "crypto_perp",
]


class ContractSpecs(_StrictModel):
    """Lot / contract conventions for one instrument. Forward declaration
    for Phase C C.1.1; real fields land in C.3 (contract_size, min_lot,
    lot_step, tick_size — all Decimal). For crypto spot, no contract
    conventions apply (fractional units are valid); the canonical default
    on Instrument is `contract_specs=None`.
    """


class SessionHours(_StrictModel):
    """Trading-session definition for non-24/7 markets.

    Phase C C.4.1 (2026-05-26) populates the body of the C.1.1 forward
    declaration. Field list per design doc §C.4 (formally locked
    2026-05-25). The schema captures session boundaries declaratively;
    the backtest engine + trader cycle (C.5 + C.6) read these fields
    to decide which bars are tradeable, with no calendar-library
    dependency in the minimum path — `weekend_closed=True` is enforced
    structurally via `df.index.weekday >= 5`. The pandas_market_calendars
    integration (DST + per-venue holiday tables) lands in C.4-full
    when equities arrive in C.9.

    Examples (per doc §C.4):
      - 24/7 crypto: `session_hours=None` on Instrument (no SessionHours)
      - FX 24/5 (Oanda):
          SessionHours(calendar="cme_fx", open_utc="22:00",
                       close_utc="22:00", weekend_closed=True)
        (Sunday 22:00 UTC open through Friday 22:00 UTC close)
      - NYSE equity:
          SessionHours(calendar="nyse", open_utc="14:30",
                       close_utc="21:00", weekend_closed=True)

    HH:MM regex: stricter than the design doc's `\\d{2}:\\d{2}` (which
    permits "99:99") — uses `([01][0-9]|2[0-3]):[0-5][0-9]` so HH is
    constrained to 00-23 and MM to 00-59. The doc's looser regex
    would defer invalid-time rejection to the engine; our stricter
    version surfaces typos at extraction / spec-validation time.
    """

    calendar: str = Field(min_length=1, max_length=32)
    open_utc: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    close_utc: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    weekend_closed: bool = True
    pre_market_open_utc: str | None = Field(
        default=None,
        pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
    )
    post_market_close_utc: str | None = Field(
        default=None,
        pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$",
    )


class Instrument(_StrictModel):
    """A tradable instrument. v1 restricted to crypto spot pairs; Phase
    C C.1.1 (2026-05-26) added `asset_class`, `contract_specs`, and
    `session_hours` to support multi-asset extensions. All three fields
    default to crypto-spot-compatible values so every pre-Phase-C spec
    parses byte-identically.
    """

    symbol: str = Field(min_length=1, max_length=32)
    exchange: str = Field(min_length=1, max_length=32)
    quote_currency: str = Field(min_length=1, max_length=10)
    asset_class: AssetClass = "crypto_spot"
    contract_specs: ContractSpecs | None = None
    session_hours: SessionHours | None = None


__all__ = [
    "AssetClass",
    "ContractSpecs",
    "Direction",
    "Instrument",
    "OrderType",
    "SessionHours",
    "Timeframe",
    "_StrictModel",
    "timeframe_rank",
]
