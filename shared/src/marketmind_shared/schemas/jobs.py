"""Job submission and view models for the Phase 0 dummy-job flow.

These are intentionally minimal. Phase 2 will extend `JobKind` with real
ingestion kinds (transcribe, extract, etc.) and add per-kind payload models.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class JobKind(StrEnum):
    DUMMY = "dummy"
    # Phase 2.1 additions. Existing rows persisted under `DUMMY` keep
    # parsing because they use the string value, not the member name —
    # adding new values is backward-compatible.
    INGEST_YOUTUBE = "ingest_youtube"
    INGEST_ARTICLE = "ingest_article"
    INGEST_RAW_TEXT = "ingest_raw_text"
    TRANSCRIBE = "transcribe"
    EXTRACT_STRATEGY = "extract_strategy"
    # Phase 3.2.
    BACKTEST = "backtest"
    # Phase 4.
    OVERFITTING_ANALYSIS = "overfitting_analysis"


class JobStatus(StrEnum):
    # Mirrors RQ's job states. We expose a strict subset so the frontend
    # only has to reason about five values.
    QUEUED = "queued"
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    DEFERRED = "deferred"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class DummyJobPayload(_StrictModel):
    message: str = Field(min_length=1, max_length=500)


class JobSubmission(_StrictModel):
    """Request body for POST /jobs.

    Phase 0 only knows about `dummy`; the discriminator pattern is here from
    day one so adding new kinds in Phase 2 is a non-breaking change.
    """

    kind: JobKind
    payload: DummyJobPayload


class JobView(_StrictModel):
    """Response body for GET /jobs/{id} and POST /jobs."""

    id: UUID
    kind: JobKind
    status: JobStatus
    result: dict[str, Any] | None = None
    error: str | None = None
    enqueued_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
