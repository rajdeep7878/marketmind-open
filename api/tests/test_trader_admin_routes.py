"""Tests for /trader/strategies/{id}/{enable,disable,approve_paper}.

Mirrors the test_admin.py pattern: reuses the shared `client`
fixture, overrides settings with admin credentials, monkeypatches
the read/write helpers in `marketmind_api.trader.read` so we
don't need testcontainers.

Three areas under test:
  1. Auth gate (401 wrong creds, 503 unconfigured creds).
  2. 404 on unknown strategy_version_id.
  3. approve_paper backtest_metrics JSONB shape validation
     (HTTP 422 with named missing-key list).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from marketmind_api.config import Settings
from marketmind_api.config import get_settings as get_settings_dep
from marketmind_api.trader import read as trader_read

_VID = uuid4()


# ---- Fixtures --------------------------------------------------------------


def _settings_with_admin() -> Settings:
    return get_settings_dep().model_copy(
        update={"admin_username": "admin", "admin_password": "secret"},
    )


def _settings_without_admin() -> Settings:
    """Force admin_username / admin_password to empty, regardless of
    whatever the host's `.env` happens to have set. Necessary because
    pydantic-settings honours `.env` and a local-dev .env (the
    operator runbook recommends setting real creds) would otherwise
    bleed into these "admin disabled" tests.
    """
    return get_settings_dep().model_copy(
        update={"admin_username": "", "admin_password": ""},
    )


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    client.app.dependency_overrides[get_settings_dep] = _settings_with_admin
    return client


@pytest.fixture
def client_admin_disabled(client: TestClient) -> TestClient:
    """A `client` with admin auth explicitly disabled — for the
    "503 when unconfigured" tests that previously relied on the
    bare environment having empty admin creds.
    """
    client.app.dependency_overrides[get_settings_dep] = _settings_without_admin
    return client


def _valid_backtest_metrics() -> dict[str, Any]:
    """The canonical two-subtree shape the drift analyzer (Step 9)
    expects. Tests use this as the baseline + drop keys to test the
    validator.
    """
    return {
        "walk_forward": {"out_of_sample_trade_freq_per_week": 3.5},
        "single_pass": {
            "win_rate": 0.55,
            "avg_return_per_trade": 0.012,
            "max_drawdown_pct": 0.08,
        },
    }


def _stub_fetch_version(
    monkeypatch: pytest.MonkeyPatch,
    *,
    found: bool = True,
    enabled: bool = True,
    approved_for_paper: bool = False,
    backtest_metrics: dict[str, Any] | None = None,
) -> None:
    """Stub `fetch_version_for_admin` to return a synthetic row
    (or None if `found=False`).
    """
    if backtest_metrics is None:
        backtest_metrics = _valid_backtest_metrics()

    def _fake(_url: str, version_id: UUID) -> dict[str, Any] | None:
        if not found:
            return None
        return {
            "id": str(version_id),
            "enabled": enabled,
            "approved_for_paper": approved_for_paper,
            "backtest_metrics": backtest_metrics,
        }

    monkeypatch.setattr(trader_read, "fetch_version_for_admin", _fake)


def _stub_update_flag(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture calls to update_version_flag for assertion."""
    captured: dict[str, Any] = {}

    def _fake(
        _url: str,
        version_id: UUID,
        *,
        enabled: bool | None = None,
        approved_for_paper: bool | None = None,
    ) -> bool:
        captured["version_id"] = version_id
        captured["enabled"] = enabled
        captured["approved_for_paper"] = approved_for_paper
        return True

    monkeypatch.setattr(trader_read, "update_version_flag", _fake)
    return captured


# ---- Auth ------------------------------------------------------------------


def test_enable_requires_credentials_when_configured(admin_client: TestClient) -> None:
    resp = admin_client.post(f"/trader/strategies/{_VID}/enable")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_disable_rejects_wrong_credentials(admin_client: TestClient) -> None:
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/disable",
        auth=("admin", "wrong"),
    )
    assert resp.status_code == 401


def test_approve_paper_returns_503_when_creds_unconfigured(
    client_admin_disabled: TestClient,
) -> None:
    resp = client_admin_disabled.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "admin_disabled"


# ---- /trader/admin/auth_check (the modal's probe) -------------------------


def test_auth_check_returns_200_on_valid_creds(admin_client: TestClient) -> None:
    """The dashboard's credential modal POSTs creds, hits this
    endpoint to confirm them, and only stores in sessionStorage
    on 200.
    """
    resp = admin_client.get("/trader/admin/auth_check", auth=("admin", "secret"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "username": "admin"}


def test_auth_check_rejects_wrong_password(admin_client: TestClient) -> None:
    resp = admin_client.get("/trader/admin/auth_check", auth=("admin", "wrong"))
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_auth_check_rejects_missing_credentials(admin_client: TestClient) -> None:
    resp = admin_client.get("/trader/admin/auth_check")
    assert resp.status_code == 401


def test_auth_check_returns_503_when_admin_disabled(
    client_admin_disabled: TestClient,
) -> None:
    """A deployment with no `ADMIN_USERNAME` set: the modal would
    refuse to submit and surface "admin auth is disabled on the
    server" rather than retrying.
    """
    resp = client_admin_disabled.get(
        "/trader/admin/auth_check", auth=("admin", "secret"),
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "admin_disabled"


# ---- Not-found path --------------------------------------------------------


def test_enable_returns_404_when_version_missing(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, found=False)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/enable",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 404


def test_disable_returns_404_when_version_missing(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, found=False)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/disable",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 404


def test_approve_paper_returns_404_when_version_missing(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, found=False)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 404


# ---- Happy paths -----------------------------------------------------------


def test_enable_flips_flag_and_returns_state(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, enabled=False)
    captured = _stub_update_flag(monkeypatch)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/enable",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 200, resp.text
    assert captured["enabled"] is True
    assert captured["approved_for_paper"] is None


def test_disable_flips_flag_and_returns_state(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, enabled=True)
    captured = _stub_update_flag(monkeypatch)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/disable",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 200, resp.text
    assert captured["enabled"] is False
    assert captured["approved_for_paper"] is None


def test_approve_paper_succeeds_with_complete_backtest_metrics(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, approved_for_paper=False)
    captured = _stub_update_flag(monkeypatch)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 200, resp.text
    assert captured["approved_for_paper"] is True
    assert captured["enabled"] is None


# ---- approve_paper backtest_metrics validation ----------------------------


def test_approve_paper_rejects_empty_backtest_metrics(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_fetch_version(monkeypatch, backtest_metrics={})
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "backtest_metrics_incomplete"
    # Both subtrees missing means both report as "subtree missing".
    assert "walk_forward (subtree missing)" in detail["missing_keys"]
    assert "single_pass (subtree missing)" in detail["missing_keys"]


def test_approve_paper_rejects_missing_walk_forward(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _valid_backtest_metrics()
    del metrics["walk_forward"]
    _stub_fetch_version(monkeypatch, backtest_metrics=metrics)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 422
    assert "walk_forward (subtree missing)" in resp.json()["detail"]["missing_keys"]


def test_approve_paper_rejects_missing_single_pass_subkey(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _valid_backtest_metrics()
    del metrics["single_pass"]["win_rate"]
    _stub_fetch_version(monkeypatch, backtest_metrics=metrics)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 422
    assert "single_pass.win_rate" in resp.json()["detail"]["missing_keys"]
    # avg_return_per_trade is still present, so it's NOT in the missing list.
    assert "single_pass.avg_return_per_trade" not in resp.json()["detail"]["missing_keys"]


def test_approve_paper_rejects_missing_walk_forward_subkey(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _valid_backtest_metrics()
    del metrics["walk_forward"]["out_of_sample_trade_freq_per_week"]
    _stub_fetch_version(monkeypatch, backtest_metrics=metrics)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 422
    missing = resp.json()["detail"]["missing_keys"]
    assert "walk_forward.out_of_sample_trade_freq_per_week" in missing


def test_approve_paper_rejects_wrong_type_subtree(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If walk_forward is not a dict (e.g. accidentally a number),
    the validator treats it as the whole subtree being missing.
    """
    metrics = _valid_backtest_metrics()
    metrics["walk_forward"] = 42  # type: ignore[assignment]
    _stub_fetch_version(monkeypatch, backtest_metrics=metrics)
    resp = admin_client.post(
        f"/trader/strategies/{_VID}/approve_paper",
        auth=("admin", "secret"),
    )
    assert resp.status_code == 422
    assert "walk_forward (subtree missing)" in resp.json()["detail"]["missing_keys"]
