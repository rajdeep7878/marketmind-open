"""Smoke tests for the Trader v1 UTC + candle-boundary helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from marketmind_shared.trader.time import (
    TimeError,
    candle_close_for,
    candle_open_for,
    next_candle_close,
    require_utc,
    timeframe_seconds,
    utc_midnight_of,
    utc_monday_of,
)


def test_require_utc_rejects_naive_datetime() -> None:
    with pytest.raises(TimeError, match="naive"):
        require_utc("x", datetime(2026, 1, 1))  # noqa: DTZ001  # naive — what we test


def test_require_utc_rejects_non_utc_tz() -> None:
    pst = timezone(timedelta(hours=-8))
    with pytest.raises(TimeError, match="UTC"):
        require_utc("x", datetime(2026, 1, 1, tzinfo=pst))


def test_require_utc_accepts_utc_passthrough() -> None:
    dt = datetime(2026, 1, 1, tzinfo=UTC)
    assert require_utc("x", dt) is dt


def test_require_utc_none_passthrough() -> None:
    # Overload contract: None is allowed and returned unchanged.
    assert require_utc("x", None) is None


def test_timeframe_seconds_known() -> None:
    assert timeframe_seconds("4h") == 14_400
    assert timeframe_seconds("1d") == 86_400


def test_timeframe_seconds_unknown_raises() -> None:
    with pytest.raises(TimeError, match="unknown timeframe"):
        timeframe_seconds("2h")


def test_candle_open_4h_floors_to_nearest_boundary() -> None:
    # 14:23:00Z falls inside the 12:00..16:00 4h bar.
    instant = datetime(2026, 5, 18, 14, 23, tzinfo=UTC)
    assert candle_open_for("4h", instant) == datetime(2026, 5, 18, 12, 0, tzinfo=UTC)


def test_candle_open_already_on_boundary_returns_same() -> None:
    boundary = datetime(2026, 5, 18, 16, 0, tzinfo=UTC)
    assert candle_open_for("4h", boundary) == boundary


def test_candle_close_is_start_of_next_bar() -> None:
    open_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    assert candle_close_for("4h", open_ts) == datetime(2026, 5, 18, 16, 0, tzinfo=UTC)


def test_next_candle_close_strict_after_boundary() -> None:
    # When `after` lies exactly on a boundary, the next close is one
    # bar later (strict-after semantics for the scheduler).
    boundary = datetime(2026, 5, 18, 16, 0, tzinfo=UTC)
    assert next_candle_close("4h", boundary) == datetime(2026, 5, 18, 20, 0, tzinfo=UTC)


def test_next_candle_close_between_boundaries() -> None:
    middle = datetime(2026, 5, 18, 14, 23, tzinfo=UTC)
    assert next_candle_close("4h", middle) == datetime(2026, 5, 18, 16, 0, tzinfo=UTC)


def test_utc_midnight_of() -> None:
    assert utc_midnight_of(datetime(2026, 5, 18, 14, 23, 59, 999, tzinfo=UTC)) == datetime(
        2026, 5, 18, tzinfo=UTC
    )


def test_utc_monday_of_thursday_walks_back_to_monday() -> None:
    # 2026-05-21 is a Thursday; the Monday of that week is 2026-05-18.
    thursday = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    assert utc_monday_of(thursday) == datetime(2026, 5, 18, tzinfo=UTC)


def test_utc_monday_of_sunday_walks_to_prior_monday() -> None:
    # 2026-05-24 is a Sunday; the prior Monday is 2026-05-18, NOT the
    # next day. Important: weekly windows close on the last Sunday
    # 23:59:59 UTC, not Monday morning.
    sunday = datetime(2026, 5, 24, 14, 0, tzinfo=UTC)
    assert utc_monday_of(sunday) == datetime(2026, 5, 18, tzinfo=UTC)


def test_utc_monday_of_monday_returns_same_day_midnight() -> None:
    monday_afternoon = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    assert utc_monday_of(monday_afternoon) == datetime(2026, 5, 18, tzinfo=UTC)
