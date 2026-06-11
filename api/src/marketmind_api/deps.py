"""FastAPI dependency providers.

Returning a Redis connection from a dep keeps the route handlers
testable — tests can override `get_redis` with a `fakeredis` instance.
"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import Depends
from redis import Redis
from rq import Queue

from marketmind_api.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_redis(settings: SettingsDep) -> Redis:
    # decode_responses=False — RQ stores pickled job payloads and breaks
    # if Redis auto-decodes to str. Keep raw bytes.
    return Redis.from_url(str(settings.redis_url), decode_responses=False)


RedisDep = Annotated[Redis, Depends(get_redis)]


def get_queue(redis: RedisDep, settings: SettingsDep) -> Queue:
    return Queue(name=settings.rq_queue_name, connection=redis)


QueueDep = Annotated[Queue, Depends(get_queue)]


def pg_ping(settings: SettingsDep) -> bool:
    """Liveness check for Postgres — used only by /health.

    We deliberately don't hold a long-lived connection in Phase 0; once
    real DB work starts, we'll switch to a connection pool (psycopg_pool).
    """
    try:
        with (
            psycopg.connect(str(settings.database_url), connect_timeout=2) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


PgPingDep = Annotated[bool, Depends(pg_ping)]


def get_database_url(settings: SettingsDep) -> str:
    """Return the raw Postgres URL for repo helpers.

    The shared workers/db/repo.py CRUD helpers take a plain URL — the
    API process re-uses them rather than re-implementing SQL. We
    deliberately do NOT hold a pool here; query volume is low in 2.1
    and a pool can come in Phase 3 when batch work appears.
    """
    return str(settings.database_url)


DatabaseUrlDep = Annotated[str, Depends(get_database_url)]
