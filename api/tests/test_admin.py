"""Tests for /admin/stats.

The endpoint touches Postgres, Redis, and RQ's FailedJobRegistry. We
already use fakeredis for the queue + Redis state, and monkeypatch the
Postgres-touching helpers so the tests stay hermetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_api.config import Settings, get_settings
from marketmind_api.routes import admin as admin_routes
from marketmind_shared.rate_limits import daily_cost_key, daily_ratelimit_rejection_key


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    """Same TestClient, but with admin credentials configured."""

    def _settings_with_admin() -> Settings:
        return get_settings().model_copy(
            update={"admin_username": "admin", "admin_password": "secret"},
        )

    from marketmind_api.config import get_settings as get_settings_dep

    client.app.dependency_overrides[get_settings_dep] = _settings_with_admin
    return client


@pytest.fixture
def _stub_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two Postgres helpers in routes.admin with deterministic data."""
    monkeypatch.setattr(admin_routes, "_fetch_submission_buckets", lambda _url: (3, 12, 47))
    monkeypatch.setattr(admin_routes, "_fetch_spend_buckets", lambda _url: (0.42, 2.10, 8.55))
    # No failed jobs in the registry by default. Override per-test if needed.
    monkeypatch.setattr(admin_routes, "_recent_errors", lambda _q, _r, *, limit: (0, []))


# ---- auth path -----------------------------------------------------------


def test_admin_stats_requires_credentials_when_configured(admin_client: TestClient) -> None:
    resp = admin_client.get("/admin/stats")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_admin_stats_rejects_wrong_credentials(admin_client: TestClient) -> None:
    resp = admin_client.get("/admin/stats", auth=("admin", "wrong"))
    assert resp.status_code == 401


def test_admin_stats_returns_503_when_creds_unconfigured(client: TestClient) -> None:
    """Force settings to have empty admin creds. The previous version
    of this test relied on the bare environment having empty admin
    creds, which broke once an operator set them via .env per the
    runbook recommendation.
    """
    from marketmind_api.config import get_settings

    def _empty_admin() -> Settings:
        return get_settings().model_copy(
            update={"admin_username": "", "admin_password": ""},
        )

    client.app.dependency_overrides[get_settings] = _empty_admin

    resp = client.get("/admin/stats", auth=("admin", "secret"))
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "admin_disabled"


# ---- happy path ----------------------------------------------------------


def test_admin_stats_renders_full_payload(
    admin_client: TestClient,
    fake_redis: FakeRedis,
    _stub_db: None,
) -> None:
    # Seed Redis counters.
    fake_redis.set(daily_cost_key(), b"123")  # cents
    fake_redis.set(daily_ratelimit_rejection_key(), b"4")

    resp = admin_client.get("/admin/stats", auth=("admin", "secret"))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()

    assert body["submissions"] == {"today": 3, "week": 12, "total": 47}
    assert body["spend"]["today_usd"] == 0.42
    assert body["spend"]["week_usd"] == 2.10
    assert body["spend"]["total_usd"] == 8.55
    assert body["cost_cap"]["current_usd"] == 1.23
    assert body["cost_cap"]["cap_gbp"] == 5.0
    # cap_usd ≈ 5 * 1.27 = 6.35
    assert body["cost_cap"]["cap_usd"] == 6.35
    assert body["ratelimit_rejections_today"] == 4
    assert body["errors_24h_count"] == 0
    assert body["recent_errors"] == []

    # generated_at parses as a real datetime
    assert datetime.fromisoformat(body["generated_at"]).tzinfo is not None


def test_admin_stats_includes_recent_errors(
    admin_client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_routes, "_fetch_submission_buckets", lambda _url: (0, 0, 0))
    monkeypatch.setattr(admin_routes, "_fetch_spend_buckets", lambda _url: (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        admin_routes,
        "_recent_errors",
        lambda _q, _r, *, limit: (
            2,
            [
                admin_routes._ErrorItem(
                    job_id="job-1",
                    ended_at=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
                    kind="extract_strategy",
                    exception="ValueError: bad transcript",
                ),
            ],
        ),
    )

    resp = admin_client.get("/admin/stats", auth=("admin", "secret"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["errors_24h_count"] == 2
    assert len(body["recent_errors"]) == 1
    assert body["recent_errors"][0]["job_id"] == "job-1"
    assert body["recent_errors"][0]["kind"] == "extract_strategy"
