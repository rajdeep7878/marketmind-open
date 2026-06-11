"""Postgres advisory locks for trader loop coordination.

The signal-execution loop uses a per-(loop, strategy_version) lock
so the same strategy version cannot be evaluated concurrently by
two workers. v1 has a single trader_worker process, but the lock
is a defensive guard for the eventual horizontal-scaling case + a
guarantee against accidental double-runs during a deploy.

LOCK SCOPE: TRANSACTION
-----------------------
Uses `pg_try_advisory_xact_lock(key1, key2)` — TRANSACTION-scoped.
Locks auto-release on COMMIT, ROLLBACK, or disconnect. No explicit
unlock means no leak risk if the worker process crashes mid-cycle.
The signal engine wraps each strategy-version evaluation in its
own transaction, so a crash inside one strategy's lock window
releases that lock and the next worker / cycle can resume.

KEY ENCODING
------------
Two int4 args: `(loop_key, entity_key)`.
  - `loop_key` is a small per-loop constant (see _LOOP_KEYS).
  - `entity_key` is the entity's UUID hashed via crc32 to a
    signed int4. Deterministic — the same UUID always maps to
    the same key.

crc32 collisions in a 32-bit space are improbable at trader v1
scale (<<<100 strategy versions); even on a collision, the worst
case is two unrelated versions serializing each other's eval
(throughput loss, never a correctness bug).
"""

from __future__ import annotations

import zlib
from typing import Any, Final
from uuid import UUID

import psycopg
from marketmind_shared.schemas.trader import LoopName

_LOOP_KEYS: Final[dict[LoopName, int]] = {
    LoopName.INGESTION: 1,
    LoopName.SIGNAL_EXECUTION: 2,
    # Reserved for future per-runner advisory locks (e.g. if we
    # ever run two runner processes concurrently and need to gate
    # mutually-exclusive housekeeping). v1 doesn't take this lock.
    LoopName.RUNNER: 3,
}


def _crc32_signed(value: bytes) -> int:
    """Map bytes to a signed int4 (Postgres int4 = signed 32-bit).

    `zlib.crc32` returns unsigned 32-bit in [0, 2^32). Values
    >= 2^31 need to be remapped to negative for Postgres' int4
    range. Round-trip is preserved at the bit level.
    """
    h = zlib.crc32(value)
    if h >= (1 << 31):
        return h - (1 << 32)
    return h


def try_advisory_xact_lock(
    conn: psycopg.Connection[Any],
    loop: LoopName,
    strategy_version_id: UUID,
) -> bool:
    """Try to acquire a transaction-scoped advisory lock for the
    ``(loop, strategy_version_id)`` pair.

    MUST be called inside an active transaction (i.e. inside a
    ``with conn.transaction():`` block, or after `conn.commit()`
    has reset the implicit transaction). Returns True if the lock
    was acquired, False if another holder has it. Released
    automatically on the surrounding transaction's commit /
    rollback.

    The signal engine's pattern:

        with conn.transaction():
            if not try_advisory_xact_lock(conn, LoopName.SIGNAL_EXECUTION, version_id):
                continue  # another worker holds it; skip this strategy
            # ... evaluate + persist ...
        # commit auto-releases the lock
    """
    loop_key = _LOOP_KEYS[loop]
    entity_key = _crc32_signed(strategy_version_id.bytes)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(%s::int4, %s::int4)",
            (loop_key, entity_key),
        )
        row = cur.fetchone()
    if row is None:
        return False
    return bool(row[0])


__all__ = ["try_advisory_xact_lock"]
