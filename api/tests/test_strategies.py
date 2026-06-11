"""Tests for the strategy extraction endpoints.

DB reads are mocked at the route module level; queue interactions use
fakeredis as elsewhere. The full Pg integration is covered by
tests/test_db_integration.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_api.routes import strategies as strategies_routes
from marketmind_shared.schemas import (
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
    YouTubeContent,
)
from rq import Queue


def _refusal_result() -> ExtractionResult:
    return ExtractionResult(
        spec=None,
        report=ExtractionReport(
            verdict=ExtractionVerdict.NOT_EXTRACTABLE,
            overall_confidence=0.05,
            summary="discretionary support and resistance",
            extracted_rules=[],
            backtestable_parts=[],
            non_backtestable_parts=["entry on hand-drawn levels"],
            author_claims=[],
            reasoning="hand-drawn levels are not mechanical",
            refusal_explanation="No algorithmic definition of support/resistance.",
        ),
    )


def _make_content() -> YouTubeContent:
    return YouTubeContent(
        video_id="abcdefghijk",
        title="t",
        channel="c",
        duration_seconds=10.0,
        audio_path=Path("/data/x.m4a"),
    )


# ---- POST /content/{id}/extract -------------------------------------------


def test_post_extract_enqueues_when_no_existing(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_id = uuid4()
    transcript_id = uuid4()

    monkeypatch.setattr(strategies_routes, "fetch_content", lambda _u, _c: _make_content())
    monkeypatch.setattr(
        strategies_routes,
        "fetch_transcript_id_for_content",
        lambda _u, _c: transcript_id,
    )
    monkeypatch.setattr(
        strategies_routes,
        "fetch_extraction_for_transcript",
        lambda _u, _t: None,
    )

    resp = client.post(f"/content/{content_id}/extract")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["from_cache"] is False
    assert body["job_id"]
    assert body["extraction_id"] is None

    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1


def test_post_extract_idempotent_when_existing(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_id = uuid4()
    transcript_id = uuid4()
    existing_id = uuid4()

    monkeypatch.setattr(strategies_routes, "fetch_content", lambda _u, _c: _make_content())
    monkeypatch.setattr(
        strategies_routes,
        "fetch_transcript_id_for_content",
        lambda _u, _c: transcript_id,
    )
    monkeypatch.setattr(
        strategies_routes,
        "fetch_extraction_for_transcript",
        lambda _u, _t: (existing_id, _refusal_result()),
    )

    resp = client.post(f"/content/{content_id}/extract")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["from_cache"] is True
    assert body["extraction_id"] == str(existing_id)
    assert body["job_id"] == ""

    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 0  # no job enqueued


def test_post_extract_404_when_no_content(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strategies_routes, "fetch_content", lambda _u, _c: None)
    resp = client.post(f"/content/{uuid4()}/extract")
    assert resp.status_code == 404


def test_post_extract_404_when_no_transcript(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strategies_routes, "fetch_content", lambda _u, _c: _make_content())
    monkeypatch.setattr(
        strategies_routes,
        "fetch_transcript_id_for_content",
        lambda _u, _c: None,
    )
    resp = client.post(f"/content/{uuid4()}/extract")
    assert resp.status_code == 404


# ---- GET /strategies/{strategy_id} -----------------------------------------


def test_get_strategy_happy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        strategies_routes,
        "fetch_extraction_by_id",
        lambda _u, _id: _refusal_result(),
    )
    resp = client.get(f"/strategies/{uuid4()}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spec"] is None
    assert body["report"]["verdict"] == "not_extractable"


def test_get_strategy_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strategies_routes, "fetch_extraction_by_id", lambda _u, _id: None)
    resp = client.get(f"/strategies/{uuid4()}")
    assert resp.status_code == 404


# ---- GET /strategies (list) ------------------------------------------------


def test_list_strategies_returns_items(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ext_id = uuid4()

    def fake_list(_url: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return [
            {
                "extraction_id": ext_id,
                "source_url": "https://example.com/v",
                "created_at": datetime(2026, 5, 15, tzinfo=UTC),
                "result": _refusal_result(),
            },
        ]

    monkeypatch.setattr(strategies_routes, "list_extractions", fake_list)

    resp = client.get("/strategies?limit=5&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    assert body["items"][0]["extraction_id"] == str(ext_id)
    assert body["items"][0]["source_url"] == "https://example.com/v"


def test_list_strategies_validates_query_bounds(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        strategies_routes,
        "list_extractions",
        lambda _u, *, limit, offset: [],
    )
    # limit out of range
    resp = client.get("/strategies?limit=0")
    assert resp.status_code == 422
    resp = client.get("/strategies?limit=101")
    assert resp.status_code == 422
    # offset negative
    resp = client.get("/strategies?offset=-1")
    assert resp.status_code == 422


def test_list_strategies_default_pagination(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    def fake_list(_url: str, *, limit: int, offset: int) -> list[dict[str, Any]]:
        captured["limit"] = limit
        captured["offset"] = offset
        return []

    monkeypatch.setattr(strategies_routes, "list_extractions", fake_list)
    resp = client.get("/strategies")
    assert resp.status_code == 200
    assert captured == {"limit": 20, "offset": 0}


# pyright-friendly: unused import guard
_ = UUID
