"""End-to-end test exercising real Redis + a real worker subprocess.

Opt-in. Run with:
    uv run pytest -m integration

This spins up a Redis container, starts an RQ worker subprocess wired to
it, then talks to a `TestClient`-hosted API instance whose dependencies
point at the same Redis. If this passes, the API <-> worker contract via
RQ (job string reference, serialization, return-value pickling) is sound.

Marked `integration` so it doesn't run by default — it pulls a docker
image and needs the docker daemon available.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.redis")
from testcontainers.redis import RedisContainer  # noqa: E402


@pytest.fixture(scope="module")
def redis_container() -> Iterator[RedisContainer]:
    container = RedisContainer("redis:7.4-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture(scope="module")
def worker_process(redis_url: str) -> Iterator[subprocess.Popen[bytes]]:
    env = {
        **os.environ,
        "ENVIRONMENT": "test",
        "LOG_LEVEL": "INFO",
        "REDIS_URL": redis_url,
        "RQ_QUEUE_NAME": "default",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "marketmind_workers.worker"],
        env=env,
    )
    # Give the worker a moment to connect.
    time.sleep(1.0)
    try:
        if proc.poll() is not None:
            raise RuntimeError("worker exited before tests started")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def api_client(redis_url: str) -> Iterator[Any]:
    # Late imports so env is set before pydantic-settings reads it.
    os.environ["ENVIRONMENT"] = "test"
    os.environ["LOG_LEVEL"] = "WARNING"
    os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"
    os.environ["REDIS_URL"] = redis_url
    os.environ["RQ_QUEUE_NAME"] = "default"

    from fastapi.testclient import TestClient
    from marketmind_api.config import get_settings
    from marketmind_api.deps import pg_ping
    from marketmind_api.main import create_app

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[pg_ping] = lambda: True

    with TestClient(app) as c:
        yield c


def test_end_to_end_dummy_job(
    api_client: Any,
    worker_process: subprocess.Popen[bytes],
) -> None:
    assert worker_process.poll() is None

    submit = api_client.post(
        "/jobs",
        json={"kind": "dummy", "payload": {"message": "e2e-hello"}},
    )
    assert submit.status_code == 201, submit.text
    job_id = submit.json()["id"]

    # Poll for completion (job sleeps 0.5s; allow generous slack).
    deadline = time.time() + 15.0
    final_status: str | None = None
    final_body: dict[str, Any] = {}
    while time.time() < deadline:
        resp = api_client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        final_body = body
        final_status = body["status"]
        if final_status in {"finished", "failed"}:
            break
        time.sleep(0.2)

    assert final_status == "finished", final_body
    assert final_body["result"]["echoed"] == "e2e-hello"
