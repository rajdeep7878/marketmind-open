"""Strategy extraction endpoints.

  POST /content/{content_id}/extract -> enqueue an extract_strategy job
                                         (idempotent: if an extraction
                                         already exists for the
                                         transcript, return its id
                                         without re-enqueueing)
  GET  /strategies/{strategy_id}     -> ExtractionResult (spec + report)
  GET  /strategies                   -> paginated list, newest first

A strategy here = one row in `extracted_strategies` = one LLM run's
output. The list endpoint joins through transcripts and
ingested_content to surface the source URL alongside each result so
the frontend doesn't need a second round-trip.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from marketmind_shared.schemas import (
    ExtractionResult,
    JobKind,
)
from pydantic import BaseModel, ConfigDict, Field

from marketmind_api.deps import DatabaseUrlDep, QueueDep
from marketmind_api.repo import (
    fetch_content,
    fetch_extraction_by_id,
    fetch_extraction_for_transcript,
    fetch_transcript_id_for_content,
    list_extractions,
)
from marketmind_api.routes.jobs import enqueue_job, job_to_view

# /strategies/* lives on a dedicated router because the prefix is
# different; /content/{id}/extract piggybacks on this router with a
# manual path to keep all extraction-related routes in one place.
router = APIRouter(tags=["strategies"])
log = structlog.get_logger(__name__)


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractionStartedResponse(_StrictResponse):
    """Returned from POST /content/{id}/extract.

    Wraps a JobView with an extra `from_cache` flag so the frontend
    knows whether to poll or immediately fetch the strategy.
    """

    job_id: str
    from_cache: bool
    extraction_id: str | None = None


class StrategySummary(_StrictResponse):
    """One row in the GET /strategies list view."""

    extraction_id: UUID
    source_url: str = Field(default="", max_length=2048)
    created_at: datetime
    result: ExtractionResult


class StrategyListResponse(_StrictResponse):
    items: list[StrategySummary]
    limit: int
    offset: int


# ---- POST /content/{content_id}/extract ------------------------------------


@router.post(
    "/content/{content_id}/extract",
    response_model=ExtractionStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_extraction(
    content_id: UUID,
    queue: QueueDep,
    database_url: DatabaseUrlDep,
) -> ExtractionStartedResponse:
    content = fetch_content(database_url, content_id)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"content {content_id} not found",
        )

    transcript_id = fetch_transcript_id_for_content(database_url, content_id)
    if transcript_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no transcript for content {content_id}; POST /content/{{id}}/transcribe first"
            ),
        )

    # Idempotent: if we already extracted for this transcript, surface the
    # existing strategy id and return a synthetic "finished" JobView-like
    # response without enqueuing a duplicate.
    existing = fetch_extraction_for_transcript(database_url, transcript_id)
    if existing is not None:
        extraction_id, _result = existing
        log.info(
            "extract_idempotent_hit",
            content_id=str(content_id),
            transcript_id=str(transcript_id),
            extraction_id=str(extraction_id),
        )
        return ExtractionStartedResponse(
            job_id="",
            from_cache=True,
            extraction_id=str(extraction_id),
        )

    rq_job = enqueue_job(
        queue,
        JobKind.EXTRACT_STRATEGY,
        {"transcript_id": str(transcript_id)},
        # Single Anthropic call with prompt-caching. The slow path is
        # the model's tool-use turnaround on a long transcript; long
        # articles with the 16k-token output ceiling can take 3-4
        # minutes (Sonnet 4.6 generates ~60-80 tokens/sec). The
        # in-extract `MAX_WALL_CLOCK_SECONDS` guard is 240s; this RQ
        # timeout must stay above that so the worker's clean error
        # path fires before RQ kills the job. 360s = 240s budget +
        # 120s margin for pre-flight + persistence + queue overhead.
        # See docs/operations/extraction-wall-clock-budget.md.
        job_timeout=360,
    )
    log.info(
        "extract_enqueued",
        content_id=str(content_id),
        transcript_id=str(transcript_id),
        job_id=rq_job.id,
    )
    # Return job_id so callers can poll GET /jobs/{id}. We deliberately
    # use the same JobView shape for the poll endpoint; this wrapper
    # adds the cache flag and the (eventual) extraction_id.
    _job_view = job_to_view(rq_job, JobKind.EXTRACT_STRATEGY)
    return ExtractionStartedResponse(
        job_id=rq_job.id,
        from_cache=False,
        extraction_id=None,
    )


# ---- GET /strategies/{strategy_id} ----------------------------------------


@router.get(
    "/strategies/{strategy_id}",
    response_model=ExtractionResult,
)
def get_strategy(strategy_id: UUID, database_url: DatabaseUrlDep) -> ExtractionResult:
    result = fetch_extraction_by_id(database_url, strategy_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"strategy {strategy_id} not found",
        )
    return result


# ---- GET /strategies (paginated list) -------------------------------------


@router.get(
    "/strategies",
    response_model=StrategyListResponse,
)
def list_strategies(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StrategyListResponse:
    rows = list_extractions(database_url, limit=limit, offset=offset)
    items = [StrategySummary.model_validate(row) for row in rows]
    return StrategyListResponse(items=items, limit=limit, offset=offset)


__all__ = ["StrategyListResponse", "StrategySummary", "router"]
