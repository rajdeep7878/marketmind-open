"""Bootstrap idempotence tests for the trader runner.

Verifies that `_bootstrap_scheduled_jobs` does NOT enqueue
duplicates when a previous runner's scheduled chain is still
present in Redis.

Uses fakeredis to keep the test hermetic — RQ's
ScheduledJobRegistry works against the fakeredis instance the
same way it works against a real Redis.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
import structlog
from fakeredis import FakeRedis
from marketmind_workers.trader import jobs as jobs_module
from marketmind_workers.trader.runner import _bootstrap_scheduled_jobs
from rq import Queue
from rq.registry import ScheduledJobRegistry


@pytest.fixture
def queue() -> Queue:
    redis = FakeRedis(decode_responses=False)
    return Queue(name="trader_default", connection=redis)


@pytest.fixture
def log() -> structlog.stdlib.BoundLogger:
    return structlog.wrap_logger(
        logging.getLogger("test"),
        wrapper_class=structlog.stdlib.BoundLogger,
    )


def _registry_ids(queue: Queue) -> list[str]:
    return ScheduledJobRegistry(queue=queue).get_job_ids()


def test_bootstrap_seeds_all_five_kinds_on_empty_queue(
    queue: Queue,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """First run on a clean Redis: every tick kind gets scheduled."""
    scheduled = _bootstrap_scheduled_jobs(queue, log)
    assert set(scheduled.keys()) == {
        "main_cycle",
        "drift",
        "summary_daily",
        "summary_weekly",
        "stale_detector",
    }
    # All five job IDs land in the registry.
    ids = _registry_ids(queue)
    assert any(jid.startswith(jobs_module.JOB_ID_MAIN_CYCLE) for jid in ids)
    assert any(jid.startswith(jobs_module.JOB_ID_DRIFT) for jid in ids)
    assert any(jid.startswith(jobs_module.JOB_ID_SUMMARY_DAILY) for jid in ids)
    assert any(jid.startswith(jobs_module.JOB_ID_SUMMARY_WEEKLY) for jid in ids)
    assert any(jid.startswith(jobs_module.JOB_ID_STALE_DETECTOR) for jid in ids)


def test_bootstrap_is_idempotent_across_calls(
    queue: Queue,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Calling bootstrap twice in a row schedules nothing the
    second time — exactly the property a restart needs.
    """
    first = _bootstrap_scheduled_jobs(queue, log)
    assert len(first) == 5  # all five seeded
    ids_after_first = sorted(_registry_ids(queue))

    second = _bootstrap_scheduled_jobs(queue, log)
    assert second == {}  # nothing additional scheduled
    ids_after_second = sorted(_registry_ids(queue))
    assert ids_after_first == ids_after_second


def test_bootstrap_seeds_only_missing_kinds(
    queue: Queue,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Pre-seed two of the five kinds manually, then bootstrap;
    only the remaining three should be added.
    """
    when = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
    queue.enqueue_at(
        when,
        jobs_module.tick_main_cycle,
        job_id=jobs_module.boundary_job_id(jobs_module.JOB_ID_MAIN_CYCLE, when),
    )
    queue.enqueue_at(
        when,
        jobs_module.tick_drift,
        job_id=jobs_module.boundary_job_id(jobs_module.JOB_ID_DRIFT, when),
    )
    scheduled = _bootstrap_scheduled_jobs(queue, log)
    assert "main_cycle" not in scheduled
    assert "drift" not in scheduled
    assert set(scheduled.keys()) == {
        "summary_daily",
        "summary_weekly",
        "stale_detector",
    }


def test_any_scheduled_with_prefix_matches_only_exact_prefix(queue: Queue) -> None:
    """Edge case: prefix matching shouldn't false-positive on
    similar-looking job IDs from unrelated jobs.
    """
    when = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
    queue.enqueue_at(when, jobs_module.tick_main_cycle, job_id="unrelated_foo_bar")
    assert not jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_MAIN_CYCLE)
    queue.enqueue_at(
        when,
        jobs_module.tick_main_cycle,
        job_id=jobs_module.boundary_job_id(jobs_module.JOB_ID_MAIN_CYCLE, when),
    )
    assert jobs_module.any_scheduled_with_prefix(queue, jobs_module.JOB_ID_MAIN_CYCLE)
