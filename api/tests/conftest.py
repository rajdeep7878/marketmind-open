"""Shared fixtures for api tests.

Tests use fakeredis instead of a real Redis: the goal is to exercise our
code paths, not Redis itself. The contract that matters (RQ enqueue ->
worker pick-up) is exercised by the opt-in integration tests in /tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from rq import Queue  # imported by the queue dep override below


def _set_test_env() -> None:
    # Provide required env vars BEFORE any module from marketmind_api is imported.
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    # Force a deterministic rate-limit value for tests, regardless of any
    # developer's `.env` override (e.g. setting it to 0 to disable the
    # guard locally). The existing TestClient-based tests assume the
    # default of 5 and would fail against a 0 .env value. Pydantic
    # env-var precedence > .env-file precedence, so this overrides .env.
    os.environ["RATE_LIMIT_INGEST_PER_DAY"] = "5"


_set_test_env()

# Imports must follow env setup so pydantic-settings sees the values.
from marketmind_api.config import get_settings  # noqa: E402
from marketmind_api.deps import (  # noqa: E402
    get_queue,
    get_redis,
    pg_ping,
)
from marketmind_api.main import create_app  # noqa: E402


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=False)


@pytest.fixture
def client(fake_redis: FakeRedis) -> Iterator[TestClient]:
    get_settings.cache_clear()
    app = create_app()

    def _fake_redis_dep() -> FakeRedis:
        return fake_redis

    def _fake_queue_dep() -> Queue:
        return Queue(name="default", connection=fake_redis)

    def _fake_pg_ping() -> bool:
        return True

    app.dependency_overrides[get_redis] = _fake_redis_dep
    app.dependency_overrides[get_queue] = _fake_queue_dep
    app.dependency_overrides[pg_ping] = _fake_pg_ping

    with TestClient(app) as c:
        yield c
