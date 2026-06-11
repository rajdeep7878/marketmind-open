"""Tests for the trader v1 market-data ingestion loop.

Split into three layers:

  - Pure-helper unit tests (no DB, no network): `_filter_closed_candles`,
    `_detect_gaps_in_timestamps`.
  - Stateful unit tests using fakeredis: the error-state machine
    (`_update_error_state`) and its threshold / recovery transitions.
  - Integration tests (`@pytest.mark.integration`) using a real
    PostgresContainer + fakeredis + a Protocol-implementing fake
    adapter. These exercise `ingest_one_cycle` end-to-end. Opt-in
    via `pytest -m integration`.

The fake adapter (`_FakeExchangeAdapter`) implements the
`ExchangeAdapter` Protocol structurally — no `cast` needed at call
sites; pyright accepts it via duck typing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from fakeredis import FakeRedis
from marketmind_workers.trader.config import TraderSettings, get_trader_settings
from marketmind_workers.trader.exchanges import ExchangeAdapter, IngestionError
from marketmind_workers.trader.ingestion import (
    IngestionResult,
    _detect_gaps_in_timestamps,
    _filter_closed_candles,
    _update_error_state,
    ingest_one_cycle,
)

# ---- Pure-helper tests -----------------------------------------------------


def test_filter_closed_candles_drops_in_flight_bar() -> None:
    """The most recent bar is "in flight" — close_ts > now − safety_margin.
    Filter drops it; older bars are retained.
    """
    now = datetime(2026, 5, 18, 12, 30, tzinfo=UTC)

    def _bar(open_ts: datetime) -> list[float]:
        return [
            float(int(open_ts.timestamp() * 1000)),
            100.0,
            101.0,
            99.0,
            100.5,
            1000.0,
        ]

    ohlcv = [
        _bar(datetime(2026, 5, 18, 4, 0, tzinfo=UTC)),  # closes 08:00 — closed
        _bar(
            datetime(2026, 5, 18, 8, 0, tzinfo=UTC)
        ),  # closes 12:00 — closed (well before 12:29:30 cutoff)
        _bar(datetime(2026, 5, 18, 12, 0, tzinfo=UTC)),  # closes 16:00 — in flight
    ]
    filtered = _filter_closed_candles(ohlcv, "4h", now)
    assert len(filtered) == 2
    assert filtered[0][0] == int(datetime(2026, 5, 18, 4, 0, tzinfo=UTC).timestamp() * 1000)
    assert filtered[1][0] == int(datetime(2026, 5, 18, 8, 0, tzinfo=UTC).timestamp() * 1000)


def test_filter_closed_candles_keeps_all_when_fully_closed() -> None:
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    ohlcv = [
        [
            float(int(datetime(2026, 5, 18, h, 0, tzinfo=UTC).timestamp() * 1000)),
            100.0,
            101.0,
            99.0,
            100.5,
            1000.0,
        ]
        for h in (0, 4, 8, 12, 16)
    ]
    filtered = _filter_closed_candles(ohlcv, "4h", now)
    assert len(filtered) == 5


def test_filter_closed_candles_empty_input_returns_empty() -> None:
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    assert _filter_closed_candles([], "4h", now) == []


def test_detect_gaps_contiguous_4h_series_has_zero_gaps() -> None:
    base = datetime(2026, 5, 18, tzinfo=UTC)
    ts = [base + timedelta(hours=4 * i) for i in range(10)]
    assert _detect_gaps_in_timestamps(ts, "4h") == 0


def test_detect_gaps_missing_one_bar_reports_one_gap() -> None:
    base = datetime(2026, 5, 18, tzinfo=UTC)
    # Bars 0, 1, 3, 4: bar 2 (i=2) is missing.
    ts = [base + timedelta(hours=4 * i) for i in (0, 1, 3, 4)]
    assert _detect_gaps_in_timestamps(ts, "4h") == 1


def test_detect_gaps_handles_empty_and_singleton() -> None:
    base = datetime(2026, 5, 18, tzinfo=UTC)
    assert _detect_gaps_in_timestamps([], "4h") == 0
    assert _detect_gaps_in_timestamps([base], "4h") == 0


def test_detect_gaps_tolerates_one_second_drift() -> None:
    """A 1-second clock-drift is within tolerance; not a gap."""
    base = datetime(2026, 5, 18, tzinfo=UTC)
    ts = [
        base,
        base + timedelta(hours=4, seconds=1),  # +1 second drift
        base + timedelta(hours=8),
    ]
    assert _detect_gaps_in_timestamps(ts, "4h") == 0


# ---- _update_error_state state machine (fakeredis) -------------------------


def test_update_error_state_increments_count_on_failure() -> None:
    fr = FakeRedis(decode_responses=False)
    u1 = _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    u2 = _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    assert u1.new_count == 1
    assert u1.action == "none"
    assert u2.new_count == 2
    assert u2.action == "none"


def test_update_error_state_fires_failure_alert_on_threshold() -> None:
    """3rd consecutive failure crosses INTO the failure state ⇒ fire."""
    fr = FakeRedis(decode_responses=False)
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)  # 1
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)  # 2
    u3 = _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)  # 3
    assert u3.new_count == 3
    assert u3.action == "fire_failure"


def test_update_error_state_suppresses_failure_alert_after_threshold() -> None:
    """4th and 5th consecutive failures do NOT re-fire — already alerted."""
    fr = FakeRedis(decode_responses=False)
    for _ in range(3):
        _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    u4 = _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    u5 = _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    assert u4.new_count == 4
    assert u4.action == "none"
    assert u5.new_count == 5
    assert u5.action == "none"


def test_update_error_state_fires_recovery_after_threshold_streak() -> None:
    """First success after a streak that hit threshold ⇒ fire recovery."""
    fr = FakeRedis(decode_responses=False)
    for _ in range(3):
        _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    recovery = _update_error_state(fr, "BTC/USDT", "4h", succeeded=True)
    assert recovery.new_count == 0
    assert recovery.action == "fire_recovery"
    assert recovery.streak_length == 3


def test_update_error_state_fires_recovery_after_extended_streak() -> None:
    """Streak length carries into the recovery payload — 5 failures
    then recovery reports streak_length=5.
    """
    fr = FakeRedis(decode_responses=False)
    for _ in range(5):
        _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    recovery = _update_error_state(fr, "BTC/USDT", "4h", succeeded=True)
    assert recovery.action == "fire_recovery"
    assert recovery.streak_length == 5


def test_update_error_state_no_recovery_below_threshold() -> None:
    """Success after a sub-threshold streak (1 or 2 failures) does
    NOT fire a recovery alert — that's a transient blip operator
    never needed to know about.
    """
    fr = FakeRedis(decode_responses=False)
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)  # 1
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)  # 2
    recovery = _update_error_state(fr, "BTC/USDT", "4h", succeeded=True)
    assert recovery.action == "none"


def test_update_error_state_single_failure_no_recovery() -> None:
    fr = FakeRedis(decode_responses=False)
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    recovery = _update_error_state(fr, "BTC/USDT", "4h", succeeded=True)
    assert recovery.action == "none"


def test_update_error_state_new_streak_after_recovery_can_alert_again() -> None:
    """After recovery, both flags are cleared. A new streak that
    again hits threshold fires another failure alert.
    """
    fr = FakeRedis(decode_responses=False)
    for _ in range(3):
        _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=True)  # recovery
    # Second streak.
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    second_threshold = _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    assert second_threshold.action == "fire_failure"


def test_update_error_state_noop_without_redis() -> None:
    # No client => always action='none'. Caller's alert logic never fires.
    u_fail = _update_error_state(None, "BTC/USDT", "4h", succeeded=False)
    u_ok = _update_error_state(None, "BTC/USDT", "4h", succeeded=True)
    assert u_fail.action == "none"
    assert u_ok.action == "none"


def test_update_error_state_isolates_pairs() -> None:
    """Per-pair scoping: BTC failures don't affect ETH state."""
    fr = FakeRedis(decode_responses=False)
    for _ in range(3):
        _update_error_state(fr, "BTC/USDT", "4h", succeeded=False)
    # ETH is still on a fresh streak.
    eth = _update_error_state(fr, "ETH/USDT", "4h", succeeded=False)
    assert eth.new_count == 1
    assert eth.action == "none"


# ---- IngestionResult model -------------------------------------------------


def test_ingestion_result_defaults_to_zero() -> None:
    r = IngestionResult()
    assert r.pairs_attempted == 0
    assert r.candles_inserted == 0


# IngestionResult inherits `frozen=True` from `_StrictModel`; the
# generic frozen-mutation assertion lives in `test_trader_schemas`
# (`test_candle_accepts_utc_and_is_frozen`). Not repeated here.


# ---- Integration tests (opt-in) --------------------------------------------


pytestmark_integration = pytest.mark.integration


class _FakeExchangeAdapter:
    """Test double implementing the `ExchangeAdapter` Protocol structurally.

    Passes to `ingest_one_cycle(..., adapter=fake)` directly — no
    `cast` needed; pyright accepts the Protocol via duck typing.

    Failure modes are configured per `(symbol, timeframe)` pair via
    `fail_on_recent` / `fail_on_since` sets. Optionally the fake
    can be flipped from failing to succeeding mid-test by toggling
    those sets.
    """

    def __init__(
        self,
        ohlcv_by_pair: dict[tuple[str, str], list[list[float]]],
        *,
        fail_on_recent: set[tuple[str, str]] | None = None,
        fail_on_since: set[tuple[str, str]] | None = None,
    ) -> None:
        self._ohlcv = ohlcv_by_pair
        self.fail_on_recent: set[tuple[str, str]] = set(fail_on_recent or set())
        self.fail_on_since: set[tuple[str, str]] = set(fail_on_since or set())
        self.recent_calls: list[tuple[str, str, int]] = []
        self.since_calls: list[tuple[str, str, int, int]] = []

    def fetch_recent_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> list[list[float]]:
        self.recent_calls.append((symbol, timeframe, limit))
        if (symbol, timeframe) in self.fail_on_recent:
            raise IngestionError(f"simulated failure for {symbol} {timeframe}")
        return list(self._ohlcv.get((symbol, timeframe), []))

    def fetch_ohlcv_since(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int = 1000,
    ) -> list[list[float]]:
        self.since_calls.append((symbol, timeframe, since_ms, limit))
        if (symbol, timeframe) in self.fail_on_since:
            raise IngestionError(f"simulated since-fetch failure for {symbol} {timeframe}")
        return list(self._ohlcv.get((symbol, timeframe), []))


def _adapter(fake: _FakeExchangeAdapter) -> ExchangeAdapter:
    """Tiny helper to make Protocol-typing explicit at call sites
    without sprinkling annotations.
    """
    return fake


@pytest.fixture(scope="module")
def pg_container() -> Iterator[object]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: object) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def _clean_trader_tables(database_url: str) -> None:
    """TRUNCATE the trader tables we touch in this file, between tests.

    The trader_strategy_versions table has a referenced FK from
    trader_signals etc.; we only TRUNCATE the candle / event / alert
    tables this loop writes to.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE trader_candles, trader_risk_events, trader_alerts RESTART IDENTITY",
        )
        conn.commit()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    """Narrow the symbols/timeframes to a single pair for predictable
    integration tests.
    """
    monkeypatch.setenv("TRADER_SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("TRADER_TIMEFRAMES", "4h")
    get_trader_settings.cache_clear()
    return get_trader_settings()


def _build_4h_candles(
    start: datetime,
    n: int,
    *,
    skip_indices: set[int] | None = None,
) -> list[list[float]]:
    """Build N 4h candles starting at `start`. Optionally skip bars
    at the given indices to simulate gaps.
    """
    skip = skip_indices or set()
    out: list[list[float]] = []
    for i in range(n):
        if i in skip:
            continue
        open_ts = start + timedelta(hours=4 * i)
        out.append(
            [
                float(int(open_ts.timestamp() * 1000)),
                100.0 + i,
                101.0 + i,
                99.0 + i,
                100.5 + i,
                1000.0,
            ],
        )
    return out


@pytestmark_integration
def test_cycle_inserts_closed_candles(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    start = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    candles = _build_4h_candles(start, n=11)  # last bar opens 16:00, closes 20:00 — all closed
    fake = _FakeExchangeAdapter({("BTC/USDT", "4h"): candles})
    fr = FakeRedis(decode_responses=False)

    result = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        redis=fr,
        now=now,
    )

    assert result.pairs_attempted == 1
    assert result.pairs_succeeded == 1
    assert result.pairs_failed == 0
    assert result.candles_inserted == 11

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_candles WHERE symbol='BTC/USDT' AND timeframe='4h'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 11


@pytestmark_integration
def test_cycle_is_idempotent_on_replay(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """ON CONFLICT DO NOTHING: re-running the same cycle inserts no
    new rows. This is the load-bearing restart-safety property.
    """
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    start = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    candles = _build_4h_candles(start, n=11)
    fake = _FakeExchangeAdapter({("BTC/USDT", "4h"): candles})
    fr = FakeRedis(decode_responses=False)

    first = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        redis=fr,
        now=now,
    )
    second = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        redis=fr,
        now=now,
    )

    assert first.candles_inserted == 11
    assert second.candles_inserted == 0  # duplicates skipped
    assert second.pairs_succeeded == 1


@pytestmark_integration
def test_cycle_drops_in_flight_bar(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """Fake returns 11 bars; the most recent two are "in flight"
    relative to `now`. Those must NOT be persisted.
    """
    # now = 2026-05-18 12:30, cutoff = 12:29:30. Bars open at
    # 2026-05-17 00:00 .. 2026-05-18 16:00 (step 4h):
    #   i=0..8: close_ts in [2026-05-17 04:00, 2026-05-18 12:00] — closed
    #   i=9:    close_ts = 2026-05-18 16:00 — in flight
    #   i=10:   close_ts = 2026-05-18 20:00 — in flight
    now = datetime(2026, 5, 18, 12, 30, tzinfo=UTC)
    start = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    candles = _build_4h_candles(start, n=11)
    fake = _FakeExchangeAdapter({("BTC/USDT", "4h"): candles})

    result = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        now=now,
    )

    assert result.candles_inserted == 9


@pytestmark_integration
def test_cycle_emits_stale_data_event_on_unfilled_gap(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """A gap that the backfill cannot fill writes a stale_data risk
    event. We simulate this by returning the SAME gappy series from
    both fetch_recent_ohlcv and fetch_ohlcv_since.
    """
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    start = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    # Skip bar index 3 — leaves a single-bar gap.
    candles = _build_4h_candles(start, n=11, skip_indices={3})
    fake = _FakeExchangeAdapter({("BTC/USDT", "4h"): candles})

    result = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        now=now,
    )

    assert result.gaps_detected >= 1
    assert result.backfill_attempts == 1
    assert result.stale_data_events == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, severity, symbol FROM trader_risk_events ORDER BY ts DESC LIMIT 1",
        )
        row = cur.fetchone()
        assert row is not None
        event_type, severity, symbol = row
        assert event_type == "stale_data"
        assert severity == "warning"
        assert symbol == "BTC/USDT"


@pytestmark_integration
def test_cycle_emits_data_feed_failure_alert_on_threshold_only(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """State-transition semantics:
      - 3rd consecutive failure: fires ONE critical alert.
      - 4th and 5th consecutive failures: suppressed (already alerted).
    Only one `critical` row in trader_alerts after 5 failures.
    """
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    fake = _FakeExchangeAdapter({}, fail_on_recent={("BTC/USDT", "4h")})
    fr = FakeRedis(decode_responses=False)

    results = [
        ingest_one_cycle(
            database_url,
            settings,
            adapter=_adapter(fake),
            redis=fr,
            now=now,
        )
        for _ in range(5)
    ]

    # First two failures: no alert.
    assert results[0].data_feed_failure_alerts == 0
    assert results[1].data_feed_failure_alerts == 0
    # Third: threshold transition, fires ONE alert.
    assert results[2].data_feed_failure_alerts == 1
    # Fourth and fifth: suppressed (state machine guards re-fires).
    assert results[3].data_feed_failure_alerts == 0
    assert results[4].data_feed_failure_alerts == 0

    # Exactly ONE critical alert row in the DB despite 5 failed cycles.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_alerts WHERE severity = 'critical'",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


@pytestmark_integration
def test_cycle_emits_recovery_alert_after_failure_streak(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """After a streak that fired a failure alert, the first
    successful fetch fires a `data_feed_recovery` alert (warning).
    Sets up: 3 failures → recovery via toggling fail_on_recent off.
    """
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    fake = _FakeExchangeAdapter(
        {("BTC/USDT", "4h"): _build_4h_candles(datetime(2026, 5, 17, tzinfo=UTC), n=5)},
        fail_on_recent={("BTC/USDT", "4h")},
    )
    fr = FakeRedis(decode_responses=False)

    # Three failures to trip the threshold.
    for _ in range(3):
        ingest_one_cycle(
            database_url,
            settings,
            adapter=_adapter(fake),
            redis=fr,
            now=now,
        )

    # Toggle the fake off failure mode and run again — should fire recovery.
    fake.fail_on_recent.clear()
    recovered = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        redis=fr,
        now=now,
    )
    assert recovered.data_feed_recovery_alerts == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT channel, severity, subject FROM trader_alerts "
            "WHERE severity = 'warning' ORDER BY ts DESC LIMIT 1",
        )
        row = cur.fetchone()
        assert row is not None
        channel, severity, subject = row
        assert channel == "telegram"
        assert severity == "warning"
        assert "recovered" in subject.lower()
        assert "BTC/USDT" in subject


@pytestmark_integration
def test_cycle_no_recovery_alert_below_threshold(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """A streak that never reached the threshold (1-2 failures) does
    NOT fire a recovery alert on resumption. Transient blip, not
    operator-visible.
    """
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    fake = _FakeExchangeAdapter(
        {("BTC/USDT", "4h"): _build_4h_candles(datetime(2026, 5, 17, tzinfo=UTC), n=5)},
        fail_on_recent={("BTC/USDT", "4h")},
    )
    fr = FakeRedis(decode_responses=False)

    # Two failures: below threshold.
    for _ in range(2):
        ingest_one_cycle(
            database_url,
            settings,
            adapter=_adapter(fake),
            redis=fr,
            now=now,
        )

    fake.fail_on_recent.clear()
    recovered = ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        redis=fr,
        now=now,
    )
    assert recovered.data_feed_recovery_alerts == 0

    # No warning-severity alert row should exist (since no failure
    # alert was fired either, the entire streak is silent).
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_alerts WHERE severity IN ('critical', 'warning')",
        )
        row = cur.fetchone()
        assert row is not None
        # stale_data events also write warnings, but this fixture has
        # no gaps. Total = 0.
        assert row[0] == 0


@pytestmark_integration
def test_cycle_heartbeats_existing_bot_run(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """When run_id is provided, the cycle touches last_heartbeat_at."""
    now = datetime(2026, 5, 18, 22, 0, tzinfo=UTC)
    # Seed a trader_bot_runs row with an obviously-stale heartbeat.
    run_id = uuid4()
    stale_ts = datetime(2025, 1, 1, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_bot_runs
                (id, loop_name, started_at, last_heartbeat_at, status, worker_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (str(run_id), "ingestion", stale_ts, stale_ts, "running", "test-worker"),
        )
        conn.commit()

    candles = _build_4h_candles(datetime(2026, 5, 17, tzinfo=UTC), n=5)
    fake = _FakeExchangeAdapter({("BTC/USDT", "4h"): candles})
    ingest_one_cycle(
        database_url,
        settings,
        adapter=_adapter(fake),
        run_id=run_id,
        now=now,
    )

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT last_heartbeat_at FROM trader_bot_runs WHERE id = %s",
            (str(run_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] > stale_ts


# Integration tests in this file are decorated with
# `@pytestmark_integration` (= `pytest.mark.integration`) so they
# run only via `pytest -m integration`. Pure-helper tests above are
# unmarked and run by default.


# ===========================================================================
# Phase C C.6 — weekend-skip integration tests
# ===========================================================================
#
# End-to-end verification of the trader-cycle weekend-skip dispatch
# (helper at workers/.../trader/session_skip.py, integrated at
# ingestion.py:498-518). Three invariants under test:
#
#   1. Crypto bit-identity (THE C.6 load-bearing regression): crypto_spot
#      cycles must proceed normally on weekends (the 3 production
#      strategies see byte-identical behaviour vs pre-C.6).
#   2. FX symbols on weekends are SKIPPED — zero adapter calls,
#      pairs_skipped_weekend counter bumps, zero alerts.
#   3. THE alert-suppression invariant: 4 consecutive weekend cycles
#      for an fx_spot symbol produce ZERO data_feed_failure alerts.
#
# Reuses the existing testcontainer + fake-redis fixtures (no new
# fixture infrastructure needed).


class _RecordingFakeAdapter:
    """Phase C C.6 fake — records every fetch call site. Used to confirm
    weekend-skip pre-empts the fetch (zero recorded calls on success).
    """

    def __init__(self) -> None:
        self.fetch_recent_calls: list[tuple[str, str, int]] = []
        self.fetch_since_calls: list[tuple[str, str, int, int]] = []

    def fetch_recent_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200,
    ) -> list[list[float]]:
        self.fetch_recent_calls.append((symbol, timeframe, limit))
        ts_ms = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000.0)
        return [[float(ts_ms), 1.10, 1.11, 1.09, 1.10, 1000.0]]

    def fetch_ohlcv_since(
        self, symbol: str, timeframe: str, since_ms: int, limit: int = 1000,
    ) -> list[list[float]]:
        self.fetch_since_calls.append((symbol, timeframe, since_ms, limit))
        return []


def _c6_fx_settings() -> TraderSettings:
    return TraderSettings(
        trader_symbols="EUR/USD",
        trader_timeframes="1h",
    )  # type: ignore[call-arg]


def _c6_crypto_settings() -> TraderSettings:
    return TraderSettings(
        trader_symbols="BTC/USDT",
        trader_timeframes="4h",
    )  # type: ignore[call-arg]


# Reference Saturday/Sunday/Monday in 2026 (no DST drama).
_C6_SAT = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
_C6_SUN = datetime(2026, 1, 11, 12, 0, tzinfo=UTC)
_C6_MON = datetime(2026, 1, 12, 12, 0, tzinfo=UTC)


@pytest.mark.integration
def test_c6_crypto_cycle_proceeds_on_saturday(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    """THE C.6 load-bearing regression: crypto_spot cycle on Saturday
    must proceed normally — fetch fires, no skip counter, no behaviour
    change vs pre-C.6.
    """
    fake = _RecordingFakeAdapter()
    fr = FakeRedis(decode_responses=False)
    result = ingest_one_cycle(
        database_url, _c6_crypto_settings(),
        adapter=cast(ExchangeAdapter, fake),
        redis=fr, now=_C6_SAT,
    )
    assert len(fake.fetch_recent_calls) == 1
    assert fake.fetch_recent_calls[0][0] == "BTC/USDT"
    assert result.pairs_skipped_weekend == 0
    assert result.pairs_attempted == 1


@pytest.mark.integration
def test_c6_crypto_cycle_proceeds_on_sunday(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    fake = _RecordingFakeAdapter()
    fr = FakeRedis(decode_responses=False)
    result = ingest_one_cycle(
        database_url, _c6_crypto_settings(),
        adapter=cast(ExchangeAdapter, fake),
        redis=fr, now=_C6_SUN,
    )
    assert len(fake.fetch_recent_calls) == 1
    assert result.pairs_skipped_weekend == 0


@pytest.mark.integration
def test_c6_fx_cycle_skipped_on_saturday(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    """fx_spot Saturday: pre-empted, no adapter fetch, counter bumps,
    no alerts."""
    fake = _RecordingFakeAdapter()
    fr = FakeRedis(decode_responses=False)
    result = ingest_one_cycle(
        database_url, _c6_fx_settings(),
        adapter=cast(ExchangeAdapter, fake),
        redis=fr, now=_C6_SAT,
    )
    assert len(fake.fetch_recent_calls) == 0, (
        "fx_spot Saturday cycle should NOT call the adapter; "
        f"got {len(fake.fetch_recent_calls)} calls"
    )
    assert result.pairs_skipped_weekend == 1
    assert result.pairs_attempted == 1
    assert result.pairs_failed == 0
    assert result.pairs_succeeded == 0
    assert result.data_feed_failure_alerts == 0


@pytest.mark.integration
def test_c6_fx_cycle_skipped_on_sunday(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    fake = _RecordingFakeAdapter()
    fr = FakeRedis(decode_responses=False)
    result = ingest_one_cycle(
        database_url, _c6_fx_settings(),
        adapter=cast(ExchangeAdapter, fake),
        redis=fr, now=_C6_SUN,
    )
    assert len(fake.fetch_recent_calls) == 0
    assert result.pairs_skipped_weekend == 1
    assert result.data_feed_failure_alerts == 0


@pytest.mark.integration
def test_c6_fx_cycle_runs_on_monday(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    """fx_spot Monday: weekend-skip does NOT fire; fetch runs normally."""
    fake = _RecordingFakeAdapter()
    fr = FakeRedis(decode_responses=False)
    result = ingest_one_cycle(
        database_url, _c6_fx_settings(),
        adapter=cast(ExchangeAdapter, fake),
        redis=fr, now=_C6_MON,
    )
    assert len(fake.fetch_recent_calls) == 1
    assert fake.fetch_recent_calls[0][0] == "EUR/USD"
    assert result.pairs_skipped_weekend == 0
    assert result.pairs_attempted == 1


@pytest.mark.integration
def test_c6_fx_four_consecutive_weekend_cycles_emit_zero_alerts(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    """THE C.6 alert-suppression invariant: 4 consecutive weekend
    cycles for an fx_spot symbol produce ZERO data_feed_failure
    alerts.

    Pre-C.6: 3 consecutive Saturday fetches would fail (empty Oanda
    response or 4xx) and trip _update_error_state's 3-strikes counter,
    emitting a critical `data_feed_failure` alert every weekend.
    The skip pre-empts that failure path entirely.
    """
    fake = _RecordingFakeAdapter()
    fr = FakeRedis(decode_responses=False)
    weekend_times = [
        datetime(2026, 1, 10, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 10, 18, 0, tzinfo=UTC),
        datetime(2026, 1, 11, 6, 0, tzinfo=UTC),
        datetime(2026, 1, 11, 18, 0, tzinfo=UTC),
    ]
    total_alerts = 0
    for now in weekend_times:
        result = ingest_one_cycle(
            database_url, _c6_fx_settings(),
            adapter=cast(ExchangeAdapter, fake),
            redis=fr, now=now,
        )
        total_alerts += result.data_feed_failure_alerts
    assert len(fake.fetch_recent_calls) == 0
    assert total_alerts == 0, (
        f"4 consecutive weekend FX cycles produced {total_alerts} alerts; "
        "expected 0 (skip should pre-empt the 3-strikes failure path)"
    )
