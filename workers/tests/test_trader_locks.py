"""Tests for the trader v1 Postgres advisory-lock helpers.

Pure-function unit tests for `_crc32_signed` plus an integration
test for `try_advisory_xact_lock` against a real Postgres
container (because the lock semantics are a Postgres feature,
not Python logic).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from marketmind_shared.schemas.trader import LoopName
from marketmind_workers.trader.locks import _crc32_signed, try_advisory_xact_lock

# ---- Pure unit tests -------------------------------------------------------


def test_crc32_signed_fits_in_int4_range() -> None:
    """Every output stays within Postgres int4 (-2^31 .. 2^31 - 1)."""
    int4_min = -(1 << 31)
    int4_max = (1 << 31) - 1
    for _ in range(100):
        result = _crc32_signed(uuid4().bytes)
        assert int4_min <= result <= int4_max


def test_crc32_signed_is_deterministic() -> None:
    """Same bytes ⇒ same int4. Load-bearing for the advisory-lock
    key: a strategy version's id must map to the same key on every
    cycle so locks compose correctly across workers.
    """
    key = uuid4().bytes
    first = _crc32_signed(key)
    second = _crc32_signed(key)
    assert first == second


def test_crc32_signed_different_inputs_likely_different() -> None:
    """Statistical: 100 distinct UUIDs should produce ~100 distinct
    keys at crc32 width. Collisions in this small sample would
    indicate a hash bug, not the bounded collision rate the design
    accepts at v1 scale.
    """
    keys = {_crc32_signed(uuid4().bytes) for _ in range(100)}
    assert len(keys) == 100


# ---- Integration: try_advisory_xact_lock against real Postgres ------------


pytestmark_integration = pytest.mark.integration


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


@pytestmark_integration
def test_lock_acquires_on_first_call(database_url: str) -> None:
    version_id = uuid4()
    with psycopg.connect(database_url) as conn, conn.transaction():
        assert try_advisory_xact_lock(conn, LoopName.SIGNAL_EXECUTION, version_id) is True


@pytestmark_integration
def test_lock_blocks_concurrent_holder(database_url: str) -> None:
    """Same (loop, version) on two separate connections: second
    must return False while the first transaction is still open.
    """
    version_id = uuid4()
    with psycopg.connect(database_url) as conn_a, conn_a.transaction():
        a_acquired = try_advisory_xact_lock(
            conn_a,
            LoopName.SIGNAL_EXECUTION,
            version_id,
        )
        assert a_acquired is True

        with psycopg.connect(database_url) as conn_b, conn_b.transaction():
            b_acquired = try_advisory_xact_lock(
                conn_b,
                LoopName.SIGNAL_EXECUTION,
                version_id,
            )
            assert b_acquired is False


@pytestmark_integration
def test_lock_releases_on_commit(database_url: str) -> None:
    """Transaction commit releases the lock; the next attempt
    acquires fresh.
    """
    version_id = uuid4()
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            assert try_advisory_xact_lock(conn, LoopName.SIGNAL_EXECUTION, version_id)
        # Commit happened — lock should be released.
        with conn.transaction():
            assert try_advisory_xact_lock(conn, LoopName.SIGNAL_EXECUTION, version_id)


@pytestmark_integration
def test_lock_releases_on_rollback(database_url: str) -> None:
    """Exception inside the transaction triggers rollback; lock
    still releases. This is the load-bearing property that keeps
    a crashed worker from leaving a stuck lock.
    """
    version_id = uuid4()
    with psycopg.connect(database_url) as conn:
        try:
            with conn.transaction():
                assert try_advisory_xact_lock(
                    conn,
                    LoopName.SIGNAL_EXECUTION,
                    version_id,
                )
                raise RuntimeError("simulated worker crash")
        except RuntimeError:
            pass
        with conn.transaction():
            # Lock released by the rollback.
            assert try_advisory_xact_lock(conn, LoopName.SIGNAL_EXECUTION, version_id)


@pytestmark_integration
def test_different_versions_dont_block_each_other(database_url: str) -> None:
    """The (loop_key, entity_key) keyspace is per-version. Two
    concurrent evaluations of DIFFERENT versions can hold their
    locks simultaneously.
    """
    version_a = uuid4()
    version_b = uuid4()
    with psycopg.connect(database_url) as conn_a, conn_a.transaction():
        assert try_advisory_xact_lock(conn_a, LoopName.SIGNAL_EXECUTION, version_a)
        with psycopg.connect(database_url) as conn_b, conn_b.transaction():
            assert try_advisory_xact_lock(conn_b, LoopName.SIGNAL_EXECUTION, version_b)


@pytestmark_integration
def test_different_loops_dont_block_each_other(database_url: str) -> None:
    """The (loop_key) differs between LoopName.INGESTION and
    LoopName.SIGNAL_EXECUTION, so a single version_id can be
    locked by both loops simultaneously.
    """
    version_id = uuid4()
    with psycopg.connect(database_url) as conn_ing, conn_ing.transaction():
        assert try_advisory_xact_lock(conn_ing, LoopName.INGESTION, version_id)
        with psycopg.connect(database_url) as conn_sig, conn_sig.transaction():
            assert try_advisory_xact_lock(conn_sig, LoopName.SIGNAL_EXECUTION, version_id)
