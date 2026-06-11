"""UTC + candle-boundary helpers.

Two invariants the trader anchors here:

  1. Every datetime in trader memory is timezone-aware UTC.
  2. Every candle close timestamp is on the natural boundary for its
     timeframe (e.g. 4h candles close at 00:00, 04:00, 08:00, ... UTC).

`now_utc()` is the single canonical wall-clock read. Strategy
templates must never call it — strategy evaluation depends only on
the candle history passed in, so determinism is preserved. Ingestion,
scheduling, risk-window calculations, and heartbeat writers are the
only callers.

Candle boundary convention: a bar opening at `open_ts` spans
`[open_ts, open_ts + bar_duration)`. The "close" timestamp is the
START of the next bar, matching Binance / ccxt fetch_ohlcv behaviour
and the existing `services.market_data` slicing semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, overload

# Single source of truth for timeframe -> seconds. Mirrors the
# `_TIMEFRAME_MS` table in `workers.services.market_data` (in ms);
# kept independent so a future timeframe addition doesn't tightly
# couple these two modules.
_TIMEFRAME_TO_SECONDS: Final[dict[str, int]] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


class TimeError(Exception):
    """Raised when a datetime is naive, non-UTC, or a timeframe is unknown."""


def now_utc() -> datetime:
    """Canonical wall-clock read. Strategy logic must never call this."""
    return datetime.now(tz=UTC)


@overload
def require_utc(name: str, value: datetime) -> datetime: ...


@overload
def require_utc(name: str, value: None) -> None: ...


def require_utc(name: str, value: datetime | None) -> datetime | None:
    """Reject naive / non-UTC datetimes; pass None through unchanged.

    The overload lets callers preserve `None | datetime` typing without
    sprinkling `if x is not None` everywhere.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise TimeError(f"{name} must be timezone-aware UTC; got naive datetime")
    offset = value.utcoffset()
    if offset is None or offset != timedelta(0):
        raise TimeError(f"{name} must be UTC (offset 0); got offset {offset}")
    return value


def timeframe_seconds(timeframe: str) -> int:
    """Number of seconds in one bar of the given timeframe."""
    try:
        return _TIMEFRAME_TO_SECONDS[timeframe]
    except KeyError as exc:
        raise TimeError(
            f"unknown timeframe {timeframe!r}; supported: {sorted(_TIMEFRAME_TO_SECONDS)}",
        ) from exc


def candle_open_for(timeframe: str, instant: datetime) -> datetime:
    """Return the OPEN timestamp of the candle that contains `instant`.

    Bar boundaries are anchored to the UTC epoch (1970-01-01T00:00Z),
    matching Binance / ccxt convention. Works because all supported
    timeframes divide a day evenly.
    """
    require_utc("instant", instant)
    seconds = timeframe_seconds(timeframe)
    epoch_seconds = int(instant.timestamp())
    # Subtract the remainder to floor to the most recent boundary.
    aligned = epoch_seconds - (epoch_seconds % seconds)
    return datetime.fromtimestamp(aligned, tz=UTC)


def candle_close_for(timeframe: str, open_ts: datetime) -> datetime:
    """Return the CLOSE timestamp of a candle that opened at `open_ts`.

    Convention: close is exclusive — the bar spans [open_ts, close_ts)
    and `close_ts` is the start of the next bar.
    """
    require_utc("open_ts", open_ts)
    return open_ts + timedelta(seconds=timeframe_seconds(timeframe))


def next_candle_close(timeframe: str, after: datetime) -> datetime:
    """Return the first candle CLOSE strictly after `after`.

    Used by the signal-execution scheduler to compute when to next
    wake up. When `after` lies exactly on a boundary, returns one
    full bar later (strictly-after semantics) so the scheduler
    doesn't immediately re-fire.
    """
    require_utc("after", after)
    current_open = candle_open_for(timeframe, after)
    current_close = candle_close_for(timeframe, current_open)
    if current_close > after:
        return current_close
    return current_close + timedelta(seconds=timeframe_seconds(timeframe))


def utc_midnight_of(instant: datetime) -> datetime:
    """Return 00:00:00 UTC on the same calendar date as `instant`.

    Used by the daily-loss-breach window: realized + unrealized PnL
    since this anchor versus `TRADER_MAX_DAILY_LOSS_PCT * starting_equity`.
    """
    require_utc("instant", instant)
    return instant.replace(hour=0, minute=0, second=0, microsecond=0)


def utc_monday_of(instant: datetime) -> datetime:
    """Return 00:00:00 UTC on the Monday of the week containing `instant`.

    Used by the weekly-loss-breach window. `weekday()` returns
    Monday=0, so subtracting that many days walks back to the
    start of the week. Equal to `instant`-midnight when `instant`
    itself is a Monday.
    """
    require_utc("instant", instant)
    midnight = utc_midnight_of(instant)
    return midnight - timedelta(days=midnight.weekday())


__all__ = [
    "TimeError",
    "candle_close_for",
    "candle_open_for",
    "next_candle_close",
    "now_utc",
    "require_utc",
    "timeframe_seconds",
    "utc_midnight_of",
    "utc_monday_of",
]
