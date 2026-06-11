"""Spec provenance: where it came from, how confident we are, what notes
the LLM left for the user to review.

ExtractionNote is also the warning carrier returned alongside successful
validation (soft warnings like direction-consistency don't fail the spec
but should surface in the UI).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import _StrictModel


class ExtractionNote(_StrictModel):
    severity: Literal["info", "warning", "error"]
    field: str = Field(default="", max_length=200)
    message: str = Field(min_length=1, max_length=1000)
    # Confidence that this note correctly captures the source's intent for
    # the referenced field. Independent of Metadata.confidence.
    confidence: float = Field(ge=0.0, le=1.0)


class Metadata(_StrictModel):
    source_url: str = ""
    source_type: Literal["youtube", "article", "manual"] = "manual"
    extracted_by: str = ""
    extracted_at: datetime | None = None
    # Overall LLM confidence in the extraction. 1.0 = perfect capture;
    # 0.0 = we guessed at everything.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    extraction_notes: list[ExtractionNote] = Field(default_factory=list)

    @field_validator("extracted_at")
    @classmethod
    def _extracted_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        """Require a timezone-aware UTC datetime (or None).

        Naive datetimes get rejected because their meaning depends on the
        producer's local tz — extraction comes from a worker process whose
        timezone is non-deterministic. Non-UTC tz-aware datetimes get
        rejected because every other timestamp in the system (RQ
        enqueued_at, logs, backtest equity-curve bars) is UTC; mixing
        timezones invites bugs.
        """
        if value is None:
            return value
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise PydanticCustomError(
                "metadata_extracted_at_must_be_utc",
                "metadata.extracted_at must be timezone-aware UTC; got naive datetime",
            )
        offset = value.utcoffset()
        if offset != timedelta(0):
            raise PydanticCustomError(
                "metadata_extracted_at_must_be_utc",
                "metadata.extracted_at must be UTC (offset 0); got offset {offset}",
                {"offset": str(offset)},
            )
        # Normalize to UTC tzinfo so equality and serialization are stable.
        return value.astimezone(UTC)


__all__ = ["ExtractionNote", "Metadata"]
