"""Tests for the content/transcript schemas.

Covers:
  - Construction of each IngestedContent variant
  - Discriminated-union round-trip (each variant resolves correctly)
  - JSON serialization round-trip
  - Rejection of naive datetimes for every datetime field
  - Validation rules (length, language, etc.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from marketmind_shared.schemas import (
    ArticleContent,
    ExtractionInput,
    RawTextContent,
    Transcript,
    TranscriptSegment,
    YouTubeContent,
)
from marketmind_shared.schemas.content import IngestedContent
from pydantic import TypeAdapter, ValidationError

_INGESTED_ADAPTER = TypeAdapter(IngestedContent)


def _make_transcript(**overrides: object) -> Transcript:
    base: dict[str, object] = {
        "language": "en",
        "full_text": "hello world",
        "segments": [
            TranscriptSegment(start_seconds=0.0, end_seconds=1.5, text="hello world"),
        ],
        "duration_seconds": 1.5,
        "model_name": "small",
    }
    base.update(overrides)
    return Transcript(**base)  # type: ignore[arg-type]


# ---- YouTubeContent ----------------------------------------------------------


def test_youtube_content_construct_minimal() -> None:
    yt = YouTubeContent(
        video_id="dQw4w9WgXcQ",
        title="Title",
        channel="Channel",
        duration_seconds=212.0,
        audio_path=Path("/data/cache/audio/dQw4w9WgXcQ.m4a"),
    )
    assert yt.source_type == "youtube"
    assert yt.video_id == "dQw4w9WgXcQ"
    assert yt.uploaded_at is None


def test_youtube_content_with_uploaded_at_utc() -> None:
    when = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    yt = YouTubeContent(
        video_id="abc",
        title="Title",
        channel="Channel",
        duration_seconds=10.0,
        audio_path=Path("/x.m4a"),
        uploaded_at=when,
    )
    assert yt.uploaded_at == when


def test_youtube_content_rejects_naive_uploaded_at() -> None:
    with pytest.raises(ValidationError) as ei:
        YouTubeContent(
            video_id="abc",
            title="Title",
            channel="Channel",
            duration_seconds=10.0,
            audio_path=Path("/x.m4a"),
            uploaded_at=datetime(2025, 1, 1, 12, 0),  # noqa: DTZ001  # naive is the point
        )
    assert any("must be timezone-aware UTC" in str(e["msg"]) for e in ei.value.errors())


def test_youtube_content_rejects_non_utc_uploaded_at() -> None:
    plus_one = timezone(timedelta(hours=1))
    with pytest.raises(ValidationError) as ei:
        YouTubeContent(
            video_id="abc",
            title="Title",
            channel="Channel",
            duration_seconds=10.0,
            audio_path=Path("/x.m4a"),
            uploaded_at=datetime(2025, 1, 1, 12, 0, tzinfo=plus_one),
        )
    assert any("offset 0" in str(e["msg"]) for e in ei.value.errors())


def test_youtube_content_rejects_too_long_video() -> None:
    with pytest.raises(ValidationError):
        YouTubeContent(
            video_id="abc",
            title="Title",
            channel="Channel",
            duration_seconds=4 * 3600 + 1,  # > 4h
            audio_path=Path("/x.m4a"),
        )


def test_youtube_content_rejects_zero_duration() -> None:
    with pytest.raises(ValidationError):
        YouTubeContent(
            video_id="abc",
            title="Title",
            channel="Channel",
            duration_seconds=0.0,
            audio_path=Path("/x.m4a"),
        )


# ---- ArticleContent ----------------------------------------------------------


def test_article_content_construct() -> None:
    art = ArticleContent(
        source_type="article",
        url="https://example.com/post",
        title="Post Title",
        text="body text here",
    )
    assert art.author is None
    assert art.published_at is None


def test_article_content_rejects_naive_published_at() -> None:
    with pytest.raises(ValidationError):
        ArticleContent(
            url="https://example.com",
            title="t",
            text="body",
            published_at=datetime(2025, 1, 1, 12, 0),  # noqa: DTZ001  # naive is the point
        )


def test_article_content_rejects_non_utc_published_at() -> None:
    minus_five = timezone(timedelta(hours=-5))
    with pytest.raises(ValidationError):
        ArticleContent(
            url="https://example.com",
            title="t",
            text="body",
            published_at=datetime(2025, 1, 1, 12, 0, tzinfo=minus_five),
        )


# ---- RawTextContent ----------------------------------------------------------


def test_raw_text_content_construct() -> None:
    rt = RawTextContent(text="some strategy notes")
    assert rt.source_type == "raw_text"
    assert rt.label is None


def test_raw_text_content_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        RawTextContent(text="")


# ---- IngestedContent (discriminated union) ----------------------------------


@pytest.mark.parametrize(
    "payload, expected_cls",
    [
        (
            {
                "source_type": "youtube",
                "video_id": "abc",
                "title": "Title",
                "channel": "C",
                "duration_seconds": 10.0,
                "audio_path": "/x.m4a",
            },
            YouTubeContent,
        ),
        (
            {
                "source_type": "article",
                "url": "https://x.com",
                "title": "Title",
                "text": "body",
            },
            ArticleContent,
        ),
        (
            {"source_type": "raw_text", "text": "x"},
            RawTextContent,
        ),
    ],
)
def test_ingested_content_discriminates(
    payload: dict[str, object], expected_cls: type[object]
) -> None:
    obj = _INGESTED_ADAPTER.validate_python(payload)
    assert isinstance(obj, expected_cls)


def test_ingested_content_rejects_unknown_source_type() -> None:
    with pytest.raises(ValidationError):
        _INGESTED_ADAPTER.validate_python({"source_type": "twitter", "text": "x"})


def test_ingested_content_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        _INGESTED_ADAPTER.validate_python({"source_type": "raw_text", "text": "x", "extra": True})


def test_ingested_content_json_round_trip() -> None:
    yt = YouTubeContent(
        video_id="abc",
        title="Title",
        channel="C",
        duration_seconds=10.0,
        audio_path=Path("/x.m4a"),
        uploaded_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    blob = yt.model_dump_json()
    restored = YouTubeContent.model_validate_json(blob)
    assert restored == yt


# ---- Transcript -------------------------------------------------------------


def test_transcript_construct() -> None:
    tr = _make_transcript()
    assert tr.language == "en"
    assert len(tr.segments) == 1


def test_transcript_language_normalized_lowercase() -> None:
    tr = _make_transcript(language="EN")
    assert tr.language == "en"


def test_transcript_rejects_non_alpha_language() -> None:
    with pytest.raises(ValidationError):
        _make_transcript(language="e1")


def test_transcript_rejects_bad_length_language() -> None:
    with pytest.raises(ValidationError):
        _make_transcript(language="eng")
    with pytest.raises(ValidationError):
        _make_transcript(language="e")


def test_transcript_json_round_trip() -> None:
    tr = _make_transcript()
    blob = tr.model_dump_json()
    restored = Transcript.model_validate_json(blob)
    assert restored == tr


def test_transcript_segment_rejects_empty_text() -> None:
    with pytest.raises(ValidationError):
        TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="")


# ---- ExtractionInput --------------------------------------------------------


def test_extraction_input_construct() -> None:
    ei = ExtractionInput(
        source_url="https://example.com",
        source_type="article",
        transcript=_make_transcript(),
    )
    assert ei.source_type == "article"
    assert ei.transcript.full_text == "hello world"


def test_extraction_input_json_round_trip() -> None:
    ei = ExtractionInput(source_type="raw_text", transcript=_make_transcript())
    blob = ei.model_dump_json()
    restored = ExtractionInput.model_validate_json(blob)
    assert restored == ei


def test_extraction_input_rejects_unknown_source_type() -> None:
    with pytest.raises(ValidationError):
        ExtractionInput(
            source_type="twitter",  # type: ignore[arg-type]
            transcript=_make_transcript(),
        )
