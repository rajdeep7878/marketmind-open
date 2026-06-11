from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_ok_when_dependencies_are_healthy(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "environment": "test",
        "postgres": "ok",
        "redis": "ok",
    }


def test_health_response_schema_is_stable(client: TestClient) -> None:
    # Guards against accidental field renames — the frontend's typed
    # fetch wrapper depends on exactly these keys.
    resp = client.get("/health")
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"status", "environment", "postgres", "redis"}
