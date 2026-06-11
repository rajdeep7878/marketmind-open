"""Integration tests for the trader v1 heartbeat / stale-detector / SIGTERM paths.

testcontainers Postgres + apply_migrations. Covers:

  - mark_orphaned_runs_crashed promotes stale 'running' rows to
    'crashed' on a fresh runner boot.
  - find_stale_runs returns rows whose last_heartbeat_at is older
    than the threshold.
  - touch_heartbeat refreshes the timestamp + records the phase
    in `notes`.
  - tick_stale_heartbeat_detector marks the stale row 'crashed'
    AND inserts a critical alert.
  - SIGTERM simulation: runner.main() can be driven through to
    its mark_stopped() path by mocking Worker.work() to return
    immediately.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID

import psycopg
import pytest
from marketmind_shared.schemas.trader import LoopName, RunStatus
from marketmind_workers.trader import heartbeat as heartbeat_module
from marketmind_workers.trader import jobs as jobs_module
from marketmind_workers.trader.config import get_trader_settings

# ---- testcontainers fixtures ----------------------------------------------


@pytest.fixture(scope="module")
def pg_container() -> Iterator[Any]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: Any) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def _clean(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE trader_bot_runs RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE trader_alerts RESTART IDENTITY CASCADE")
        conn.commit()


@pytest.fixture
def trader_settings(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point get_trader_settings() at the testcontainer DB."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("TRADER_ALLOW_LIVE", "false")
    get_trader_settings.cache_clear()


# ---- Heartbeat lifecycle ---------------------------------------------------


def test_create_bot_run_inserts_running_row(
    database_url: str,
    _clean: None,
) -> None:
    with psycopg.connect(database_url) as conn:
        run_id = heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id="test:1",
        )
        conn.commit()
        status = heartbeat_module.fetch_run_status(conn, run_id)
    assert status == RunStatus.RUNNING


def test_touch_heartbeat_updates_phase_in_notes(
    database_url: str,
    _clean: None,
) -> None:
    with psycopg.connect(database_url) as conn:
        run_id = heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id="test:1",
        )
        heartbeat_module.touch_heartbeat(conn, run_id, phase="signal")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT notes FROM trader_bot_runs WHERE id = %s", (str(run_id),))
            row = cur.fetchone()
    assert row is not None
    assert row[0] == "phase=signal"


def test_mark_stopped_transitions_status(database_url: str, _clean: None) -> None:
    with psycopg.connect(database_url) as conn:
        run_id = heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id="test:1",
        )
        heartbeat_module.mark_stopped(conn, run_id)
        conn.commit()
        status = heartbeat_module.fetch_run_status(conn, run_id)
    assert status == RunStatus.STOPPED


def test_mark_crashed_transitions_and_records_reason(
    database_url: str,
    _clean: None,
) -> None:
    with psycopg.connect(database_url) as conn:
        run_id = heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id="test:1",
        )
        heartbeat_module.mark_crashed(conn, run_id, reason="OOM at phase=execute")
        conn.commit()
        status = heartbeat_module.fetch_run_status(conn, run_id)
        with conn.cursor() as cur:
            cur.execute("SELECT notes FROM trader_bot_runs WHERE id = %s", (str(run_id),))
            row = cur.fetchone()
    assert status == RunStatus.CRASHED
    assert row is not None
    assert "OOM at phase=execute" in row[0]


# ---- Stale-heartbeat detector ---------------------------------------------


def _insert_stale_run(
    conn: psycopg.Connection[Any],
    *,
    age_seconds: int,
    loop_name: str = "runner",
    worker_id: str = "stale:1",
) -> UUID:
    """Insert a 'running' row whose last_heartbeat_at is in the past."""
    with conn.cursor() as cur:
        stale_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
        cur.execute(
            """
            INSERT INTO trader_bot_runs
                (loop_name, started_at, last_heartbeat_at, status, worker_id, notes)
            VALUES (%s, %s, %s, 'running', %s, 'phase=signal')
            RETURNING id
            """,
            (loop_name, stale_at, stale_at, worker_id),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def test_find_stale_runs_returns_rows_older_than_threshold(
    database_url: str,
    _clean: None,
) -> None:
    with psycopg.connect(database_url) as conn:
        stale_id = _insert_stale_run(conn, age_seconds=600)
        # A fresh row should not be returned
        fresh_id = heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id="fresh:1",
        )
        conn.commit()
        stale = heartbeat_module.find_stale_runs(conn, threshold_seconds=300)
    stale_ids = {item[0] for item in stale}
    assert stale_id in stale_ids
    assert fresh_id not in stale_ids


def test_stale_detector_marks_crashed_and_emits_alert(
    database_url: str,
    _clean: None,
    trader_settings: None,
) -> None:
    """The end-to-end behaviour the operator depends on."""
    with psycopg.connect(database_url) as conn:
        stale_id = _insert_stale_run(conn, age_seconds=600)
        conn.commit()

    # Run the detector (its re-enqueue uses Redis; mock that out).
    with patch.object(jobs_module, "_connect_queue") as mock_queue:
        mock_queue.return_value.enqueue_at = lambda *args, **kwargs: None
        jobs_module.tick_stale_heartbeat_detector()

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM trader_bot_runs WHERE id = %s",
            (str(stale_id),),
        )
        status_row = cur.fetchone()
        cur.execute(
            "SELECT severity, subject FROM trader_alerts WHERE delivered = FALSE "
            "ORDER BY ts DESC LIMIT 1",
        )
        alert_row = cur.fetchone()

    assert status_row is not None
    assert status_row[0] == "crashed"
    assert alert_row is not None
    assert alert_row[0] == "critical"
    assert "stale heartbeat" in alert_row[1].lower()


# ---- Orphan cleanup --------------------------------------------------------


def test_mark_orphaned_runs_crashed_skips_current_run(
    database_url: str,
    _clean: None,
) -> None:
    """A new runner's bootstrap claim — all pre-existing 'running'
    rows except its own go to 'crashed'.
    """
    with psycopg.connect(database_url) as conn:
        orphan_a = _insert_stale_run(conn, age_seconds=60, worker_id="orphan:a")
        orphan_b = _insert_stale_run(conn, age_seconds=60, worker_id="orphan:b")
        current = heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id="current:1",
        )
        conn.commit()

    count = jobs_module.mark_orphaned_runs_crashed(database_url, current)
    assert count == 2

    with psycopg.connect(database_url) as conn:
        statuses = {
            run_id: heartbeat_module.fetch_run_status(conn, run_id)
            for run_id in (orphan_a, orphan_b, current)
        }
    assert statuses[orphan_a] == RunStatus.CRASHED
    assert statuses[orphan_b] == RunStatus.CRASHED
    assert statuses[current] == RunStatus.RUNNING


# ---- Runner SIGTERM (graceful shutdown) -----------------------------------


def test_runner_main_marks_stopped_when_worker_returns(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end SIGTERM simulation: mock Worker.work() so it
    returns immediately (mimicking SIGTERM); verify the
    bot_run row transitions to 'stopped'.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:9999/0")  # never used
    monkeypatch.setenv("TRADER_ALLOW_LIVE", "false")
    monkeypatch.setenv("TRADER_QUEUE_NAME", "trader_default_test")
    get_trader_settings.cache_clear()
    from marketmind_workers.config import get_settings as get_worker_settings

    get_worker_settings.cache_clear()

    # Use a fake Redis under the hood (the test never starts a worker).
    from fakeredis import FakeRedis
    from marketmind_workers.trader import runner

    fake = FakeRedis(decode_responses=False)
    monkeypatch.setattr(
        "marketmind_workers.trader.runner.Redis.from_url",
        lambda *_args, **_kwargs: fake,
    )

    # Make Worker.work() return immediately to simulate SIGTERM.
    worker_calls: dict[str, bool] = {"work_called": False}

    class _FakeWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs

        def work(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            worker_calls["work_called"] = True

    monkeypatch.setattr("marketmind_workers.trader.runner.Worker", _FakeWorker)

    # Run the runner. Should return 0 (graceful shutdown path).
    exit_code = runner.main()

    assert exit_code == 0
    assert worker_calls["work_called"] is True

    # The most recent bot_run row should be status='stopped'.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM trader_bot_runs "
            "WHERE worker_id LIKE 'localhost:%' OR worker_id LIKE '%:%' "
            "ORDER BY started_at DESC LIMIT 1",
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "stopped"


def test_runner_main_orphans_previous_running_row(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart safety: a runner boot must mark any previous
    'running' row 'crashed' so the new run is the sole 'running'
    one. Mimics the case where the previous runner was kill -9'd.
    """
    # Pre-seed a stale 'running' row.
    with psycopg.connect(database_url) as conn:
        previous_id = _insert_stale_run(conn, age_seconds=60, worker_id="previous:1")
        conn.commit()

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:9999/0")
    monkeypatch.setenv("TRADER_ALLOW_LIVE", "false")
    monkeypatch.setenv("TRADER_QUEUE_NAME", "trader_default_test2")
    get_trader_settings.cache_clear()
    from marketmind_workers.config import get_settings as get_worker_settings

    get_worker_settings.cache_clear()

    from fakeredis import FakeRedis

    fake = FakeRedis(decode_responses=False)
    monkeypatch.setattr(
        "marketmind_workers.trader.runner.Redis.from_url",
        lambda *_args, **_kwargs: fake,
    )

    class _FakeWorker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs

        def work(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs

    monkeypatch.setattr("marketmind_workers.trader.runner.Worker", _FakeWorker)

    from marketmind_workers.trader import runner

    exit_code = runner.main()
    assert exit_code == 0

    # Previous row should now be 'crashed'.
    with psycopg.connect(database_url) as conn:
        previous_status = heartbeat_module.fetch_run_status(conn, previous_id)
    assert previous_status == RunStatus.CRASHED


# ---- Marker so pytest -m integration picks these up -----------------------


pytestmark = pytest.mark.integration


# Silence pyright on unused import (os is needed for the testcontainer env).
_ = os
