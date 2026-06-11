"""Phase 4 overfitting analysis endpoints.

  POST /backtests/{backtest_id}/overfitting -> enqueue or cache hit
  GET  /overfitting/{analysis_id}            -> full OverfittingAnalysis
  GET  /backtests/{backtest_id}/overfitting  -> latest analysis for a
                                                 backtest (404 if none)

Idempotency: a backtest can have at most one analysis row (UNIQUE
index on backtest_id). The POST endpoint short-circuits to the cached
row when present.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, status
from marketmind_shared.schemas import (
    JobKind,
    OverfittingAnalysis,
)
from pydantic import BaseModel, ConfigDict

from marketmind_api.deps import DatabaseUrlDep, QueueDep
from marketmind_api.repo import (
    fetch_backtest_by_id,
    fetch_overfitting_by_id,
    fetch_overfitting_for_backtest,
)
from marketmind_api.routes.jobs import enqueue_job

router = APIRouter(tags=["overfitting"])
log = structlog.get_logger(__name__)


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OverfittingStartedResponse(_StrictResponse):
    """POST /backtests/{id}/overfitting response.

    `from_cache=True` means a prior analysis exists; the client should
    fetch /overfitting/{analysis_id} directly instead of polling.
    """

    job_id: str
    from_cache: bool
    analysis_id: str | None = None


# ---- POST /backtests/{backtest_id}/overfitting ----------------------------


@router.post(
    "/backtests/{backtest_id}/overfitting",
    response_model=OverfittingStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_overfitting(
    backtest_id: UUID,
    queue: QueueDep,
    database_url: DatabaseUrlDep,
) -> OverfittingStartedResponse:
    if fetch_backtest_by_id(database_url, backtest_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"backtest {backtest_id} not found",
        )

    existing = fetch_overfitting_for_backtest(database_url, backtest_id)
    if existing is not None:
        analysis_id, _ = existing
        log.info(
            "overfitting_idempotent_hit",
            backtest_id=str(backtest_id),
            analysis_id=str(analysis_id),
        )
        return OverfittingStartedResponse(
            job_id="",
            from_cache=True,
            analysis_id=str(analysis_id),
        )

    rq_job = enqueue_job(
        queue,
        JobKind.OVERFITTING_ANALYSIS,
        {"backtest_id": str(backtest_id)},
        # Three sub-analyses run sequentially: walk-forward (N
        # windows × 1 backtest each), parameter sweep (grid of
        # backtests), and monte-carlo (resample-and-run). On a
        # parameter sweep with ≥9 cells over a multi-year window
        # the total can approach 25 minutes. 30-minute ceiling.
        job_timeout=1800,
    )
    log.info(
        "overfitting_enqueued",
        backtest_id=str(backtest_id),
        job_id=rq_job.id,
    )
    return OverfittingStartedResponse(
        job_id=rq_job.id,
        from_cache=False,
        analysis_id=None,
    )


# ---- GET /overfitting/{analysis_id} ---------------------------------------


@router.get(
    "/overfitting/{analysis_id}",
    response_model=OverfittingAnalysis,
)
def get_overfitting(analysis_id: UUID, database_url: DatabaseUrlDep) -> OverfittingAnalysis:
    row = fetch_overfitting_by_id(database_url, analysis_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"overfitting analysis {analysis_id} not found",
        )
    _, analysis, _ = row
    return analysis


# ---- GET /backtests/{backtest_id}/overfitting -----------------------------


class BacktestOverfittingResponse(_StrictResponse):
    """Wrapper so the UI can distinguish 'no analysis yet' from a 404
    on the backtest itself.
    """

    analysis_id: str
    analysis: OverfittingAnalysis


@router.get(
    "/backtests/{backtest_id}/overfitting",
    response_model=BacktestOverfittingResponse,
)
def get_overfitting_for_backtest(
    backtest_id: UUID,
    database_url: DatabaseUrlDep,
) -> BacktestOverfittingResponse:
    if fetch_backtest_by_id(database_url, backtest_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"backtest {backtest_id} not found",
        )
    row = fetch_overfitting_for_backtest(database_url, backtest_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no overfitting analysis for backtest {backtest_id}",
        )
    analysis_id, analysis = row
    return BacktestOverfittingResponse(
        analysis_id=str(analysis_id),
        analysis=analysis,
    )


__all__ = [
    "BacktestOverfittingResponse",
    "OverfittingStartedResponse",
    "router",
]
