"""Content ingestion + transcription endpoints.

  POST /content/ingest                  -> enqueue an ingest_* job
  GET  /content/{content_id}            -> fetch persisted IngestedContent
  POST /content/{content_id}/transcribe -> enqueue a transcribe job
  GET  /content/{content_id}/transcript -> fetch the latest Transcript

Kind detection: when the request body specifies a URL but no `kind`,
the URL is matched against the YouTube hostname regex. Anything else
is treated as an article. Raw-text submissions skip URL detection
entirely.

Job <-> content linkage is recorded as a Redis hash with a short TTL.
That keeps the wire model simple (caller polls the job via GET
/jobs/{id}) while still letting tools that have only a content_id
discover the in-flight job that's processing it.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Body, HTTPException, Response, status
from marketmind_shared.schemas import (
    IngestedContent,
    JobKind,
    JobView,
    Transcript,
)
from marketmind_shared.urls import is_youtube_url
from pydantic import BaseModel, ConfigDict, Field, model_validator

from marketmind_api.deps import DatabaseUrlDep, QueueDep, RedisDep
from marketmind_api.rate_limit import IngestGuardDep
from marketmind_api.repo import fetch_content, fetch_transcript_for_content
from marketmind_api.routes.jobs import enqueue_job, job_to_view

router = APIRouter(prefix="/content", tags=["content"])
log = structlog.get_logger(__name__)

# Redis key prefix for job<->content links. Short TTL: this is only
# used by the API to recover the in-flight job for a content_id while
# polling. Once the job finishes, the persisted content row is the
# authoritative source.
_CONTENT_JOB_KEY_PREFIX: Final[str] = "marketmind:content_job"
_CONTENT_JOB_TTL_SECONDS: Final[int] = 3600


# ---- request models ---------------------------------------------------------


_IngestKindStr = Literal["youtube", "article", "raw_text"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IngestRequest(_StrictModel):
    """Request body for POST /content/ingest.

    Exactly one of (url, text) must be present:
      - url present → ingest_youtube / ingest_article (kind auto-detected
        or forced by `kind`)
      - text present → ingest_raw_text (requires kind="raw_text" or omitted)
    """

    url: str | None = Field(default=None, min_length=1, max_length=2048)
    text: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, max_length=200)
    kind: _IngestKindStr | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> IngestRequest:
        has_url = self.url is not None
        has_text = self.text is not None
        if has_url == has_text:
            raise ValueError(
                "exactly one of `url` or `text` must be provided",
            )
        if has_text and self.kind not in (None, "raw_text"):
            raise ValueError(
                f"text submissions must have kind='raw_text' or omitted; got {self.kind!r}",
            )
        return self


# ---- kind detection ---------------------------------------------------------


def detect_kind(req: IngestRequest) -> _IngestKindStr:
    """Resolve the IngestRequest to a concrete kind.

    Examples:
      detect_kind({"url": "https://youtu.be/abc"}) == "youtube"
      detect_kind({"url": "https://example.com/post"}) == "article"
      detect_kind({"text": "hi"}) == "raw_text"
      detect_kind({"url": "https://example.com", "kind": "article"}) == "article"
    """
    if req.kind is not None:
        return req.kind
    if req.text is not None:
        return "raw_text"
    assert req.url is not None  # model_validator guarantees this
    return "youtube" if is_youtube_url(req.url) else "article"


# ---- helpers ----------------------------------------------------------------


def _content_job_key(content_id: UUID) -> str:
    return f"{_CONTENT_JOB_KEY_PREFIX}:{content_id}"


def _record_content_job_link(redis: RedisDep, content_id: UUID, job_id: str) -> None:
    """Best-effort link of content_id -> in-flight job_id in Redis.

    Used by tools polling on content_id without a job_id; not relied on
    by any code path that *must* succeed. Short TTL means we don't
    accumulate dangling keys for old content rows.
    """
    redis.set(
        _content_job_key(content_id),
        job_id.encode("utf-8"),
        ex=_CONTENT_JOB_TTL_SECONDS,
    )


# ---- endpoints --------------------------------------------------------------


@router.post(
    "/ingest",
    response_model=JobView,
    status_code=status.HTTP_202_ACCEPTED,
)
def ingest_content(
    body: Annotated[IngestRequest, Body()],
    queue: QueueDep,
    response: Response,
    remaining: IngestGuardDep,
) -> JobView:
    # IngestGuardDep enforces the per-IP rate limit + daily cost cap
    # BEFORE any work is enqueued; if either fires it raises an
    # HTTPException and we never reach this body.
    kind = detect_kind(body)
    if kind == "youtube":
        assert body.url is not None
        rq_job = enqueue_job(queue, JobKind.INGEST_YOUTUBE, {"url": body.url})
        log_kind = JobKind.INGEST_YOUTUBE
    elif kind == "article":
        assert body.url is not None
        rq_job = enqueue_job(queue, JobKind.INGEST_ARTICLE, {"url": body.url})
        log_kind = JobKind.INGEST_ARTICLE
    else:  # raw_text
        assert body.text is not None
        rq_job = enqueue_job(
            queue,
            JobKind.INGEST_RAW_TEXT,
            {"text": body.text, "label": body.label},
        )
        log_kind = JobKind.INGEST_RAW_TEXT
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    log.info(
        "content_ingest_enqueued",
        kind=log_kind.value,
        job_id=rq_job.id,
        rate_limit_remaining=remaining,
    )
    return job_to_view(rq_job, log_kind)


@router.get(
    "/{content_id}",
    response_model=IngestedContent,
)
def get_content(content_id: UUID, database_url: DatabaseUrlDep) -> IngestedContent:
    content = fetch_content(database_url, content_id)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"content {content_id} not found",
        )
    return content


@router.post(
    "/{content_id}/transcribe",
    response_model=JobView,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_transcription(
    content_id: UUID,
    queue: QueueDep,
    redis: RedisDep,
    database_url: DatabaseUrlDep,
) -> JobView:
    # Verify the content row exists before enqueuing.
    content = fetch_content(database_url, content_id)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"content {content_id} not found",
        )
    if content.source_type != "youtube":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"only YouTube content is transcribable; got source_type={content.source_type}",
        )

    rq_job = enqueue_job(
        queue,
        JobKind.TRANSCRIBE,
        {"content_id": str(content_id), "language": "en"},
        # faster-whisper on CPU transcribes at roughly real-time
        # (~1× audio duration). 30 minutes covers videos up to
        # ~25 minutes with comfortable headroom for model load
        # and ffmpeg conversion. RQ's library default of 180s
        # only handles 3-minute videos, which is what surfaced
        # the original JobTimeoutException.
        job_timeout=1800,
    )
    _record_content_job_link(redis, content_id, rq_job.id)
    return job_to_view(rq_job, JobKind.TRANSCRIBE)


@router.get(
    "/{content_id}/transcript",
    response_model=Transcript,
)
def get_transcript(content_id: UUID, database_url: DatabaseUrlDep) -> Transcript:
    transcript = fetch_transcript_for_content(database_url, content_id)
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no transcript yet for content {content_id}",
        )
    return transcript


__all__ = ["detect_kind", "router"]
