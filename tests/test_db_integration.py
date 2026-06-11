"""End-to-end DB tests against a real Postgres container.

Opt-in. Run with:
    uv run pytest -m integration

Marked `integration` because it pulls a postgres image and needs the
docker daemon. The unit slice of the migration runner lives in
workers/tests/test_db_migrations.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration

# testcontainers is a test-only dep; importorskip lets the file collect
# cleanly on machines without it installed.
testcontainers = pytest.importorskip("testcontainers.postgres")
from marketmind_shared.schemas import (  # noqa: E402
    ArticleContent,
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
    Transcript,
    TranscriptSegment,
    YouTubeContent,
)
from marketmind_workers.db import (  # noqa: E402
    apply_migrations,
    fetch_content,
    fetch_extraction_by_id,
    fetch_extraction_for_transcript,
    fetch_transcript_for_content,
    list_extractions,
    save_content,
    save_extraction,
    save_extraction_cost,
    save_extraction_with_cost,
    save_transcript,
)
from testcontainers.postgres import PostgresContainer  # noqa: E402


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: PostgresContainer) -> str:
    url = pg_container.get_connection_url()
    # testcontainers gives us a psycopg2-style URL; psycopg3 accepts
    # postgresql:// directly. Strip the "+psycopg2" if present.
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    # gen_random_uuid() needs pgcrypto. The migration files don't
    # CREATE EXTENSION (that lives in infra/postgres/init.sql, which
    # docker-compose mounts) — install the extension here so the migrations
    # apply cleanly inside the bare testcontainer.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


def test_migrations_idempotent(database_url: str) -> None:
    # First call already happened in the fixture. A second call must
    # be a no-op and not raise.
    second = apply_migrations(database_url)
    assert second == []


def test_save_and_fetch_youtube_content(database_url: str) -> None:
    yt = YouTubeContent(
        video_id="abc12345xyz",
        title="Test video",
        channel="Test channel",
        duration_seconds=120.0,
        audio_path=Path("/data/cache/audio/abc12345xyz.m4a"),
    )
    content_id = save_content(database_url, yt)
    fetched = fetch_content(database_url, content_id)
    assert fetched == yt


def test_save_and_fetch_article_content(database_url: str) -> None:
    art = ArticleContent(
        source_type="article",
        url="https://example.com/test-article",
        title="Title",
        author="Author",
        text="A" * 500,
    )
    content_id = save_content(database_url, art)
    fetched = fetch_content(database_url, content_id)
    assert fetched == art


def test_fetch_content_missing_returns_none(database_url: str) -> None:
    assert fetch_content(database_url, uuid4()) is None


def test_save_and_fetch_transcript(database_url: str) -> None:
    yt = YouTubeContent(
        video_id="ZZZ12345xyz",
        title="With transcript",
        channel="C",
        duration_seconds=60.0,
        audio_path=Path("/data/x.m4a"),
    )
    content_id = save_content(database_url, yt)
    transcript = Transcript(
        language="en",
        full_text="hello world",
        segments=[
            TranscriptSegment(start_seconds=0.0, end_seconds=2.0, text="hello world"),
        ],
        duration_seconds=2.0,
        model_name="small",
    )
    transcript_id = save_transcript(database_url, content_id, transcript)
    assert transcript_id is not None

    loaded = fetch_transcript_for_content(database_url, content_id)
    assert loaded == transcript


def test_save_and_fetch_extraction(database_url: str) -> None:
    yt = YouTubeContent(
        video_id="EXT12345xyz",
        title="With extraction",
        channel="C",
        duration_seconds=60.0,
        audio_path=Path("/data/x.m4a"),
    )
    content_id = save_content(database_url, yt)
    transcript_id = save_transcript(
        database_url,
        content_id,
        Transcript(
            language="en",
            full_text="x",
            segments=[],
            duration_seconds=1.0,
            model_name="small",
        ),
    )
    refusal = ExtractionResult(
        spec=None,
        report=ExtractionReport(
            verdict=ExtractionVerdict.NOT_EXTRACTABLE,
            overall_confidence=0.05,
            summary="x",
            extracted_rules=[],
            backtestable_parts=[],
            non_backtestable_parts=[],
            author_claims=[],
            reasoning="x",
            refusal_explanation="manually drawn levels",
        ),
    )
    extraction_id = save_extraction(database_url, transcript_id, refusal)
    save_extraction_cost(
        database_url,
        extraction_id,
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cached_tokens=0,
        cache_write_tokens=0,
        estimated_usd=0.0105,
    )

    fetched = fetch_extraction_by_id(database_url, extraction_id)
    assert fetched is not None
    fetched_transcript_id, fetched_result = fetched
    assert fetched_transcript_id == transcript_id
    assert fetched_result == refusal

    by_transcript = fetch_extraction_for_transcript(database_url, transcript_id)
    assert by_transcript is not None
    assert by_transcript[0] == extraction_id

    listed = list_extractions(database_url, limit=10)
    assert any(row[0] == extraction_id for row in listed)


def test_save_extraction_with_cost_atomic_refusal(database_url: str) -> None:
    """Regression for smoke-test-4: a refusal verdict (spec=None) must
    persist successfully via save_extraction_with_cost. Before the
    Phase 2.2 spec_json-nullable migration this raised
    NotNullViolation, and because the cost insert ran in a separate
    transaction the cost row was lost along with the strategy row.

    Asserts the atomic helper:
      - writes both rows
      - writes them in one transaction (verified indirectly by
        confirming both ids exist on a successful call)
    """
    yt = YouTubeContent(
        video_id="REF12345xyz",
        title="Refusal",
        channel="C",
        duration_seconds=10.0,
        audio_path=Path("/data/x.m4a"),
    )
    content_id = save_content(database_url, yt)
    transcript_id = save_transcript(
        database_url,
        content_id,
        Transcript(
            language="en",
            full_text="x",
            segments=[],
            duration_seconds=1.0,
            model_name="small",
        ),
    )
    refusal = ExtractionResult(
        spec=None,
        report=ExtractionReport(
            verdict=ExtractionVerdict.NOT_EXTRACTABLE,
            overall_confidence=0.05,
            summary="x",
            extracted_rules=[],
            backtestable_parts=[],
            non_backtestable_parts=[],
            author_claims=[],
            reasoning="x",
            refusal_explanation="hand-drawn levels",
        ),
    )

    extraction_id = save_extraction_with_cost(
        database_url,
        transcript_id,
        refusal,
        model="claude-sonnet-4-6",
        input_tokens=2200,
        output_tokens=1100,
        cached_tokens=18000,
        cache_write_tokens=0,
        estimated_usd=0.0226,
    )

    # Strategy row landed with spec_json = null
    fetched = fetch_extraction_by_id(database_url, extraction_id)
    assert fetched is not None
    _, fetched_result = fetched
    assert fetched_result.spec is None
    assert fetched_result.report.verdict is ExtractionVerdict.NOT_EXTRACTABLE

    # Cost row landed in the same transaction
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT input_tokens, output_tokens, cached_tokens, estimated_usd "
            "FROM extraction_costs WHERE extracted_strategy_id = %s",
            (str(extraction_id),),
        )
        row = cur.fetchone()
    assert row is not None
    assert row == (2200, 1100, 18000, 0.0226)


def test_cascade_delete_removes_transcript(database_url: str) -> None:
    yt = YouTubeContent(
        video_id="DEL12345xyz",
        title="Will be deleted",
        channel="C",
        duration_seconds=30.0,
        audio_path=Path("/data/x.m4a"),
    )
    content_id = save_content(database_url, yt)
    save_transcript(
        database_url,
        content_id,
        Transcript(
            language="en",
            full_text="x",
            segments=[],
            duration_seconds=1.0,
            model_name="small",
        ),
    )
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ingested_content WHERE id = %s", (str(content_id),))
        cur.execute(
            "SELECT count(*) FROM transcripts WHERE content_id = %s",
            (str(content_id),),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 0
