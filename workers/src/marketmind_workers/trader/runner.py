"""Trader v1 runner entrypoint.

Invoked as `python -m marketmind_workers.trader.runner` from the
`trader_worker` docker-compose service (and in production, the
Railway worker). This is the process that, in steady state,
drives the entire trader.

Lifecycle:

  1. assert_paper_only()  — first call, before any imports that
     could touch network / DB. Bails on any value other than
     "false" for TRADER_ALLOW_LIVE.
  2. configure_logging()  — structlog → stdout.
  3. apply_migrations()   — idempotent; pulls in any new SQL
     under infra/db/migrations/ that hasn't been recorded in
     _schema_migrations.
  4. verify_paper_only_first_line() — AST self-check of jobs.py.
     If a future commit drops the assert from any tick callable,
     boot fails immediately rather than silently.
  5. Create a fresh `trader_bot_runs` row (status='running').
  6. Mark any pre-existing 'running' rows as 'crashed' (orphan
     cleanup — only one runner at a time in v1).
  7. Bootstrap scheduled jobs: enqueue any of the five tick kinds
     that aren't already in the ScheduledJobRegistry.
  8. Worker.work(with_scheduler=True) — blocks until SIGTERM.
     RQ 2's in-process scheduler handles the timed re-enqueues.
  9. On graceful return: mark the bot_run row 'stopped'.

SIGTERM handling: RQ 2's Worker installs its own SIGTERM handler
that allows the current job to complete + then returns from
.work(). We rely on that — no custom signal.signal() call. After
.work() returns, the runner's main() proceeds to mark_stopped()
and exits zero.

Advisory locks: `try_advisory_xact_lock` (per-pair gating in
signal_engine.py) uses pg_try_advisory_xact_lock — automatically
released at transaction commit/rollback. SIGTERM mid-cycle: the
current phase's transaction commits, locks release, the next
phase doesn't start, .work() returns. No orphaned locks.
"""

from __future__ import annotations

import socket
import sys
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import psycopg
import structlog
from marketmind_shared.schemas.trader import LoopName
from redis import Redis
from rq import Queue, Worker

from marketmind_workers.config import get_settings as get_worker_settings
from marketmind_workers.db import apply_migrations
from marketmind_workers.logging import configure_logging
from marketmind_workers.trader import heartbeat as heartbeat_module
from marketmind_workers.trader import jobs as jobs_module
from marketmind_workers.trader.config import (
    assert_paper_only,
    get_trader_settings,
)

# Boot-time signature for `trader_bot_runs.worker_id`. Format
# `<hostname>:<pid>` — matches Phase 0's RQ worker naming
# convention so log queries match.
_WORKER_ID_FORMAT: Final[str] = "{host}:{pid}"


# ---- Bootstrap helpers -----------------------------------------------------


def _bootstrap_scheduled_jobs(queue: Queue, log: structlog.stdlib.BoundLogger) -> dict[str, str]:
    """Enqueue any tick kind that has no entry in the ScheduledJobRegistry.

    Idempotent: if a previous runner's chain is still alive (its
    scheduled jobs survive in Redis across the restart), this
    function will see those entries and skip re-bootstrapping.
    If the chain was broken (clean Redis, missing entries), it
    re-seeds.

    Returns a dict of {kind: job_id} for the entries it
    scheduled. The dict is empty when nothing needed scheduling.
    """
    now = datetime.now(UTC)
    scheduled: dict[str, str] = {}

    if not jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_MAIN_CYCLE):
        when = jobs_module.next_minute_boundary(now)
        job_id = jobs_module.boundary_job_id(jobs_module.JOB_ID_MAIN_CYCLE, when)
        queue.enqueue_at(when, jobs_module.tick_main_cycle, job_id=job_id)
        scheduled["main_cycle"] = job_id

    if not jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_DRIFT):
        when = jobs_module.next_daily_at(now, hour=1)
        job_id = jobs_module.boundary_job_id(jobs_module.JOB_ID_DRIFT, when)
        queue.enqueue_at(when, jobs_module.tick_drift, job_id=job_id)
        scheduled["drift"] = job_id

    if not jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_SUMMARY_DAILY):
        when = jobs_module.next_daily_at(now, hour=0, minute=5)
        job_id = jobs_module.boundary_job_id(jobs_module.JOB_ID_SUMMARY_DAILY, when)
        queue.enqueue_at(when, jobs_module.tick_summary_daily, job_id=job_id)
        scheduled["summary_daily"] = job_id

    if not jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_SUMMARY_WEEKLY):
        when = jobs_module.next_monday_at(now, hour=0, minute=10)
        job_id = jobs_module.boundary_job_id(jobs_module.JOB_ID_SUMMARY_WEEKLY, when)
        queue.enqueue_at(when, jobs_module.tick_summary_weekly, job_id=job_id)
        scheduled["summary_weekly"] = job_id

    if not jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_STALE_DETECTOR):
        when = jobs_module.next_n_minute_boundary(now, n=5)
        job_id = jobs_module.boundary_job_id(jobs_module.JOB_ID_STALE_DETECTOR, when)
        queue.enqueue_at(when, jobs_module.tick_stale_heartbeat_detector, job_id=job_id)
        scheduled["stale_detector"] = job_id

    log.info("trader_bootstrap_complete", scheduled=scheduled)
    return scheduled


def _worker_id() -> str:
    import os

    return _WORKER_ID_FORMAT.format(host=socket.gethostname(), pid=os.getpid())


def _create_run_row(database_url: str, worker_id: str) -> UUID:
    """Insert a fresh `trader_bot_runs` row + return its id."""
    with psycopg.connect(database_url) as conn, conn.transaction():
        return heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id=worker_id,
        )


def _mark_run_stopped(database_url: str, run_id: UUID) -> None:
    with psycopg.connect(database_url) as conn, conn.transaction():
        heartbeat_module.mark_stopped(conn, run_id)


# ---- Main ------------------------------------------------------------------


def main() -> int:
    """Runner entry point. Returns a process exit code.

    PLR0911 (too many return statements) is disabled — early
    returns map cleanly to distinct boot-failure modes that all
    deserve their own exit code conceptually (we use 1 here for
    uniformity, but the structure makes future fan-out easy).
    """
    # Step 1: assert_paper_only FIRST, before anything else that
    # could touch the network or DB.
    assert_paper_only()

    worker_settings = get_worker_settings()
    configure_logging(
        level=worker_settings.log_level,
        environment=worker_settings.environment,
    )
    log = structlog.get_logger(__name__)
    log.info("trader_runner_starting", environment=worker_settings.environment)

    trader_settings = get_trader_settings()
    database_url = str(trader_settings.database_url)

    # Phase C C.1.5: enforce the homogeneous-asset-class invariant on
    # TRADER_SYMBOLS at boot. The C.1.4 ingestion dispatch is single-
    # adapter-per-cycle; a mixed-class deployment would silently use
    # the wrong adapter for some symbols. Multi-class loops land in
    # C.5/C.6/C.7.
    try:
        trader_settings.assert_symbols_homogeneous_asset_class()
    except ValueError:
        log.exception("trader_symbols_homogeneous_class_check_failed")
        return 1

    # Step 3: apply migrations (idempotent).
    try:
        applied = apply_migrations(database_url)
        if applied:
            log.info("trader_migrations_applied", count=len(applied), files=applied)
        else:
            log.info("trader_migrations_up_to_date")
    except Exception:
        log.exception("trader_migrations_failed")
        return 1

    # Step 4: AST self-check of jobs.py. Fails fast if a future
    # commit drops the assert from any tick callable.
    try:
        jobs_module.verify_paper_only_first_line()
    except AssertionError:
        log.exception("trader_paper_only_self_check_failed")
        return 1

    # Step 5: create the trader_bot_runs row.
    worker_id = _worker_id()
    try:
        run_id = _create_run_row(database_url, worker_id)
    except Exception:
        log.exception("trader_create_bot_run_failed")
        return 1
    log.info("trader_bot_run_created", run_id=str(run_id), worker_id=worker_id)

    # Step 6: orphan cleanup — any pre-existing 'running' rows
    # from a runner that died without SIGTERM get marked
    # 'crashed' so they don't fool the stale-heartbeat detector
    # or _active_run_id() into picking the wrong run.
    orphaned = jobs_module.mark_orphaned_runs_crashed(database_url, run_id)
    if orphaned:
        log.warning("trader_orphaned_runs_marked_crashed", count=orphaned)

    # Step 7: bootstrap scheduled jobs.
    redis = Redis.from_url(str(trader_settings.redis_url), decode_responses=False)
    queue = Queue(name=trader_settings.trader_queue_name, connection=redis)
    try:
        _bootstrap_scheduled_jobs(queue, log)
    except Exception:
        log.exception("trader_bootstrap_failed")
        return 1

    # Step 8: run the worker (blocks until SIGTERM).
    worker = Worker(
        queues=[queue],
        connection=redis,
        name=worker_id,
    )
    log.info("trader_worker_starting", queue=trader_settings.trader_queue_name)
    try:
        worker.work(with_scheduler=True, logging_level=worker_settings.log_level)
    except Exception:
        log.exception("trader_worker_died")
        # Don't mark stopped on crash — let the stale-detector
        # mark it 'crashed' after the threshold elapses so the
        # operator sees the right state in /trader/risk/status.
        return 1

    # Step 9: graceful shutdown path.
    log.info("trader_worker_returned", run_id=str(run_id))
    try:
        _mark_run_stopped(database_url, run_id)
    except Exception:
        log.exception("trader_mark_stopped_failed", run_id=str(run_id))
        return 1
    log.info("trader_run_stopped", run_id=str(run_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
