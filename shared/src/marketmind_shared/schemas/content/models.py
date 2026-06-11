"""Pydantic models for ingested content + transcripts.

`IngestedContent` is a discriminated union by `source_type`:

  - "youtube"   -> YouTubeContent
  - "article"   -> ArticleContent
  - "raw_text"  -> RawTextContent

The shared `_StrictModel` base mirrors the strategy-spec conventions:
extra="forbid" + frozen=True, so typos and accidental mutations both
raise at validation time.

Datetimes: every datetime field is validated to be timezone-aware UTC.
The producer is a worker process whose host timezone is non-deterministic,
so naive datetimes are rejected outright. Non-UTC offsets are normalized
or rejected based on context — for *_at fields we accept any tz-aware
value and convert to UTC; for fields the spec calls "UTC", we additionally
reject non-zero offsets. We follow Metadata.extracted_at: reject anything
that isn't already UTC, so producers stay explicit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import _StrictModel

# Plain alias (not PEP 695 `type` keyword) because pydantic treats those
# differently from imported types in TypeAdapter contexts — sticking with
# TypeAlias keeps Pydantic v2's discriminated-union machinery happy.
ContentSourceType: TypeAlias = Literal["youtube", "article", "raw_text"]  # noqa: UP040


def _require_utc(field_name: str, value: datetime | None) -> datetime | None:
    """Shared validator body for tz-aware UTC datetime fields.

    Mirrors Metadata.extracted_at in metadata.py — keep these in lockstep.
    """
    if value is None:
        return value
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise PydanticCustomError(
            "datetime_must_be_utc",
            "{field} must be timezone-aware UTC; got naive datetime",
            {"field": field_name},
        )
    offset = value.utcoffset()
    if offset != timedelta(0):
        raise PydanticCustomError(
            "datetime_must_be_utc",
            "{field} must be UTC (offset 0); got offset {offset}",
            {"field": field_name, "offset": str(offset)},
        )
    return value.astimezone(UTC)


class YouTubeContent(_StrictModel):
    """Metadata + on-disk audio path for a YouTube video.

    audio_path is required: ingest_youtube only returns this model after
    the audio has been written to disk (cache hit or fresh download).
    """

    source_type: Literal["youtube"] = "youtube"
    video_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=500)
    channel: str = Field(min_length=1, max_length=200)
    duration_seconds: float = Field(gt=0.0, le=4 * 3600.0)
    audio_path: Path
    uploaded_at: datetime | None = None

    @field_validator("uploaded_at")
    @classmethod
    def _uploaded_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc("uploaded_at", value)


class ArticleContent(_StrictModel):
    source_type: Literal["article"] = "article"
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    author: str | None = Field(default=None, max_length=200)
    published_at: datetime | None = None
    # Extracted body, plain text. 200-char floor enforced at the service
    # boundary (ContentTooShortError) — keep the schema permissive so
    # historical edge-cases (very-short articles loaded by hand) can be
    # validated against the model directly.
    text: str = Field(min_length=1)

    @field_validator("published_at")
    @classmethod
    def _published_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc("published_at", value)


class RawTextContent(_StrictModel):
    source_type: Literal["raw_text"] = "raw_text"
    text: str = Field(min_length=1)
    label: str | None = Field(default=None, max_length=200)


# Discriminated union — Pydantic uses source_type to pick the variant.
# The Annotated form is the v2 idiom; we deliberately don't call
# model_rebuild() here because the variants don't forward-reference
# IngestedContent (unlike the strategy_spec.Condition tree).
IngestedContent: TypeAlias = Annotated[  # noqa: UP040
    YouTubeContent | ArticleContent | RawTextContent,
    Field(discriminator="source_type"),
]


class TranscriptSegment(_StrictModel):
    """A single timed segment from the speech-to-text output."""

    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(ge=0.0)
    text: str = Field(min_length=1, max_length=10_000)


class Transcript(_StrictModel):
    """Speech-to-text output, plus enough provenance to be reproducible.

    `language` is ISO 639-1 (e.g. "en"). `model_name` is the
    faster-whisper model identifier we used so we can detect when a
    cached transcript was produced by an older model.
    """

    language: str = Field(min_length=2, max_length=2)
    full_text: str = Field(min_length=0)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    duration_seconds: float = Field(gt=0.0)
    model_name: str = Field(min_length=1, max_length=200)

    @field_validator("language")
    @classmethod
    def _language_lowercase(cls, value: str) -> str:
        if not value.isalpha():
            raise PydanticCustomError(
                "language_must_be_alpha",
                "language must be 2 alphabetic chars (ISO 639-1); got {value!r}",
                {"value": value},
            )
        return value.lower()


class ExtractionInput(_StrictModel):
    """Input bundle for the (Phase 2.2) LLM extraction service.

    Carries the transcript text plus the minimal provenance the
    extraction prompt needs in order to populate Metadata.source_url and
    Metadata.source_type. Defined now so the boundary stays stable and
    workers/api can wire it up in 2.1 without changing the schema in 2.2.
    """

    source_url: str = Field(default="", max_length=2048)
    source_type: ContentSourceType
    transcript: Transcript


__all__ = [
    "ArticleContent",
    "ContentSourceType",
    "ExtractionInput",
    "IngestedContent",
    "RawTextContent",
    "Transcript",
    "TranscriptSegment",
    "YouTubeContent",
]
