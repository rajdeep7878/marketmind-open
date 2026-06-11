"""Tests for the env-driven CORS allow-list.

Covers both the pure parser (`Settings.cors_origins_list`) and the
end-to-end preflight behaviour — the latter via TestClient with a
custom origin set per test so we don't have to spin up uvicorn just
to verify the policy.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from marketmind_api.config import Settings, get_settings
from marketmind_api.main import create_app

# ---- parser ---------------------------------------------------------------


def test_cors_origins_list_default_includes_localhost() -> None:
    s = Settings(
        database_url="postgresql://t:t@h:5432/d",  # type: ignore[arg-type]
        redis_url="redis://h:6379/0",  # type: ignore[arg-type]
    )
    assert "http://localhost:3000" in s.cors_origins_list()
    assert "http://localhost:8000" in s.cors_origins_list()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", []),
        ("  ", []),
        ("https://a.example", ["https://a.example"]),
        ("https://a.example,https://b.example", ["https://a.example", "https://b.example"]),
        # Spaces around entries get trimmed; empty trailing entry dropped.
        (" https://a.example , https://b.example ,", ["https://a.example", "https://b.example"]),
    ],
)
def test_cors_origins_list_parsing(raw: str, expected: list[str]) -> None:
    s = Settings(
        database_url="postgresql://t:t@h:5432/d",  # type: ignore[arg-type]
        redis_url="redis://h:6379/0",  # type: ignore[arg-type]
        cors_origins=raw,
    )
    assert s.cors_origins_list() == expected


# ---- end-to-end preflight ------------------------------------------------


def _fresh_client(cors: str) -> TestClient:
    """Build a fresh FastAPI app with the given CORS_ORIGINS env value.

    `create_app()` reads `get_settings()` at construction time, so we
    clear the cache and stamp the env var before each build.
    """
    os.environ["CORS_ORIGINS"] = cors
    get_settings.cache_clear()
    return TestClient(create_app())


def test_preflight_allowed_for_listed_origin() -> None:
    client = _fresh_client("http://localhost:3000")
    resp = client.options(
        "/content/ingest",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    # POST is in the explicit allow list and surfaces back to the client.
    assert "POST" in resp.headers.get("access-control-allow-methods", "")


def test_preflight_blocked_for_unlisted_origin() -> None:
    client = _fresh_client("https://example.com")
    resp = client.options(
        "/content/ingest",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    # Starlette's CORSMiddleware returns 400 for disallowed origins.
    assert resp.status_code == 400
    # And critically, no ACAO header → the browser will refuse the
    # subsequent POST.
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_preflight_blocked_when_allow_list_empty() -> None:
    client = _fresh_client("")
    resp = client.options(
        "/content/ingest",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 400
