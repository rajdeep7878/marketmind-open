"""Tests for the /content/* endpoints.

Strategy: TestClient + fakeredis + SimpleWorker for the queue path,
and monkeypatched DB read helpers for the GET endpoints. We do NOT
exercise the real Postgres — that's covered by the opt-in
@integration tests in tests/test_db_integration.py.

Worker-side service calls (yt-dlp, faster-whisper, trafilatura) are
mocked at the job module level so the SimpleWorker run is hermetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_api.routes import content as content_routes
from marketmind_shared.schemas import (
    ArticleContent,
    RawTextContent,
    Transcript,
    TranscriptSegment,
    YouTubeContent,
)
from rq import Queue, SimpleWorker

# ---- kind detection --------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected_kind",
    [
        ({"url": "https://youtu.be/dQw4w9WgXcQ"}, "youtube"),
        ({"url": "https://www.youtube.com/watch?v=x"}, "youtube"),
        ({"url": "https://example.com/blog/post"}, "article"),
        ({"text": "raw notes"}, "raw_text"),
        ({"text": "x", "label": "manual"}, "raw_text"),
        # Explicit kind overrides detection
        ({"url": "https://example.com", "kind": "article"}, "article"),
        ({"url": "https://example.com", "kind": "youtube"}, "youtube"),
    ],
)
def test_detect_kind(body: dict[str, Any], expected_kind: str) -> None:
    req = content_routes.IngestRequest.model_validate(body)
    assert content_routes.detect_kind(req) == expected_kind


def test_ingest_request_rejects_both_url_and_text() -> None:
    with pytest.raises(ValueError):
        content_routes.IngestRequest.model_validate({"url": "x", "text": "y"})


def test_ingest_request_rejects_neither_url_nor_text() -> None:
    with pytest.raises(ValueError):
        content_routes.IngestRequest.model_validate({})


def test_ingest_request_rejects_text_with_wrong_kind() -> None:
    with pytest.raises(ValueError):
        content_routes.IngestRequest.model_validate({"text": "x", "kind": "article"})


# ---- POST /content/ingest --------------------------------------------------


def test_post_ingest_youtube_enqueues(client: TestClient, fake_redis: FakeRedis) -> None:
    resp = client.post("/content/ingest", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["kind"] == "ingest_youtube"
    assert body["status"] == "queued"
    UUID(body["id"])

    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1


def test_post_ingest_article_enqueues(client: TestClient, fake_redis: FakeRedis) -> None:
    resp = client.post("/content/ingest", json={"url": "https://example.com/post"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "ingest_article"
    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1


def test_post_ingest_raw_text_enqueues(client: TestClient, fake_redis: FakeRedis) -> None:
    resp = client.post(
        "/content/ingest",
        json={"text": "buy low sell high", "label": "manual entry"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "ingest_raw_text"


def test_post_ingest_rejects_missing_body(client: TestClient) -> None:
    resp = client.post("/content/ingest", json={})
    assert resp.status_code == 422


def test_post_ingest_rejects_both_url_and_text(client: TestClient) -> None:
    resp = client.post("/content/ingest", json={"url": "x", "text": "y"})
    assert resp.status_code == 422


# ---- end-to-end via SimpleWorker -------------------------------------------


def test_ingest_youtube_end_to_end(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit -> SimpleWorker runs the mocked job -> GET /jobs/{id} sees result."""
    yt = YouTubeContent(
        video_id="abcdefghijk",
        title="Title",
        channel="C",
        duration_seconds=10.0,
        audio_path=Path("/data/x.m4a"),
    )

    # Mock the worker-side service and DB write so the SimpleWorker run is hermetic.
    from marketmind_workers.jobs import ingest_youtube as ingest_youtube_job

    monkeypatch.setattr(ingest_youtube_job, "ingest_youtube", lambda url, data_dir: yt)
    monkeypatch.setattr(
        ingest_youtube_job,
        "save_content",
        lambda _url, _content: UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    )

    submit = client.post("/content/ingest", json={"url": "https://youtu.be/abcdefghijk"})
    job_id = submit.json()["id"]

    queue = Queue(name="default", connection=fake_redis)
    SimpleWorker([queue], connection=fake_redis).work(burst=True, with_scheduler=False)

    fetched = client.get(f"/jobs/{job_id}")
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["status"] == "finished"
    assert body["kind"] == "ingest_youtube"
    assert body["result"]["content_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert body["result"]["video_id"] == "abcdefghijk"


def test_ingest_article_end_to_end(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    art = ArticleContent(url="https://example.com", title="Title", text="A" * 500)
    from marketmind_workers.jobs import ingest_article as ingest_article_job

    monkeypatch.setattr(ingest_article_job, "ingest_article", lambda url, data_dir: art)
    monkeypatch.setattr(
        ingest_article_job,
        "save_content",
        lambda _url, _content: UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    )
    # Article ingest also writes a synthetic transcript so the extract
    # pipeline (keyed on transcript_id) can find it — mock that DB call.
    monkeypatch.setattr(
        ingest_article_job,
        "save_transcript",
        lambda _url, _cid, _tr: UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"),
    )

    submit = client.post("/content/ingest", json={"url": "https://example.com/blog/post"})
    job_id = submit.json()["id"]

    queue = Queue(name="default", connection=fake_redis)
    SimpleWorker([queue], connection=fake_redis).work(burst=True, with_scheduler=False)

    fetched = client.get(f"/jobs/{job_id}").json()
    assert fetched["status"] == "finished"
    assert fetched["kind"] == "ingest_article"
    assert fetched["result"]["content_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_ingest_raw_text_end_to_end(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marketmind_workers.jobs import ingest_raw_text as ingest_raw_text_job

    monkeypatch.setattr(
        ingest_raw_text_job,
        "save_content",
        lambda _url, _content: UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
    )
    # Raw-text ingest also writes a synthetic transcript (mirroring the
    # article path) so a subsequent /content/{id}/extract doesn't 404.
    monkeypatch.setattr(
        ingest_raw_text_job,
        "save_transcript",
        lambda _url, _cid, _tr: UUID("cccccccc-cccc-cccc-cccc-ccccccccccc1"),
    )

    submit = client.post(
        "/content/ingest",
        json={"text": "buy low sell high", "label": "manual"},
    )
    job_id = submit.json()["id"]

    queue = Queue(name="default", connection=fake_redis)
    SimpleWorker([queue], connection=fake_redis).work(burst=True, with_scheduler=False)

    fetched = client.get(f"/jobs/{job_id}").json()
    assert fetched["status"] == "finished", fetched
    assert fetched["kind"] == "ingest_raw_text"
    assert fetched["result"]["content_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"


# ---- GET /content/{id} -----------------------------------------------------


def test_get_content_returns_youtube(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yt = YouTubeContent(
        video_id="abcdefghijk",
        title="Title",
        channel="C",
        duration_seconds=10.0,
        audio_path=Path("/data/x.m4a"),
    )

    def fake_fetch(_url: str, _cid: UUID) -> YouTubeContent:
        return yt

    monkeypatch.setattr(content_routes, "fetch_content", fake_fetch)

    cid = uuid4()
    resp = client.get(f"/content/{cid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["video_id"] == "abcdefghijk"


def test_get_content_returns_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_routes, "fetch_content", lambda _u, _c: None)
    resp = client.get(f"/content/{uuid4()}")
    assert resp.status_code == 404


# ---- POST /content/{id}/transcribe ----------------------------------------


def test_post_transcribe_enqueues(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
) -> None:
    yt = YouTubeContent(
        video_id="abcdefghijk",
        title="t",
        channel="c",
        duration_seconds=10.0,
        audio_path=Path("/data/x.m4a"),
    )
    monkeypatch.setattr(content_routes, "fetch_content", lambda _u, _c: yt)

    cid = uuid4()
    resp = client.post(f"/content/{cid}/transcribe")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["kind"] == "transcribe"

    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1

    # The content->job link should be in Redis under our prefix.
    keys = list(fake_redis.scan_iter(match=f"marketmind:content_job:{cid}"))
    assert len(keys) == 1


def test_post_transcribe_rejects_missing_content(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_routes, "fetch_content", lambda _u, _c: None)
    resp = client.post(f"/content/{uuid4()}/transcribe")
    assert resp.status_code == 404


def test_post_transcribe_rejects_non_youtube_content(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = RawTextContent(text="not transcribable")
    monkeypatch.setattr(content_routes, "fetch_content", lambda _u, _c: raw)
    resp = client.post(f"/content/{uuid4()}/transcribe")
    assert resp.status_code == 400


# ---- GET /content/{id}/transcript ------------------------------------------


def test_get_transcript_returns_when_ready(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tr = Transcript(
        language="en",
        full_text="hello",
        segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="hello")],
        duration_seconds=1.0,
        model_name="small",
    )
    monkeypatch.setattr(content_routes, "fetch_transcript_for_content", lambda _u, _c: tr)

    resp = client.get(f"/content/{uuid4()}/transcript")
    assert resp.status_code == 200, resp.text
    assert resp.json()["full_text"] == "hello"


def test_get_transcript_returns_404_when_not_ready(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(content_routes, "fetch_transcript_for_content", lambda _u, _c: None)
    resp = client.get(f"/content/{uuid4()}/transcript")
    assert resp.status_code == 404
