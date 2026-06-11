"""Trader v1 market-data ingestion loop.

`ingest_one_cycle(database_url, settings, ...)` is the public entry
point. Each cycle:

  1. For every (symbol, timeframe) in TRADER_SYMBOLS × TRADER_TIMEFRAMES:
      a. Fetch ~200 most recent candles from Binance via BinanceAdapter.
      b. Filter out the in-flight current bar (close_ts > now − 30s).
      c. Upsert into `trader_candles` with `ON CONFLICT DO NOTHING` on
         the (symbol, timeframe, close_ts) unique constraint.
  2. Re-scan the recent window for gaps in close_ts ordering. If a
     gap is detected, attempt a one-shot backfill via
     `fetch_ohlcv_since`. If the gap persists after backfill, write
     a `stale_data` risk event row.
  3. Track consecutive ccxt errors per (symbol, timeframe) in Redis.
     On the third consecutive error, write a `data_feed_failure`
     alert row (severity=critical, delivered=false — the alert
     dispatcher in Step 10 picks these up for Telegram delivery).
  4. Update the `trader_bot_runs` heartbeat row if a `run_id` is
     provided.

Idempotency: `ON CONFLICT DO NOTHING` means re-running covers the
same closed candles without duplicates. A process kill mid-cycle
replays cleanly on the next start.

Determinism: this loop reads live data, so its raw output depends
on what Binance returns. Its DB effects are deterministic given the
same input. The signal engine reads strictly-closed candles from
`trader_candles`, so any non-determinism from upstream is bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import psycopg
import structlog
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.trader.time import now_utc, timeframe_seconds
from psycopg.types.json import Jsonb
from redis import Redis

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.exchanges import (
    BinanceAdapter,
    ExchangeAdapter,
    IngestionError,
    infer_asset_class_from_symbol,
    make_adapter,
)
from marketmind_workers.trader.heartbeat import touch_heartbeat
from marketmind_workers.trader.session_skip import should_skip_weekend

log = structlog.get_logger(__name__)


_SAFETY_MARGIN_SECONDS: Final[int] = 30
_GAP_DETECTION_LOOKBACK: Final[int] = 50
_DATA_FEED_FAILURE_THRESHOLD: Final[int] = 3
# Consecutive-error counter Redis key TTL. 24h is long enough for
# operator intervention; short enough that a stale counter from a
# resolved incident won't keep alerting forever.
_REDIS_ERR_TTL_S: Final[int] = 86_400


class IngestionResult(_StrictModel):
    """Aggregate stats from one ingestion cycle.

    Returned by `ingest_one_cycle`; the RQ wrapper in Step 12 will
    log these and re-enqueue itself for the next boundary.

    Failure / recovery alert counters reflect the state-transition
    semantics in `_update_error_state` — `data_feed_failure_alerts`
    counts edges crossing INTO the failure state (threshold hit
    for the first time this streak); `data_feed_recovery_alerts`
    counts edges crossing OUT (first success after a failure
    streak that previously hit threshold).
    """

    pairs_attempted: int = 0
    pairs_succeeded: int = 0
    pairs_failed: int = 0
    pairs_skipped_weekend: int = 0  # C.6: per-pair weekend-skip count
    candles_inserted: int = 0
    gaps_detected: int = 0
    backfill_attempts: int = 0
    stale_data_events: int = 0
    data_feed_failure_alerts: int = 0
    data_feed_recovery_alerts: int = 0


@dataclass
class _CycleState:
    """Mutable accumulator during the cycle. Converted to a frozen
    `IngestionResult` at return.
    """

    pairs_attempted: int = 0
    pairs_succeeded: int = 0
    pairs_failed: int = 0
    pairs_skipped_weekend: int = 0  # C.6: per-pair weekend-skip count
    candles_inserted: int = 0
    gaps_detected: int = 0
    backfill_attempts: int = 0
    stale_data_events: int = 0
    data_feed_failure_alerts: int = 0
    data_feed_recovery_alerts: int = 0


# ---- Pure helpers (no DB / no network) -------------------------------------


def _filter_closed_candles(
    ohlcv: list[list[float]],
    timeframe: str,
    now: datetime,
    safety_margin_seconds: int = _SAFETY_MARGIN_SECONDS,
) -> list[list[float]]:
    """Drop the in-flight current bar.

    A candle is "closed" iff ``open_ts + bar_duration <= now − safety_margin``.
    Pure function over ccxt's raw OHLCV format.
    """
    bar_ms = timeframe_seconds(timeframe) * 1000
    cutoff_ms = int((now - timedelta(seconds=safety_margin_seconds)).timestamp() * 1000)
    return [row for row in ohlcv if int(row[0]) + bar_ms <= cutoff_ms]


def _detect_gaps_in_timestamps(
    timestamps: list[datetime],
    timeframe: str,
) -> int:
    """Count gap events in a sorted list of close_ts values.

    A "gap" is any consecutive pair whose delta != bar_duration
    (with 1s tolerance for clock drift). Pure function — DB I/O is
    in `_recent_close_ts`. Returns 0 if the list has <2 timestamps.
    """
    bar_seconds = timeframe_seconds(timeframe)
    if len(timestamps) < 2:
        return 0
    sorted_ts = sorted(timestamps)
    gap_count = 0
    for i in range(1, len(sorted_ts)):
        delta = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
        if abs(delta - bar_seconds) > 1:
            gap_count += 1
    return gap_count


# ---- DB-touching helpers ---------------------------------------------------


def _upsert_candles(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    ohlcv: list[list[float]],
) -> int:
    """Upsert closed candles. Returns the count of NEW rows inserted.

    psycopg returns rowcount=1 for newly inserted rows and rowcount=0
    when ON CONFLICT DO NOTHING skips a duplicate. Summing the values
    yields the count actually persisted by THIS call.

    Floats from ccxt are converted via ``Decimal(str(...))`` to dodge
    the binary-float contamination trap (`to_decimal` semantics).
    """
    if not ohlcv:
        return 0
    bar_seconds = timeframe_seconds(timeframe)
    inserted = 0
    with conn.cursor() as cur:
        for row in ohlcv:
            open_ms = int(row[0])
            open_ts = datetime.fromtimestamp(open_ms / 1000, tz=UTC)
            close_ts = open_ts + timedelta(seconds=bar_seconds)
            cur.execute(
                """
                INSERT INTO trader_candles
                    (symbol, timeframe, open_ts, close_ts,
                     open, high, low, close, volume, is_closed, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, timeframe, close_ts) DO NOTHING
                """,
                (
                    symbol,
                    timeframe,
                    open_ts,
                    close_ts,
                    Decimal(str(row[1])),
                    Decimal(str(row[2])),
                    Decimal(str(row[3])),
                    Decimal(str(row[4])),
                    Decimal(str(row[5])),
                    True,
                    "ccxt",
                ),
            )
            inserted += cur.rowcount or 0
    return inserted


def _recent_close_ts(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    limit: int = _GAP_DETECTION_LOOKBACK,
) -> list[datetime]:
    """Return the most recent N close_ts values, ascending."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close_ts FROM trader_candles
            WHERE symbol = %s AND timeframe = %s
            ORDER BY close_ts DESC
            LIMIT %s
            """,
            (symbol, timeframe, limit),
        )
        rows = cur.fetchall()
    return sorted(row[0] for row in rows)


def _emit_stale_data_event(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    details: dict[str, Any],
) -> None:
    """Write a `stale_data` risk-event row. severity=warning because
    a single gap is operationally interesting but not immediately
    actionable; the alerts dispatcher (Step 10) decides whether to
    Telegram on it.
    """
    full_details = {"timeframe": timeframe, **details}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_risk_events
                (event_type, severity, symbol, details)
            VALUES (%s, %s, %s, %s)
            """,
            ("stale_data", "warning", symbol, Jsonb(full_details)),
        )


def _emit_data_feed_failure_alert(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    consecutive_failures: int,
) -> None:
    """Write a critical `data_feed_failure` alert row. delivered=false;
    Step 10's alerts dispatcher delivers it to Telegram.

    Fires ONLY on the streak's threshold transition (the Nth failure
    where N=_DATA_FEED_FAILURE_THRESHOLD). Subsequent failures during
    the same streak are suppressed by the state machine in
    `_update_error_state` to avoid alert fatigue.
    """
    subject = f"Data feed failed {consecutive_failures}x in a row: {symbol} {timeframe}"
    body = (
        f"{consecutive_failures} consecutive ccxt fetch errors for "
        f"{symbol} {timeframe}. Ingestion loop is unable to update "
        f"trader_candles for this pair. Check Binance status and "
        f"network connectivity. This alert fires once per streak; "
        f"a recovery alert will follow when the feed resumes."
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_alerts
                (channel, severity, subject, body, delivered)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("telegram", "critical", subject, body, False),
        )


def _emit_data_feed_recovery_alert(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    streak_length: int,
) -> None:
    """Write a warning `data_feed_recovery` alert row.

    Fires on the FIRST successful fetch after a streak that
    previously tripped the failure threshold. Recovery from a
    sub-threshold streak (1-2 failures) does NOT alert — those
    transient blips never warranted operator attention to begin
    with.
    """
    subject = f"Data feed recovered: {symbol} {timeframe}"
    body = (
        f"ccxt fetch_ohlcv succeeded for {symbol} {timeframe} after "
        f"{streak_length} consecutive failures. trader_candles is "
        f"being updated again."
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_alerts
                (channel, severity, subject, body, delivered)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("telegram", "warning", subject, body, False),
        )


@dataclass(frozen=True)
class _ErrorStateUpdate:
    """Result of one transition through the error-state machine."""

    new_count: int
    # 'fire_failure'   -> emit `data_feed_failure` alert (threshold hit, first time this streak).
    # 'fire_recovery'  -> emit `data_feed_recovery` alert (streak ended after >= threshold).
    # 'none'           -> no alert.
    action: str = "none"
    # Length of the streak that just ended (recovery only); 0 otherwise.
    streak_length: int = 0


def _update_error_state(
    redis: Redis | None,
    symbol: str,
    timeframe: str,
    *,
    succeeded: bool,
) -> _ErrorStateUpdate:
    """Update the per-pair error-state machine; return what alert
    (if any) the caller should fire.

    State machine (PagerDuty-style state-transition alerting):
      - Failure N (N == THRESHOLD, first time this streak):
          → set `alerted` flag, return action='fire_failure'.
      - Failure N (N > THRESHOLD):
          → suppress (already alerted this streak), action='none'.
      - Failure N (N < THRESHOLD):
          → just increment, action='none'.
      - Success after a streak that hit THRESHOLD (alerted flag set):
          → clear flags, return action='fire_recovery'.
      - Success after a sub-threshold streak (alerted flag NOT set):
          → clear flags, action='none' (transient blip).

    Two Redis keys per pair:
      - `trader:ingestion:err_count:{symbol}:{timeframe}` — int counter
      - `trader:ingestion:err_alerted:{symbol}:{timeframe}` — set to b"1"
        when the failure alert has fired for the current streak.
    Both expire after `_REDIS_ERR_TTL_S` (24h). Success clears both.

    When `redis` is None, the state machine is a no-op and always
    returns action='none' — caller's alert logic never fires.
    Right behavior for environments without cross-cycle state
    (e.g., one-shot test runs).
    """
    if redis is None:
        return _ErrorStateUpdate(new_count=0, action="none", streak_length=0)

    count_key = f"trader:ingestion:err_count:{symbol}:{timeframe}".encode()
    alerted_key = f"trader:ingestion:err_alerted:{symbol}:{timeframe}".encode()

    if succeeded:
        # Read the streak length and "already alerted" flag BEFORE
        # clearing, so we can decide whether to fire recovery.
        prior_raw = redis.get(count_key)
        prior_count = int(prior_raw) if prior_raw is not None else 0  # type: ignore[arg-type]
        was_alerted = redis.get(alerted_key) is not None

        redis.delete(count_key)
        redis.delete(alerted_key)

        if was_alerted:
            return _ErrorStateUpdate(
                new_count=0,
                action="fire_recovery",
                streak_length=prior_count,
            )
        return _ErrorStateUpdate(new_count=0, action="none", streak_length=prior_count)

    # Failure path.
    new_count = int(redis.incr(count_key))  # type: ignore[arg-type]
    redis.expire(count_key, _REDIS_ERR_TTL_S)

    # First failure of THIS streak to hit the threshold — the
    # alerted-key guard handles the (rare, defensive) case of a
    # prior incomplete success leaving the flag set.
    if new_count == _DATA_FEED_FAILURE_THRESHOLD and redis.get(alerted_key) is None:
        redis.set(alerted_key, b"1", ex=_REDIS_ERR_TTL_S)
        return _ErrorStateUpdate(
            new_count=new_count,
            action="fire_failure",
            streak_length=new_count,
        )

    # new_count > THRESHOLD: already alerted, suppress.
    # new_count < THRESHOLD: streak still building, no alert.
    return _ErrorStateUpdate(new_count=new_count, action="none", streak_length=new_count)


# ---- Orchestrator ----------------------------------------------------------


def ingest_one_cycle(
    database_url: str,
    settings: TraderSettings,
    *,
    adapter: ExchangeAdapter | None = None,
    redis: Redis | None = None,
    run_id: UUID | None = None,
    now: datetime | None = None,
) -> IngestionResult:
    """Run one full ingestion cycle.

    Parameters
    ----------
    database_url
        psycopg connection string.
    settings
        TraderSettings. The (symbol, tf) pairs to ingest are the
        Cartesian product of `TRADER_SYMBOLS` × `TRADER_TIMEFRAMES`.
    adapter
        Optional `ExchangeAdapter` (typically a `BinanceAdapter`).
        Defaults to a fresh `BinanceAdapter`; tests inject a Protocol-
        compatible fake here directly — no `cast` needed.
    redis
        Optional Redis client for cross-cycle error-state tracking.
        When None, `data_feed_failure` / `data_feed_recovery` alerts
        never fire — caller has no cross-cycle state.
    run_id
        Optional UUID of an existing trader_bot_runs row whose
        `last_heartbeat_at` we should touch.
    now
        Optional clock override for tests; defaults to `now_utc()`.

    Returns
    -------
    IngestionResult
        Aggregate stats for the cycle.

    Notes
    -----
    A fetch error for one (symbol, timeframe) pair does NOT abort
    the loop — the cycle continues with the remaining pairs. Each
    pair's writes commit independently in their own transaction so
    a downstream failure can't roll back already-persisted candles.
    """
    state = _CycleState()
    actual_now = now if now is not None else now_utc()

    symbols = settings.symbols_list()
    timeframes = settings.timeframes_list()
    pairs = [(s, t) for s in symbols for t in timeframes]

    # Phase C C.1.4: dispatch the adapter on the asset_class inferred
    # from the FIRST symbol. The Phase C invariant is one asset class
    # per cycle (TRADER_SYMBOLS should be homogeneous); mixed-class
    # deployments arrive in C.5/C.6/C.7 with a per-pair adapter map.
    # `BinanceAdapter` is kept in the import block so the existing
    # e2e test monkeypatch path continues to work — `make_adapter`
    # references the class via its exchanges-module import, NOT
    # ingestion-module's, so the e2e test fixture's monkeypatch
    # target moved to `exchanges_module.BinanceAdapter` in C.1.4.
    #
    # Phase C C.1.6: factory-constructed adapters may hold long-lived
    # connection pools (httpx in OandaAdapter); close at cycle end via
    # the try/finally below so SSL sockets don't leak between cycles.
    # Injected adapters are caller-owned — we don't close them.
    adapter_owned_by_us = False
    if adapter is not None:
        actual_adapter: ExchangeAdapter = adapter
    elif symbols:
        actual_adapter = make_adapter(infer_asset_class_from_symbol(symbols[0]))
        adapter_owned_by_us = True
    else:
        # No symbols configured at all — preserve the legacy crypto
        # default so an empty TRADER_SYMBOLS doesn't crash startup.
        actual_adapter = BinanceAdapter()
        adapter_owned_by_us = True

    log.info(
        "ingest_cycle_starting",
        pairs=len(pairs),
        symbols=symbols,
        timeframes=timeframes,
    )

    with psycopg.connect(database_url) as conn:
        # Heartbeat first, outside the per-pair loop, so even a
        # cycle that fails on every pair still records "the loop is
        # alive".
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="ingest")

        for symbol, timeframe in pairs:
            state.pairs_attempted += 1
            # Phase C C.6: skip per-pair weekend fetches for non-24/7 venues.
            # Without this, an FX/metals/equity symbol's three consecutive
            # Saturday/Sunday cycles trip _update_error_state's
            # data_feed_failure alert (severity=critical) every weekend.
            # Crypto symbols (asset_class=crypto_spot) skip the check
            # entirely and proceed to the existing fetch path — bit-
            # identical to pre-C.6 for the 3 production strategies.
            symbol_class = infer_asset_class_from_symbol(symbol)
            if should_skip_weekend(symbol_class, actual_now):
                state.pairs_skipped_weekend += 1
                log.info(
                    "ingest_pair_skipped_weekend",
                    symbol=symbol,
                    timeframe=timeframe,
                    asset_class=symbol_class,
                    weekday_name=actual_now.strftime("%A"),
                    ts_utc=actual_now.isoformat(),
                )
                continue
            try:
                raw = actual_adapter.fetch_recent_ohlcv(symbol, timeframe, limit=200)
            except IngestionError as exc:
                state.pairs_failed += 1
                log.error(
                    "ingest_fetch_failed",
                    symbol=symbol,
                    timeframe=timeframe,
                    error=str(exc),
                )
                update = _update_error_state(
                    redis,
                    symbol,
                    timeframe,
                    succeeded=False,
                )
                if update.action == "fire_failure":
                    with conn.transaction():
                        _emit_data_feed_failure_alert(
                            conn,
                            symbol,
                            timeframe,
                            update.new_count,
                        )
                    state.data_feed_failure_alerts += 1
                continue

            closed = _filter_closed_candles(raw, timeframe, actual_now)
            with conn.transaction():
                inserted = _upsert_candles(conn, symbol, timeframe, closed)
            state.candles_inserted += inserted

            recovery_update = _update_error_state(
                redis,
                symbol,
                timeframe,
                succeeded=True,
            )
            if recovery_update.action == "fire_recovery":
                with conn.transaction():
                    _emit_data_feed_recovery_alert(
                        conn,
                        symbol,
                        timeframe,
                        recovery_update.streak_length,
                    )
                state.data_feed_recovery_alerts += 1

            # Gap detection over the recent window.
            timestamps = _recent_close_ts(conn, symbol, timeframe)
            gaps = _detect_gaps_in_timestamps(timestamps, timeframe)
            if gaps > 0:
                state.gaps_detected += gaps
                state.backfill_attempts += 1
                # One-shot backfill: re-fetch a wider range starting
                # from the earliest known timestamp. This usually
                # covers any short-range gap; for deeper gaps the
                # stale_data event below is the operator's cue.
                try:
                    since_ms = int(timestamps[0].timestamp() * 1000)
                    backfill_raw = actual_adapter.fetch_ohlcv_since(
                        symbol,
                        timeframe,
                        since_ms,
                        limit=1000,
                    )
                    backfill_closed = _filter_closed_candles(
                        backfill_raw,
                        timeframe,
                        actual_now,
                    )
                    with conn.transaction():
                        backfill_inserted = _upsert_candles(
                            conn,
                            symbol,
                            timeframe,
                            backfill_closed,
                        )
                    state.candles_inserted += backfill_inserted
                except IngestionError as exc:
                    log.warning(
                        "ingest_backfill_failed",
                        symbol=symbol,
                        timeframe=timeframe,
                        error=str(exc),
                    )

                # Re-detect gaps after the backfill attempt.
                timestamps_after = _recent_close_ts(conn, symbol, timeframe)
                remaining_gaps = _detect_gaps_in_timestamps(
                    timestamps_after,
                    timeframe,
                )
                if remaining_gaps > 0:
                    with conn.transaction():
                        _emit_stale_data_event(
                            conn,
                            symbol,
                            timeframe,
                            {
                                "remaining_gaps": remaining_gaps,
                                "lookback": _GAP_DETECTION_LOOKBACK,
                            },
                        )
                    state.stale_data_events += 1

            state.pairs_succeeded += 1

    result = IngestionResult(
        pairs_attempted=state.pairs_attempted,
        pairs_succeeded=state.pairs_succeeded,
        pairs_failed=state.pairs_failed,
        pairs_skipped_weekend=state.pairs_skipped_weekend,
        candles_inserted=state.candles_inserted,
        gaps_detected=state.gaps_detected,
        backfill_attempts=state.backfill_attempts,
        stale_data_events=state.stale_data_events,
        data_feed_failure_alerts=state.data_feed_failure_alerts,
        data_feed_recovery_alerts=state.data_feed_recovery_alerts,
    )
    log.info("ingest_cycle_complete", **result.model_dump())
    # C.1.6: release the adapter's connection pool if we constructed it
    # ourselves. Injected adapters are caller-owned and stay open.
    if adapter_owned_by_us and hasattr(actual_adapter, "close"):
        actual_adapter.close()  # type: ignore[attr-defined]
    return result


__all__ = ["IngestionResult", "ingest_one_cycle"]
