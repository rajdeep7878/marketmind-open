"""Multi-process SIGTERM smoke test for the trader runner.

Step 12's in-process test mocks `Worker.work()` so SIGTERM never
travels through the operating-system signal machinery. This test
fills that gap: spawn the real runner as a subprocess, send a real
SIGTERM, and verify the graceful-shutdown path lands the bot_run
row in status='stopped'.

Why this matters: the Step 12 design relies on RQ 2's
`Worker.work(with_scheduler=True)` to install its OWN signal
handler, finish the current job, and return. A bug in that chain
(e.g., an unhandled exception that bypasses .work()'s cleanup)
would leave the runner with status='running' until the
stale-detector picks it up. This test verifies the actual signal
path end-to-end.

Cost: ~6 seconds per run (the subprocess needs time to boot:
apply migrations, bootstrap, install signal handlers, enter the
RQ work loop). Marked integration; opt-in via `-m integration`.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.integration


# ---- Container fixtures ---------------------------------------------------


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
def redis_container() -> Iterator[Any]:
    pytest.importorskip("testcontainers.redis")
    from testcontainers.redis import RedisContainer

    container = RedisContainer("redis:7.4-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: Any) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module")
def redis_url(redis_container: Any) -> str:
    host = redis_container.get_container_host_ip()  # type: ignore[attr-defined]
    port = redis_container.get_exposed_port(6379)  # type: ignore[attr-defined]
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


# ---- Test helpers ----------------------------------------------------------


def _subprocess_env(database_url: str, redis_url: str) -> dict[str, str]:
    """Env vars the spawned runner needs. Inherit the test
    environment for $PATH etc.; override the data-layer URLs and
    the paper-only guard.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["REDIS_URL"] = redis_url
    env["TRADER_ALLOW_LIVE"] = "false"
    env["TRADER_QUEUE_NAME"] = "trader_default_subprocess"
    env["TRADER_SYMBOLS"] = "BTC/USDT"
    env["TRADER_TIMEFRAMES"] = "1h"
    env["TRADER_STARTING_CASH_GBP"] = "10000"
    env["LOG_LEVEL"] = "WARNING"
    env["ENVIRONMENT"] = "test"
    return env


def _wait_for_running_row(
    database_url: str,
    *,
    timeout_s: float = 15.0,
    exclude: set[str] | None = None,
) -> str:
    """Poll until a `trader_bot_runs` row with status='running'
    that isn't in `exclude` appears. Returns its id. Raises
    TimeoutError if none.

    `exclude` lets the orphan-cleanup test wait for the *new*
    runner's row to appear, skipping the stale row from a
    previous SIGKILL'd runner that's still status='running'
    until orphan cleanup transitions it.
    """
    skip = exclude or set()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM trader_bot_runs "
                "WHERE status = 'running' ORDER BY started_at DESC",
            )
            rows = cur.fetchall()
        for row in rows:
            run_id = str(row[0])
            if run_id not in skip:
                return run_id
        time.sleep(0.2)
    msg = f"runner did not register a 'running' bot_run within {timeout_s}s"
    raise TimeoutError(msg)


def _wait_for_status(
    database_url: str,
    run_id: str,
    expected: str,
    *,
    timeout_s: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_status: str | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM trader_bot_runs WHERE id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if row is not None:
            last_status = row[0]
            if last_status == expected:
                return
        time.sleep(0.2)
    msg = (
        f"runner row {run_id} did not transition to '{expected}' "
        f"within {timeout_s}s (last observed: {last_status!r})"
    )
    raise AssertionError(msg)


# ---- The test --------------------------------------------------------------


def test_runner_subprocess_sigterm_marks_stopped(
    database_url: str,
    redis_url: str,
) -> None:
    """Spawn the real runner; SIGTERM it; verify graceful shutdown."""
    # Clean slate so _wait_for_running_row finds OUR new row.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE trader_bot_runs RESTART IDENTITY CASCADE")
        conn.commit()

    env = _subprocess_env(database_url, redis_url)
    proc = subprocess.Popen(
        [sys.executable, "-m", "marketmind_workers.trader.runner"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Run in its own process group so SIGTERM to the pid
        # propagates to the worker rather than getting swallowed
        # by any shell wrapper.
        start_new_session=True,
    )
    captured_err = b""
    try:
        run_id = _wait_for_running_row(database_url)
        # The bot_run row gets written BEFORE the runner enters
        # worker.work() and installs its SIGTERM handler. A small
        # sleep lets the worker reach the work loop; without it,
        # SIGTERM can arrive while Python's default handler is
        # still in effect → exit code -15 (signal kill, not
        # graceful return). 1s is empirical headroom; the test
        # under high CI load still exits in <5s total.
        time.sleep(1.0)
        # Send SIGTERM. RQ's worker.work() should drain the
        # current job (none in this test) and return cleanly.
        proc.send_signal(signal.SIGTERM)
        try:
            _, captured_err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, captured_err = proc.communicate(timeout=5)
            raise AssertionError(
                "runner subprocess did not exit within 15s of SIGTERM. "
                f"STDERR:\n{captured_err.decode(errors='replace')[:2000]}",
            ) from None

        # The runner's graceful-shutdown path writes 'stopped'
        # BEFORE process exit. The subprocess has now exited so
        # the row should already reflect the final state.
        try:
            _wait_for_status(database_url, run_id, "stopped", timeout_s=5.0)
        except AssertionError:
            raise AssertionError(
                "runner did not mark its bot_run as 'stopped' after SIGTERM. "
                f"Exit code: {proc.returncode}. STDERR:\n"
                f"{captured_err.decode(errors='replace')[:2000]}",
            ) from None
    finally:
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=5)


# Note on orphan-cleanup via SIGKILL'd subprocess: the in-process
# test `test_runner_main_orphans_previous_running_row` in
# `test_trader_heartbeat_and_runner_integration.py` already covers
# the orphan-cleanup logic end-to-end (it calls
# mark_orphaned_runs_crashed from runner.main() against a pre-
# seeded stale row). Doing it via SIGKILL + subprocess adds
# OS-level signal coverage but also process-group cleanup
# complexity (RQ 2's in-process scheduler forks a child for the
# scheduler thread; SIGKILL'ing the parent leaves the child
# holding stdin/stdout pipes open and pytest's unraisable-
# exception capture flags it as a failure). Skipped for v1;
# revisit if a real-world bug surfaces in this path.
