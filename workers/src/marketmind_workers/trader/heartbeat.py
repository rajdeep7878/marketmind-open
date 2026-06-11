"""Shared bot-run heartbeat helpers.

The `trader_bot_runs` table is the runner process's liveness
record. Step 1 created it; Step 12 collapses the per-loop model
into a single runner process and uses this module to centralise
all reads/writes of that table.

Lifecycle:

  1. Runner boots          → `create_bot_run()`     → status='running'
  2. Each phase completes  → `touch_heartbeat()`    → last_heartbeat_at=NOW(),
                                                       phase recorded in `notes`
  3. Runner exits cleanly  → `mark_stopped()`       → status='stopped'
  4. Runner dies silently  → no heartbeat → detector → status='crashed'
                              via `find_stale_runs()` + `mark_crashed()`

Why a separate module:

  - Pre-Step-12 the helper was duplicated in `ingestion.py` and
    `signal_engine.py`. Refactoring them to use this module
    matches the Step 12 carry-forward checklist (item 1).
  - The Step 12 runner needs `create_bot_run`, `mark_stopped`,
    and `mark_crashed` — none of which existed before.
  - The stale-heartbeat detector (item 8 of the checklist) needs
    `find_stale_runs` and an atomic transition helper.

Concurrency note: every function in this module is transaction-
agnostic — it operates on a passed-in `psycopg.Connection` and
does NOT commit. The caller decides the transaction boundary.
This lets the runner fold heartbeat updates into the same
transaction as the work they accompany (so a hung commit
matches a stale heartbeat).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
import structlog
from marketmind_shared.schemas.trader import LoopName, RunStatus

log = structlog.get_logger(__name__)


# Phase tag stored in trader_bot_runs.notes. Convention is
# `phase=<name>` — kept simple so a SQL query (`SELECT notes`) is
# enough to see where the runner was last seen. We use prefixed
# strings rather than a JSONB column to avoid another schema
# migration; the column is already free-form text.
_PHASE_PREFIX = "phase="


# ---- Lifecycle: create / stopped / crashed --------------------------------


def create_bot_run(
    conn: psycopg.Connection[Any],
    *,
    loop_name: LoopName,
    worker_id: str,
    started_at: datetime | None = None,
) -> UUID:
    """Insert a new `trader_bot_runs` row, return its id.

    Status starts at 'running'. `started_at` defaults to NOW()
    when not provided — the parameter exists so tests can pin a
    deterministic timestamp.
    """
    with conn.cursor() as cur:
        if started_at is None:
            cur.execute(
                """
                INSERT INTO trader_bot_runs (loop_name, status, worker_id)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (loop_name.value, RunStatus.RUNNING.value, worker_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO trader_bot_runs
                    (loop_name, started_at, last_heartbeat_at, status, worker_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    loop_name.value,
                    started_at,
                    started_at,
                    RunStatus.RUNNING.value,
                    worker_id,
                ),
            )
        row = cur.fetchone()
    assert row is not None  # INSERT … RETURNING always returns a row
    return UUID(str(row[0]))


def mark_stopped(conn: psycopg.Connection[Any], run_id: UUID) -> None:
    """Transition a run to status='stopped'. Idempotent.

    Used by the runner's SIGTERM path after the current cycle
    completes — separate from `mark_crashed` so a graceful
    shutdown is distinguishable from a death.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trader_bot_runs
            SET status = %s, last_heartbeat_at = NOW()
            WHERE id = %s
            """,
            (RunStatus.STOPPED.value, str(run_id)),
        )


def mark_crashed(
    conn: psycopg.Connection[Any],
    run_id: UUID,
    *,
    reason: str = "",
) -> None:
    """Transition a run to status='crashed'. Idempotent.

    The reason is stored in `notes` for forensic queries — kept
    short, since the column has no length limit but log
    aggregators usually do.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trader_bot_runs
            SET status = %s, notes = %s
            WHERE id = %s
            """,
            (RunStatus.CRASHED.value, f"crashed: {reason}"[:512], str(run_id)),
        )


# ---- Heartbeat -------------------------------------------------------------


def touch_heartbeat(
    conn: psycopg.Connection[Any],
    run_id: UUID,
    *,
    phase: str | None = None,
) -> None:
    """Set `last_heartbeat_at = NOW()` for the given run.

    If `phase` is provided, also overwrite `notes` with
    `phase=<phase>` so the stale-detector can include the
    last-seen phase in its alert. Pass `phase=None` to refresh
    the timestamp without rewriting notes (used by the runner
    once per outer loop iteration, between phases).
    """
    with conn.cursor() as cur:
        if phase is None:
            cur.execute(
                "UPDATE trader_bot_runs SET last_heartbeat_at = NOW() WHERE id = %s",
                (str(run_id),),
            )
        else:
            cur.execute(
                """
                UPDATE trader_bot_runs
                SET last_heartbeat_at = NOW(), notes = %s
                WHERE id = %s
                """,
                (f"{_PHASE_PREFIX}{phase}", str(run_id)),
            )


# ---- Stale-heartbeat detection --------------------------------------------


def find_stale_runs(
    conn: psycopg.Connection[Any],
    *,
    threshold_seconds: int,
) -> list[tuple[UUID, str, datetime, str]]:
    """Return rows whose status='running' and last_heartbeat_at
    is older than `threshold_seconds` ago.

    Each tuple is `(id, loop_name, last_heartbeat_at, notes)` so
    the caller can build a useful alert without a second
    fetch. v1's detector uses threshold_seconds=300 (5 min).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, loop_name, last_heartbeat_at, notes
            FROM trader_bot_runs
            WHERE status = %s
              AND last_heartbeat_at < NOW() - make_interval(secs => %s)
            ORDER BY last_heartbeat_at ASC
            """,
            (RunStatus.RUNNING.value, threshold_seconds),
        )
        rows = cur.fetchall()
    return [(UUID(str(r[0])), r[1], r[2], r[3]) for r in rows]


def fetch_run_status(
    conn: psycopg.Connection[Any], run_id: UUID,
) -> RunStatus | None:
    """Return the current status of a run, or None if the row is
    missing. Mostly used by tests; the runner doesn't need it
    inline.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM trader_bot_runs WHERE id = %s",
            (str(run_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RunStatus(row[0])


__all__ = [
    "create_bot_run",
    "fetch_run_status",
    "find_stale_runs",
    "mark_crashed",
    "mark_stopped",
    "touch_heartbeat",
]
