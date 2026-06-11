"""Unit tests for the extract_strategy RQ job callable.

The DB layer and the LLM service are both mocked; the test verifies
the job's glue logic: id lookups, idempotent cache hit, and the
persistence calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from marketmind_shared.schemas import (
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
    Transcript,
    TranscriptSegment,
    YouTubeContent,
)
from marketmind_workers.jobs import extract_strategy as job
from marketmind_workers.services.extract import UsageStats


def _make_transcript() -> Transcript:
    return Transcript(
        language="en",
        full_text="hello",
        segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="hello")],
        duration_seconds=1.0,
        model_name="small",
    )


def _refusal_result() -> ExtractionResult:
    return ExtractionResult(
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
            refusal_explanation="x",
        ),
    )


def _usage() -> UsageStats:
    return UsageStats(
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
        cache_write_tokens=0,
        estimated_usd=0.001,
    )


def test_extract_strategy_job_persists_and_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript_id = uuid4()
    content_id = uuid4()
    extraction_id = uuid4()

    monkeypatch.setattr(job, "fetch_extraction_for_transcript", lambda _url, _t: None)
    monkeypatch.setattr(job, "fetch_transcript_by_id", lambda _url, _t: _make_transcript())
    monkeypatch.setattr(job, "fetch_content_id_for_transcript", lambda _url, _t: content_id)
    yt = YouTubeContent(
        video_id="abcdefghijk",
        title="Title",
        channel="C",
        duration_seconds=10.0,
        audio_path=Path("/data/x.m4a"),
    )
    monkeypatch.setattr(job, "fetch_content", lambda _url, _c: yt)
    monkeypatch.setattr(
        job,
        "extract_strategy",
        lambda _t, _s: (_refusal_result(), _usage()),
    )

    combined_calls: list[Any] = []

    def fake_save(_url: str, tr_id: UUID, _result: Any, **kwargs: Any) -> UUID:
        # Records the atomic call so we can assert the cost kwargs
        # were threaded through correctly.
        combined_calls.append({"transcript_id": tr_id, **kwargs})
        return extraction_id

    monkeypatch.setattr(job, "save_extraction_with_cost", fake_save)

    result = job.run(str(transcript_id))

    assert result["extraction_id"] == str(extraction_id)
    assert result["verdict"] == "not_extractable"
    assert result["from_cache"] is False
    assert len(combined_calls) == 1
    assert combined_calls[0]["transcript_id"] == transcript_id
    # Cost kwargs from the usage record reach the persistence call.
    assert combined_calls[0]["model"] == "claude-sonnet-4-6"
    assert combined_calls[0]["input_tokens"] == 100
    assert combined_calls[0]["estimated_usd"] == 0.001


def test_extract_strategy_job_idempotent_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript_id = uuid4()
    extraction_id = uuid4()
    cached_result = _refusal_result()

    monkeypatch.setattr(
        job,
        "fetch_extraction_for_transcript",
        lambda _url, _t: (extraction_id, cached_result),
    )

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("extract_strategy should not be called on cache hit")

    monkeypatch.setattr(job, "extract_strategy", boom)
    monkeypatch.setattr(job, "save_extraction_with_cost", boom)

    result = job.run(str(transcript_id))
    assert result["extraction_id"] == str(extraction_id)
    assert result["from_cache"] is True


def test_extract_strategy_job_missing_transcript_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "fetch_extraction_for_transcript", lambda _url, _t: None)
    monkeypatch.setattr(job, "fetch_transcript_by_id", lambda _url, _t: None)

    with pytest.raises(ValueError, match="no transcript row"):
        job.run(str(uuid4()))


def test_extract_strategy_job_missing_content_link_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "fetch_extraction_for_transcript", lambda _url, _t: None)
    monkeypatch.setattr(job, "fetch_transcript_by_id", lambda _url, _t: _make_transcript())
    monkeypatch.setattr(job, "fetch_content_id_for_transcript", lambda _url, _t: None)

    with pytest.raises(ValueError, match="no linked ingested_content"):
        job.run(str(uuid4()))
