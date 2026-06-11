"""All RQ job callables for the trader v1 runner.

Step 12 of the trader rollout. Every callable here is enqueued
onto the `trader_default` queue by the runner. Each callable is
structured so that:

  1. `assert_paper_only()` is the LITERAL FIRST STATEMENT after
     the docstring. Verified by `test_jobs_paper_only.py`.
  2. The "active" `trader_bot_runs` row is discovered at
     execution time, NOT baked into the job's arguments. Stale
     scheduled jobs left behind by a crashed runner pick up the
     new run as soon as the next runner bootstraps.
  3. Each callable re-enqueues itself for the next boundary
     BEFORE returning — except when the active run can't be
     found, in which case the chain pauses and the next
     runner-boot's bootstrap re-seeds it.

The 6 phases of the main cycle run in this order:

    ingest → signal → risk → execute → snapshot → alerts

Each phase commits its own work before the next reads. The phases
are wrapped in independent try/except blocks so one failing
phase doesn't kill the cycle (and break the re-enqueue chain).

Daily and weekly summary alerts run on separate schedules — they
are NOT part of the 6-phase cycle, because the cycle runs every
minute and we don't want a summary alert per minute.

Drift analysis also runs on its own daily schedule (at 01:00 UTC)
rather than per-cycle: drift compares paper performance against
walk-forward backtest metrics — the comparison is meaningful at
daily cadence, noise at per-minute cadence.

Stale-heartbeat detection runs every 5 min on its own schedule.
It marks any 'running' run whose last_heartbeat_at is older than
the threshold as 'crashed' and emits a critical alert. This is a
self-watch: if the entire runner process dies, no detector fires
(Railway/k8s restart is the outer safety net). The detector
catches the case where the process is alive but a single phase
is stuck.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import structlog
from marketmind_shared.schemas.trader import AlertChannel, Severity
from marketmind_shared.trader.time import utc_midnight_of, utc_monday_of
from redis import Redis
from rq import Queue
from rq.registry import ScheduledJobRegistry

from marketmind_workers.observability.daily_summary import generate_and_write
from marketmind_workers.trader import alerts as alerts_module
from marketmind_workers.trader import drift as drift_module
from marketmind_workers.trader import execution as execution_module
from marketmind_workers.trader import heartbeat as heartbeat_module
from marketmind_workers.trader import ingestion as ingestion_module
from marketmind_workers.trader import portfolio as portfolio_module
from marketmind_workers.trader import risk as risk_module
from marketmind_workers.trader import signal_engine as signal_engine_module
from marketmind_workers.trader.config import (
    TraderSettings,
    assert_paper_only,
    get_trader_settings,
)

log = structlog.get_logger(__name__)


# Job-ID prefixes used by the runner's idempotent bootstrap to
# detect whether a scheduled job for each tick kind already
# exists. The trailing component is the next-boundary timestamp
# (isoformat), which makes the full job ID deterministic and
# replaces older entries cleanly via RQ's id-collision semantics.
# RQ 2 enforces a strict pattern for job IDs (letters, digits,
# underscores, dashes only — no colons or dots). We use snake_case
# prefixes + a `_<unix_seconds>` suffix to encode the scheduled
# boundary deterministically.
JOB_ID_MAIN_CYCLE: str = "trader_tick_main_cycle"
JOB_ID_DRIFT: str = "trader_tick_drift"
JOB_ID_SUMMARY_DAILY: str = "trader_tick_summary_daily"
JOB_ID_SUMMARY_WEEKLY: str = "trader_tick_summary_weekly"
JOB_ID_STALE_DETECTOR: str = "trader_tick_stale_heartbeat_detector"


def boundary_job_id(prefix: str, when: datetime) -> str:
    """Build a deterministic, RQ-safe job ID from a prefix + boundary.

    Format: `{prefix}_{unix_seconds}`. Two enqueue_at calls for
    the same prefix + same boundary produce the same job ID, so
    RQ deduplicates naturally.
    """
    return f"{prefix}_{int(when.timestamp())}"


# Stale-heartbeat threshold: a run whose last_heartbeat_at is
# older than this gets marked 'crashed'. 5 minutes covers normal
# pause periods (the main cycle runs every 60s) with margin for
# slow DB writes; tighter than this would false-positive on
# transient DB hiccups.
_STALE_HEARTBEAT_THRESHOLD_S: int = 300

# Schedules. All times are UTC.
_DRIFT_HOUR_UTC: int = 1
_SUMMARY_DAILY_HOUR_UTC: int = 0
_SUMMARY_DAILY_MINUTE_UTC: int = 5
_SUMMARY_WEEKLY_HOUR_UTC: int = 0
_SUMMARY_WEEKLY_MINUTE_UTC: int = 10
_STALE_DETECTOR_INTERVAL_MINUTES: int = 5


# ---- Boundary helpers (pure) ----------------------------------------------


def next_minute_boundary(now: datetime) -> datetime:
    """Strict-after :00 of the next minute. Used by the main cycle."""
    return now.replace(second=0, microsecond=0) + timedelta(minutes=1)


def next_n_minute_boundary(now: datetime, n: int) -> datetime:
    """Next datetime on an n-minute grid in UTC (strict-after).

    Example for n=5 and now=14:23:45 UTC → 14:25:00 UTC.
    """
    floor_minute = (now.minute // n) * n
    floored = now.replace(minute=floor_minute, second=0, microsecond=0)
    return floored + timedelta(minutes=n)


def next_daily_at(now: datetime, *, hour: int, minute: int = 0) -> datetime:
    """Next UTC datetime at the given hour:minute. Strict-after now.

    Always uses UTC midnight as the day boundary (see
    `utc_midnight_of` in the shared time helpers).
    """
    candidate = utc_midnight_of(now) + timedelta(hours=hour, minutes=minute)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def next_monday_at(now: datetime, *, hour: int, minute: int = 0) -> datetime:
    """Next Monday at the given UTC hour:minute. Strict-after now.

    Uses `utc_monday_of` which floors to the most recent Monday
    00:00 UTC.
    """
    candidate = utc_monday_of(now) + timedelta(hours=hour, minutes=minute)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


# ---- Redis / queue accessors ----------------------------------------------


def _connect_redis(settings: TraderSettings) -> Redis:
    """One-shot redis connection. Each job opens its own — we
    don't share connections across cycles to keep failures local.
    """
    return Redis.from_url(str(settings.redis_url))


def _connect_queue(settings: TraderSettings, *, redis: Redis | None = None) -> Queue:
    return Queue(
        settings.trader_queue_name,
        connection=redis if redis is not None else _connect_redis(settings),
    )


def any_scheduled_with_prefix(queue: Queue, prefix: str) -> bool:
    """Return True if any job in the ScheduledJobRegistry's ID
    list starts with the given prefix.

    Used by the runner's bootstrap to detect whether a tick kind
    is already scheduled (so re-bootstrapping doesn't double-up).
    """
    registry = ScheduledJobRegistry(queue=queue)
    return any(job_id.startswith(prefix) for job_id in registry.get_job_ids())


# ---- Active-run discovery --------------------------------------------------


def _active_run_id(database_url: str) -> UUID | None:
    """Most recent `trader_bot_runs` row whose status='running'.

    The runner creates exactly one 'running' row at boot and
    marks any pre-existing ones 'crashed' (`mark_orphaned_runs_crashed`
    below). Within a healthy lifecycle there is at most one
    'running' row, so the ORDER BY ... LIMIT 1 is defensive
    rather than load-bearing.

    Why look it up dynamically rather than baking run_id into the
    job arg: a runner that crashed leaves scheduled jobs in the
    registry pointing at its (now stale) run_id. Looking up at
    execution time means the next runner's run_id is used
    automatically, without re-enqueueing every existing job.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM trader_bot_runs
            WHERE status = 'running'
            ORDER BY started_at DESC
            LIMIT 1
            """,
        )
        row = cur.fetchone()
    return UUID(str(row[0])) if row else None


def mark_orphaned_runs_crashed(database_url: str, current_run_id: UUID) -> int:
    """Mark every 'running' row EXCEPT current_run_id as 'crashed'.

    Called by the runner's bootstrap. Without this, a previous
    runner that died without SIGTERM would leave a stale 'running'
    row that the stale-detector would eventually clean up — but
    we don't want to wait, because jobs from the stale chain
    would happily heartbeat the stale row in the meantime.

    Returns the count of rows transitioned.
    """
    with psycopg.connect(database_url) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trader_bot_runs
            SET status = 'crashed', notes = 'orphaned by new runner boot'
            WHERE status = 'running' AND id <> %s
            """,
            (str(current_run_id),),
        )
        return cur.rowcount or 0


# ---- Phase-failure visibility ---------------------------------------------
#
# Step 13. Each phase has a Redis-backed consecutive-failure
# counter. State-transition alerts mirror Step 4's data-feed
# pattern: a streak that crosses INTO the threshold fires a
# critical alert; the first success after a streak >= threshold
# fires an info recovery alert; sub-threshold streaks stay silent.
#
# Storing per-phase rather than per-(phase, run_id) means the
# counter survives runner restarts — exactly what we want, since
# a phase failing because of a structural bug (bad SQL, missing
# index) will keep failing across restarts and should escalate.

_PHASE_FAILURE_THRESHOLD: int = 3
# 24h TTL on the counter so a single transient failure doesn't
# keep weighing down the counter forever if the phase isn't
# exercised again that day (e.g., drift, daily summary).
_PHASE_FAILURE_TTL_S: int = 86_400


def _phase_failure_key(phase: str) -> str:
    return f"trader:phase_failures:{phase}"


def _record_phase_outcome(
    redis: Redis,
    database_url: str,
    phase: str,
    *,
    success: bool,
) -> None:
    """Update the per-phase Redis counter; emit edge-transition alerts.

    Increment-on-failure, reset-on-success. Emits a critical
    alert when the counter crosses INTO `_PHASE_FAILURE_THRESHOLD`
    (i.e. equals the threshold for the first time this streak).
    Emits an info recovery alert when a success follows a streak
    that had already crossed the threshold. Sub-threshold streaks
    stay silent.

    The increment uses Redis INCR (atomic); the EXPIRE refresh is
    a separate command — the worst-case race is "TTL ran out
    between INCR and EXPIRE" which makes the counter live
    forever; the next INCR or DELETE corrects it.
    """
    key = _phase_failure_key(phase)
    if success:
        prev_raw = redis.get(key)
        prev_count = int(prev_raw) if prev_raw is not None else 0  # type: ignore[arg-type]
        if prev_count > 0:
            redis.delete(key)
        if prev_count >= _PHASE_FAILURE_THRESHOLD:
            _emit_alert(
                database_url,
                severity=Severity.INFO,
                channel=AlertChannel.TELEGRAM,
                subject=f"Trader phase recovered: {phase}",
                body=(f"Phase '{phase}' succeeded after {prev_count} consecutive failures."),
                context={"phase": phase, "previous_failures": prev_count},
            )
        return

    raw_count = redis.incr(key)
    new_count = int(raw_count)  # type: ignore[arg-type]
    redis.expire(key, _PHASE_FAILURE_TTL_S)
    if new_count == _PHASE_FAILURE_THRESHOLD:
        _emit_alert(
            database_url,
            severity=Severity.CRITICAL,
            channel=AlertChannel.TELEGRAM,
            subject=f"Trader phase failing: {phase}",
            body=(f"Phase '{phase}' failed {new_count} consecutive cycles."),
            context={"phase": phase, "consecutive_failures": new_count},
        )


# ---- Internal: phase runner -----------------------------------------------


def _run_phase(
    redis: Redis,
    database_url: str,
    phase: str,
    fn: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Run one phase. Logs+swallows exceptions; records the
    outcome in the per-phase Redis failure counter.

    The cycle's re-enqueue logic depends on tick_main_cycle
    completing — a phase that raises must not break the chain.
    Exceptions are logged via structlog. The counter handles
    repeated failures: a streak crossing the threshold fires a
    critical alert (state-transition semantics, see
    `_record_phase_outcome`). KeyboardInterrupt / SystemExit are
    NOT swallowed (they're BaseException, not Exception).
    """
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("trader_phase_failed", phase=phase)
        _record_phase_outcome(redis, database_url, phase, success=False)
    else:
        _record_phase_outcome(redis, database_url, phase, success=True)


# ---- Alert emission --------------------------------------------------------


def _emit_alert(
    database_url: str,
    *,
    severity: Severity,
    channel: AlertChannel,
    subject: str,
    body: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Insert a `trader_alerts` row with delivered=False.

    The alert dispatcher (Phase 6 of the main cycle) picks it up
    on the next cycle and routes per the channel + severity
    matrix. We don't dispatch inline because that would block the
    job on Telegram HTTP, defeating the cycle cadence.

    `context` is folded into `body` as a one-line "(...)"
    suffix because `trader_alerts` has no payload column in v1.
    Migration 0009 may add JSONB context later.
    """
    if context:
        body = f"{body} ({context})"
    with psycopg.connect(database_url) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_alerts (channel, severity, subject, body, delivered)
            VALUES (%s, %s, %s, %s, FALSE)
            """,
            (channel.value, severity.value, subject, body),
        )


# ---- Summary builders ------------------------------------------------------


def _build_daily_summary(database_url: str) -> str:
    """Render a one-paragraph summary of yesterday's paper trading.

    Pulls counts of trades closed, sum of realised PnL, and the
    most recent equity / drawdown_pct values. Returns the human-
    readable body of the alert.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE status = 'CLOSED'
                      AND exit_ts >= NOW() - INTERVAL '1 day'
                ) AS closed_today,
                COALESCE(SUM(realised_pnl) FILTER (
                    WHERE status = 'CLOSED'
                      AND exit_ts >= NOW() - INTERVAL '1 day'
                ), 0) AS pnl_today
            FROM trader_paper_positions
            """,
        )
        row = cur.fetchone()
        closed_today = int(row[0]) if row else 0
        pnl_today = Decimal(row[1] or 0) if row else Decimal("0")

        cur.execute(
            """
            SELECT equity, drawdown_pct, open_positions_count
            FROM trader_portfolio_snapshots
            ORDER BY ts DESC LIMIT 1
            """,
        )
        snap_row = cur.fetchone()
    if snap_row is None:
        return (
            f"Daily summary: {closed_today} positions closed, "
            f"realised PnL = {pnl_today}. No snapshot yet."
        )
    equity, drawdown_pct, open_count = snap_row
    return (
        f"Daily summary (last 24h): {closed_today} positions closed, "
        f"realised PnL = {pnl_today}. "
        f"Latest snapshot — equity={equity}, drawdown_pct={drawdown_pct}, "
        f"open_positions={open_count}."
    )


def _build_weekly_summary(database_url: str) -> str:
    """Render a one-paragraph summary of the last 7 days."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE status = 'CLOSED'
                      AND exit_ts >= NOW() - INTERVAL '7 days'
                ) AS closed_week,
                COALESCE(SUM(realised_pnl) FILTER (
                    WHERE status = 'CLOSED'
                      AND exit_ts >= NOW() - INTERVAL '7 days'
                ), 0) AS pnl_week
            FROM trader_paper_positions
            """,
        )
        row = cur.fetchone()
        closed = int(row[0]) if row else 0
        pnl = Decimal(row[1] or 0) if row else Decimal("0")
    return f"Weekly summary (last 7d): {closed} positions closed, realised PnL = {pnl}."


# ---- Tick callables --------------------------------------------------------
#
# Every callable below has `assert_paper_only()` as its literal
# first statement. The unit test `test_jobs_paper_only.py`
# AST-parses this module and verifies it. Do NOT add code above
# the assert.


def tick_main_cycle() -> None:
    """One full 6-phase cycle. Re-enqueues itself for the next minute boundary.

    Phases in order: ingest → signal → risk → execute →
    snapshot → alerts. Each phase commits its own work; a phase
    failure is logged + an alert emitted but does not break the
    chain (the re-enqueue still runs).

    Skips if no active run exists (the runner's bootstrap will
    seed a new run on next boot).
    """
    assert_paper_only()
    settings = get_trader_settings()
    db = str(settings.database_url)
    run_id = _active_run_id(db)
    if run_id is None:
        log.warning("trader_no_active_run_main_cycle_skipped")
        return

    log.info("trader_main_cycle_starting", run_id=str(run_id))
    # One redis connection per cycle — the phase-failure tracker
    # uses it for the consecutive-failure counters; the queue
    # connection at the end of the cycle reuses it.
    redis = _connect_redis(settings)
    _run_phase(
        redis,
        db,
        "ingest",
        ingestion_module.ingest_one_cycle,
        db,
        settings,
        run_id=run_id,
    )
    _run_phase(
        redis,
        db,
        "signal",
        signal_engine_module.evaluate_one_cycle,
        db,
        settings,
        run_id=run_id,
    )
    _run_phase(
        redis,
        db,
        "risk",
        risk_module.process_pending_signals,
        db,
        settings,
        run_id=run_id,
    )
    _run_phase(
        redis,
        db,
        "execute",
        execution_module.process_one_cycle,
        db,
        settings,
        run_id=run_id,
    )
    _run_phase(
        redis,
        db,
        "snapshot",
        portfolio_module.compute_and_persist_snapshot,
        db,
        settings,
        run_id=run_id,
    )
    _run_phase(
        redis,
        db,
        "alerts",
        alerts_module.dispatch_pending_alerts,
        db,
        settings,
        run_id=run_id,
    )
    log.info("trader_main_cycle_complete", run_id=str(run_id))

    # Re-enqueue for the next minute. Always — even if every phase
    # failed — so the chain self-heals.
    queue = _connect_queue(settings, redis=redis)
    next_run = next_minute_boundary(datetime.now(UTC))
    queue.enqueue_at(
        next_run,
        tick_main_cycle,
        job_id=boundary_job_id(JOB_ID_MAIN_CYCLE, next_run),
    )


def tick_drift() -> None:
    """Daily drift analysis. Re-enqueues for next 01:00 UTC.

    Runs separately from `tick_main_cycle` because drift
    comparisons need >= a day of paper trades to be meaningful;
    running per-minute would just emit noise.
    """
    assert_paper_only()
    settings = get_trader_settings()
    db = str(settings.database_url)
    run_id = _active_run_id(db)
    if run_id is None:
        log.warning("trader_no_active_run_drift_skipped")
        return

    log.info("trader_drift_tick_starting", run_id=str(run_id))
    redis = _connect_redis(settings)
    _run_phase(
        redis,
        db,
        "drift",
        drift_module.compute_and_persist_drift_for_all,
        db,
        settings,
        run_id=run_id,
    )

    queue = _connect_queue(settings, redis=redis)
    next_run = next_daily_at(datetime.now(UTC), hour=_DRIFT_HOUR_UTC)
    queue.enqueue_at(
        next_run,
        tick_drift,
        job_id=boundary_job_id(JOB_ID_DRIFT, next_run),
    )


def tick_summary_daily() -> None:
    """Emit the daily summary alert. Re-enqueues for next 00:05 UTC.

    The alert is INSERTed with delivered=False; the alert
    dispatcher on the next main cycle picks it up.
    """
    assert_paper_only()
    settings = get_trader_settings()
    db = str(settings.database_url)

    # Structured daily summary — JSON + rendered text to
    # /data/daily-summaries/. Best-effort: a failure here must not block
    # the activity-feed alert or the re-enqueue that keeps the tick alive.
    try:
        _, json_path, _ = generate_and_write(db, datetime.now(UTC))
        log.info("daily_summary_report_written", path=str(json_path))
    except Exception:
        log.exception("daily_summary_report_failed")

    # The one-line activity-feed alert is kept unchanged — the dashboard
    # feed reads trader_alerts. The structured report above augments it.
    body = _build_daily_summary(db)
    _emit_alert(
        db,
        severity=Severity.INFO,
        channel=AlertChannel.TELEGRAM,
        subject="Daily summary",
        body=body,
    )

    queue = _connect_queue(settings)
    next_run = next_daily_at(
        datetime.now(UTC),
        hour=_SUMMARY_DAILY_HOUR_UTC,
        minute=_SUMMARY_DAILY_MINUTE_UTC,
    )
    queue.enqueue_at(
        next_run,
        tick_summary_daily,
        job_id=boundary_job_id(JOB_ID_SUMMARY_DAILY, next_run),
    )


def tick_summary_weekly() -> None:
    """Emit the weekly summary alert. Re-enqueues for next Monday 00:10 UTC."""
    assert_paper_only()
    settings = get_trader_settings()
    db = str(settings.database_url)
    body = _build_weekly_summary(db)
    _emit_alert(
        db,
        severity=Severity.INFO,
        channel=AlertChannel.TELEGRAM,
        subject="Weekly summary",
        body=body,
    )

    queue = _connect_queue(settings)
    next_run = next_monday_at(
        datetime.now(UTC),
        hour=_SUMMARY_WEEKLY_HOUR_UTC,
        minute=_SUMMARY_WEEKLY_MINUTE_UTC,
    )
    queue.enqueue_at(
        next_run,
        tick_summary_weekly,
        job_id=boundary_job_id(JOB_ID_SUMMARY_WEEKLY, next_run),
    )


def tick_stale_heartbeat_detector() -> None:
    """Mark stale 'running' runs as 'crashed' + emit critical alerts.

    Self-watch: if the runner process itself dies, no one runs
    this. Railway/k8s health checks are the outer safety net.
    The detector catches the case where the process is alive but
    a phase is stuck (no heartbeat written), which is the more
    common silent-failure mode.

    Re-enqueues every 5 minutes.
    """
    assert_paper_only()
    settings = get_trader_settings()
    db = str(settings.database_url)

    with psycopg.connect(db) as conn, conn.transaction():
        stale = heartbeat_module.find_stale_runs(
            conn,
            threshold_seconds=_STALE_HEARTBEAT_THRESHOLD_S,
        )
        for run_id, loop_name, last_hb, notes in stale:
            heartbeat_module.mark_crashed(
                conn,
                run_id,
                reason=(
                    f"stale heartbeat at {last_hb.isoformat()}; loop={loop_name}; notes={notes!r}"
                ),
            )
            log.error(
                "trader_run_crashed_stale_heartbeat",
                run_id=str(run_id),
                loop_name=loop_name,
                last_heartbeat_at=last_hb.isoformat(),
            )

    # Alert emission outside the lock-holding transaction.
    for run_id, loop_name, last_hb, notes in stale:
        _emit_alert(
            db,
            severity=Severity.CRITICAL,
            channel=AlertChannel.TELEGRAM,
            subject="Trader run crashed (stale heartbeat)",
            body=(
                f"Run {run_id} (loop={loop_name}) had no heartbeat since "
                f"{last_hb.isoformat()} (>5 min). Marked status='crashed'. "
                f"Last notes: {notes!r}."
            ),
            context={
                "run_id": str(run_id),
                "loop_name": loop_name,
                "last_heartbeat_at": last_hb.isoformat(),
            },
        )

    queue = _connect_queue(settings)
    next_run = next_n_minute_boundary(
        datetime.now(UTC),
        n=_STALE_DETECTOR_INTERVAL_MINUTES,
    )
    queue.enqueue_at(
        next_run,
        tick_stale_heartbeat_detector,
        job_id=boundary_job_id(JOB_ID_STALE_DETECTOR, next_run),
    )


# ---- AST-level self-check helpers ------------------------------------------
#
# `verify_paper_only_first_line()` is exported so the unit test
# can run it; it also runs at runner-boot time as an extra
# defence (the unit test catches drift at PR time; the boot-time
# check catches a worker shipping a stale wheel).


_TICK_FUNC_PREFIX: str = "tick_"


def _is_docstring(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr):
        return False
    value = node.value
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def _is_assert_paper_only_call(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Name) and func.id == "assert_paper_only"


def verify_paper_only_first_line() -> None:
    """AST-check that every `tick_*` function in this module has
    `assert_paper_only()` as its literal first statement (after
    the optional docstring). Raises AssertionError on drift.

    Called by the runner at boot. Also covered by
    `test_jobs_paper_only.py` to fail at PR time rather than at
    runtime.
    """
    source = inspect.getsource(_current_module())
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith(_TICK_FUNC_PREFIX):
            continue
        body = node.body
        first_idx = 1 if body and _is_docstring(body[0]) else 0
        if first_idx >= len(body):
            offenders.append(f"{node.name}: empty body")
            continue
        if not _is_assert_paper_only_call(body[first_idx]):
            offenders.append(
                f"{node.name}: first stmt is not `assert_paper_only()`",
            )
    if offenders:
        raise AssertionError(
            "trader.jobs paper-only first-line check failed:\n  " + "\n  ".join(offenders),
        )


def _current_module() -> Any:
    """Return this module object (for `inspect.getsource`). Indirected
    to keep the AST check simple — looking up `__name__` directly
    would work too but the indirection makes mocking easier in
    tests that need a custom module.
    """
    import sys

    return sys.modules[__name__]


__all__ = [
    "JOB_ID_DRIFT",
    "JOB_ID_MAIN_CYCLE",
    "JOB_ID_STALE_DETECTOR",
    "JOB_ID_SUMMARY_DAILY",
    "JOB_ID_SUMMARY_WEEKLY",
    "any_scheduled_with_prefix",
    "boundary_job_id",
    "mark_orphaned_runs_crashed",
    "next_daily_at",
    "next_minute_boundary",
    "next_monday_at",
    "next_n_minute_boundary",
    "tick_drift",
    "tick_main_cycle",
    "tick_stale_heartbeat_detector",
    "tick_summary_daily",
    "tick_summary_weekly",
    "verify_paper_only_first_line",
]
