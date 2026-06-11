"""Jobs endpoint — the Phase 0 dummy-job flow.

POST /jobs   -> enqueue (dummy only in Phase 0)
GET  /jobs/{id} -> fetch RQ job, map to JobView

Note the string job reference in `enqueue()`: the API process never
imports `marketmind_workers` code. Keeping that boundary clean now
prevents accidental coupling later.
"""

from __future__ import annotations

from datetime import UTC
from typing import Final
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, HTTPException, status
from marketmind_shared.schemas import JobKind, JobStatus, JobSubmission, JobView
from pydantic import BaseModel, ConfigDict
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.job import JobStatus as RqJobStatus

from marketmind_api.deps import QueueDep, RedisDep

router = APIRouter(prefix="/jobs", tags=["jobs"])
log = structlog.get_logger(__name__)

# Maps "kind" -> dotted-string reference to the worker callable. The
# worker module must be importable by the worker process, NOT by the
# API process. RQ resolves the string at execution time on the worker.
_JOB_TARGETS: dict[JobKind, str] = {
    JobKind.DUMMY: "marketmind_workers.jobs.dummy.run",
    JobKind.INGEST_YOUTUBE: "marketmind_workers.jobs.ingest_youtube.run",
    JobKind.INGEST_ARTICLE: "marketmind_workers.jobs.ingest_article.run",
    JobKind.INGEST_RAW_TEXT: "marketmind_workers.jobs.ingest_raw_text.run",
    JobKind.TRANSCRIBE: "marketmind_workers.jobs.transcribe.run",
    JobKind.EXTRACT_STRATEGY: "marketmind_workers.jobs.extract_strategy.run",
    JobKind.BACKTEST: "marketmind_workers.jobs.backtest.run",
    JobKind.OVERFITTING_ANALYSIS: "marketmind_workers.jobs.overfitting_analysis.run",
}

# RQ's `job.meta` is a free-form dict; we use it as a single source of
# truth for the JobKind so GET /jobs/{id} can recover the kind without
# tracking a sidecar Redis hash. Phase 2.2 may add more meta keys
# (originating user id, payload digest, etc.) — keep this key stable.
_META_KIND: Final[str] = "marketmind:kind"

# Map RQ's internal status enum to our public-facing one. Unknown states
# fall back to FAILED rather than silently passing through.
# Keyed by the raw string value of RQ's enum because get_status() may
# return either an enum member or a bare string depending on RQ version;
# str(enum_member) returns "JobStatus.QUEUED" (not "queued"), which is
# why we normalize via .value below rather than str().
_RQ_STATUS_MAP: dict[str, JobStatus] = {
    RqJobStatus.QUEUED.value: JobStatus.QUEUED,
    RqJobStatus.STARTED.value: JobStatus.STARTED,
    RqJobStatus.FINISHED.value: JobStatus.FINISHED,
    RqJobStatus.FAILED.value: JobStatus.FAILED,
    RqJobStatus.DEFERRED.value: JobStatus.DEFERRED,
    RqJobStatus.SCHEDULED.value: JobStatus.QUEUED,
    RqJobStatus.CANCELED.value: JobStatus.FAILED,
    RqJobStatus.STOPPED.value: JobStatus.FAILED,
}


def _normalize_rq_status(raw: object) -> str:
    if isinstance(raw, RqJobStatus):
        return raw.value
    return str(raw) if raw is not None else RqJobStatus.QUEUED.value


def job_to_view(job: Job, kind: JobKind) -> JobView:
    rq_status_raw = job.get_status(refresh=False)
    rq_status = _normalize_rq_status(rq_status_raw)
    mapped = _RQ_STATUS_MAP.get(rq_status, JobStatus.FAILED)

    result: dict[str, object] | None = None
    if mapped is JobStatus.FINISHED and isinstance(job.result, dict):
        result = job.result

    error: str | None = None
    if mapped is JobStatus.FAILED:
        # exc_info is a string captured by RQ when the job raised.
        error = job.exc_info or "job failed"

    return JobView(
        id=UUID(job.id),
        kind=kind,
        status=mapped,
        result=result,
        error=error,
        enqueued_at=job.enqueued_at.replace(tzinfo=UTC) if job.enqueued_at else None,
        started_at=job.started_at.replace(tzinfo=UTC) if job.started_at else None,
        ended_at=job.ended_at.replace(tzinfo=UTC) if job.ended_at else None,
    )


def enqueue_job(
    queue: Queue,
    kind: JobKind,
    kwargs: dict[str, object],
    *,
    job_id: str | None = None,
    job_timeout: int | None = None,
) -> Job:
    """Shared enqueue path used by both /jobs and /content/*.

    Stores the kind in job.meta so the GET endpoint can recover it.
    Phase 0's /jobs flow only knows about DUMMY; Phase 2.1's /content
    flows pass INGEST_YOUTUBE / INGEST_ARTICLE / TRANSCRIBE.

    `job_timeout` (seconds) — when not provided, RQ's library default
    of 180s applies. Long-running jobs (transcribe, extract,
    backtest, overfitting) must pass an explicit value at the call
    site sized to the job's worst-case wall-clock. Ingest and dummy
    jobs leave it None.
    """
    target = _JOB_TARGETS[kind]
    jid = job_id or str(uuid4())
    enqueue_kwargs: dict[str, object] = {
        "kwargs": kwargs,
        "job_id": jid,
        "meta": {_META_KIND: kind.value},
        "result_ttl": 3600,  # keep finished results for 1h
        "failure_ttl": 86400,  # keep failure info for 24h for debugging
    }
    if job_timeout is not None:
        enqueue_kwargs["job_timeout"] = job_timeout
    rq_job = queue.enqueue(target, **enqueue_kwargs)  # type: ignore[arg-type]
    log.info(
        "job_enqueued",
        job_id=jid,
        kind=kind.value,
        target=target,
        job_timeout=job_timeout,
    )
    return rq_job


def _kind_from_job(job: Job) -> JobKind:
    """Recover the JobKind for a fetched RQ job.

    Falls back to DUMMY for legacy Phase-0 rows enqueued before
    job.meta carried a kind tag.
    """
    raw = job.meta.get(_META_KIND) if job.meta else None
    if isinstance(raw, str):
        try:
            return JobKind(raw)
        except ValueError:
            pass
    return JobKind.DUMMY


@router.post(
    "",
    response_model=JobView,
    status_code=status.HTTP_201_CREATED,
)
def submit_job(submission: JobSubmission, queue: QueueDep) -> JobView:
    rq_job = enqueue_job(
        queue,
        submission.kind,
        kwargs=submission.payload.model_dump(),
    )
    return job_to_view(rq_job, submission.kind)


# String markers the API matches in `job.exc_info` to detect specific
# worker-side IngestError subclasses. The worker module path is stable
# (relocating a class would be a deliberate, reviewed change); matching
# on the dotted path avoids false positives on the bare class name
# appearing inside someone's traceback for an unrelated reason.
_COOKIE_ERROR_MARKER: Final[str] = "marketmind_workers.services.ingest.CookieError"
_FORMAT_UNAVAILABLE_MARKER: Final[str] = "marketmind_workers.services.ingest.FormatUnavailableError"
# RQ raises JobTimeoutException when a job exceeds its `job_timeout`.
# We surface a 504 + plain-English message instead of leaking the
# RQ traceback to the operator. The 30-minute transcribe timeout
# (set in routes/content.py) handles ~25-min videos on CPU whisper;
# anything longer needs a smaller video or a faster model.
_TIMEOUT_MARKER: Final[str] = "rq.timeouts.JobTimeoutException"

_COOKIE_ERROR_USER_MESSAGE: Final[str] = (
    "Extraction temporarily unavailable. Our team has been notified. "
    "Please try again later or contact support."
)
_FORMAT_UNAVAILABLE_USER_MESSAGE: Final[str] = (
    "This video's audio format isn't supported. Please try a different video."
)
_TIMEOUT_USER_MESSAGE: Final[str] = (
    "This step took longer than expected. Try a shorter video, "
    "or check the worker logs for stuck jobs."
)


def _exc_info_contains(rq_job: Job, marker: str) -> bool:
    return marker in (rq_job.exc_info or "")


@router.get(
    "/{job_id}",
    response_model=JobView,
)
def get_job(job_id: UUID, redis: RedisDep) -> JobView:
    try:
        rq_job = Job.fetch(str(job_id), connection=redis)
    except NoSuchJobError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"job {job_id} not found",
        ) from exc

    # Specific IngestError subclasses → friendly bodies instead of the
    # default path that would surface the full Python traceback in the
    # `error` field of a 200 JobView.
    #   - CookieError → 503 (transient; operator must rotate cookies)
    #   - FormatUnavailableError → 422 (permanent for this source; the
    #     client should pick a different video)
    rq_status = _normalize_rq_status(rq_job.get_status(refresh=False))
    if _RQ_STATUS_MAP.get(rq_status) is JobStatus.FAILED:
        kind = _kind_from_job(rq_job)
        if _exc_info_contains(rq_job, _COOKIE_ERROR_MARKER):
            log.error("job_failed_cookie_auth", job_id=str(rq_job.id), kind=kind.value)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "extraction_unavailable",
                    "message": _COOKIE_ERROR_USER_MESSAGE,
                },
            )
        if _exc_info_contains(rq_job, _FORMAT_UNAVAILABLE_MARKER):
            log.error("job_failed_format_unavailable", job_id=str(rq_job.id), kind=kind.value)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "format_unavailable",
                    "message": _FORMAT_UNAVAILABLE_USER_MESSAGE,
                },
            )
        if _exc_info_contains(rq_job, _TIMEOUT_MARKER):
            log.error("job_failed_timeout", job_id=str(rq_job.id), kind=kind.value)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail={
                    "error": "job_timeout",
                    "message": _TIMEOUT_USER_MESSAGE,
                },
            )

    return job_to_view(rq_job, _kind_from_job(rq_job))


# ---- /jobs/{id}/progress ---------------------------------------------------


# RQ meta key that workers write structured progress under. Mirrors
# `_PROGRESS_KEY` in workers/jobs/overfitting_analysis.py — kept in
# sync manually so the API doesn't import the worker module.
_PROGRESS_META_KEY: Final[str] = "marketmind:overfitting:progress"


class JobProgress(BaseModel):
    """Lightweight progress descriptor.

    `step` is a free-form string (e.g., "walk_forward", "parameter_sweep").
    `current`/`total` lets the UI render "step 2 of 4" without having to
    enumerate the step list itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: JobStatus
    step: str | None = None
    current: int | None = None
    total: int | None = None


@router.get(
    "/{job_id}/progress",
    response_model=JobProgress,
)
def get_job_progress(job_id: UUID, redis: RedisDep) -> JobProgress:
    """Generic progress endpoint. Reads `_PROGRESS_META_KEY` from the
    RQ job's meta dict; returns None fields when the worker hasn't
    published anything yet (the job's still queued, or it doesn't
    publish progress at all).
    """
    try:
        rq_job = Job.fetch(str(job_id), connection=redis)
    except NoSuchJobError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"job {job_id} not found",
        ) from exc
    rq_status_raw = rq_job.get_status(refresh=False)
    rq_status = _normalize_rq_status(rq_status_raw)
    mapped = _RQ_STATUS_MAP.get(rq_status, JobStatus.FAILED)
    progress = rq_job.meta.get(_PROGRESS_META_KEY) if rq_job.meta else None
    if not isinstance(progress, dict):
        return JobProgress(job_id=job_id, status=mapped)
    step = progress.get("step")
    current = progress.get("current")
    total = progress.get("total")
    return JobProgress(
        job_id=job_id,
        status=mapped,
        step=step if isinstance(step, str) else None,
        current=current if isinstance(current, int) else None,
        total=total if isinstance(total, int) else None,
    )


__all__ = ["_JOB_TARGETS", "_META_KIND", "enqueue_job", "router"]
