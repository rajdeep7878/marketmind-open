"""Pure unit tests for the scheduling-boundary helpers in jobs.py.

These functions are critical for the runner's correctness — a
mis-implemented `next_daily_at` could mean the daily summary
fires at process-start-time instead of UTC midnight, or the
weekly summary fires every day instead of every Monday.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from marketmind_workers.trader.jobs import (
    next_daily_at,
    next_minute_boundary,
    next_monday_at,
    next_n_minute_boundary,
)

# ---- next_minute_boundary --------------------------------------------------


def test_next_minute_boundary_zero_second_clamps_forward() -> None:
    """At exactly :00, return next minute (strict-after, never
    schedule for `now` itself — RQ would fire it immediately and
    blow the cadence).
    """
    now = datetime(2026, 5, 18, 14, 23, 0, tzinfo=UTC)
    nxt = next_minute_boundary(now)
    assert nxt == datetime(2026, 5, 18, 14, 24, 0, tzinfo=UTC)


def test_next_minute_boundary_strips_sub_second() -> None:
    """Microseconds and seconds dropped; minute rounded UP."""
    now = datetime(2026, 5, 18, 14, 23, 45, 678_900, tzinfo=UTC)
    nxt = next_minute_boundary(now)
    assert nxt == datetime(2026, 5, 18, 14, 24, 0, tzinfo=UTC)


def test_next_minute_boundary_wraps_hour() -> None:
    now = datetime(2026, 5, 18, 14, 59, 30, tzinfo=UTC)
    nxt = next_minute_boundary(now)
    assert nxt == datetime(2026, 5, 18, 15, 0, 0, tzinfo=UTC)


# ---- next_n_minute_boundary (used by stale-detector at n=5) ---------------


def test_next_5_minute_grid_aligns_to_floor_plus_5() -> None:
    now = datetime(2026, 5, 18, 14, 23, 45, tzinfo=UTC)
    nxt = next_n_minute_boundary(now, n=5)
    assert nxt == datetime(2026, 5, 18, 14, 25, 0, tzinfo=UTC)


def test_next_5_minute_grid_at_exact_grid_advances() -> None:
    """At :25:00 exactly, return :30 (strict-after)."""
    now = datetime(2026, 5, 18, 14, 25, 0, tzinfo=UTC)
    nxt = next_n_minute_boundary(now, n=5)
    assert nxt == datetime(2026, 5, 18, 14, 30, 0, tzinfo=UTC)


def test_next_5_minute_grid_wraps_hour() -> None:
    now = datetime(2026, 5, 18, 14, 58, 0, tzinfo=UTC)
    nxt = next_n_minute_boundary(now, n=5)
    assert nxt == datetime(2026, 5, 18, 15, 0, 0, tzinfo=UTC)


# ---- next_daily_at ---------------------------------------------------------


def test_next_daily_at_midnight_when_now_is_afternoon() -> None:
    """`hour=0, minute=0` when called at 14:00 UTC → tomorrow 00:00."""
    now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=UTC)
    nxt = next_daily_at(now, hour=0)
    assert nxt == datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)


def test_next_daily_at_fires_at_utc_midnight_not_process_start() -> None:
    """The load-bearing test for the daily-summary schedule.

    A naive implementation would schedule the daily summary
    24 hours from process start. We use `utc_midnight_of(now) +
    1 day`, which lands on TRUE UTC midnight regardless of when
    the runner boots.
    """
    now = datetime(2026, 5, 18, 14, 35, 17, 123_456, tzinfo=UTC)
    nxt = next_daily_at(now, hour=0)
    assert nxt.hour == 0
    assert nxt.minute == 0
    assert nxt.second == 0
    assert nxt.microsecond == 0
    assert nxt == datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)


def test_next_daily_at_summary_minute_offset() -> None:
    """The daily-summary tick is at 00:05 UTC each day."""
    now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=UTC)
    nxt = next_daily_at(now, hour=0, minute=5)
    assert nxt == datetime(2026, 5, 19, 0, 5, 0, tzinfo=UTC)


def test_next_daily_at_when_already_past_boundary_today() -> None:
    """At 00:30 UTC, the next 00:05 is tomorrow's, not today's."""
    now = datetime(2026, 5, 18, 0, 30, 0, tzinfo=UTC)
    nxt = next_daily_at(now, hour=0, minute=5)
    assert nxt == datetime(2026, 5, 19, 0, 5, 0, tzinfo=UTC)


def test_next_daily_at_drift_hour() -> None:
    """The drift tick is at 01:00 UTC each day."""
    now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=UTC)
    nxt = next_daily_at(now, hour=1)
    assert nxt == datetime(2026, 5, 19, 1, 0, 0, tzinfo=UTC)


# ---- next_monday_at --------------------------------------------------------


def test_next_monday_at_from_tuesday() -> None:
    """2026-05-19 is a Tuesday; next Monday is 2026-05-25."""
    tuesday = datetime(2026, 5, 19, 14, 0, 0, tzinfo=UTC)
    assert tuesday.weekday() == 1  # sanity: Tuesday
    nxt = next_monday_at(tuesday, hour=0, minute=10)
    assert nxt == datetime(2026, 5, 25, 0, 10, 0, tzinfo=UTC)


def test_next_monday_at_from_monday_after_boundary() -> None:
    """Monday 12:00 UTC → next Monday."""
    monday_noon = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)
    assert monday_noon.weekday() == 0
    nxt = next_monday_at(monday_noon, hour=0, minute=10)
    assert nxt == datetime(2026, 5, 25, 0, 10, 0, tzinfo=UTC)


def test_next_monday_at_from_monday_before_boundary() -> None:
    """Monday 00:00 UTC → same Monday 00:10 UTC (strict-after)."""
    monday_midnight = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    nxt = next_monday_at(monday_midnight, hour=0, minute=10)
    assert nxt == datetime(2026, 5, 18, 0, 10, 0, tzinfo=UTC)


def test_next_monday_at_from_sunday() -> None:
    """Sunday → next day Monday."""
    sunday = datetime(2026, 5, 17, 14, 0, 0, tzinfo=UTC)
    assert sunday.weekday() == 6
    nxt = next_monday_at(sunday, hour=0, minute=10)
    assert nxt == datetime(2026, 5, 18, 0, 10, 0, tzinfo=UTC)


def test_next_monday_at_always_lands_on_monday() -> None:
    """Property check: regardless of the input weekday, the
    result is always a Monday at the requested hour:minute.
    """
    base = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)  # Monday
    for offset_days in range(0, 14):
        now = base + timedelta(days=offset_days)
        nxt = next_monday_at(now, hour=0, minute=10)
        assert nxt.weekday() == 0, f"offset_days={offset_days}: {nxt} not Monday"
        assert nxt.hour == 0
        assert nxt.minute == 10
        assert nxt > now
