"""Backtest run + view endpoints (Phase 3.2).

  POST /strategies/{strategy_id}/backtest -> enqueue or short-circuit.
                                              Idempotent on
                                              (strategy, start, end,
                                               initial_capital).
  GET  /backtests/{backtest_id}            -> BacktestResult, with the
                                              equity curve downsampled
                                              to ≤ 500 points.
  GET  /strategies/{strategy_id}/backtests -> paginated list of
                                              BacktestSummary rows.

The equity-curve downsampling happens at the API boundary. The DB
keeps the full-resolution curve so the per-run page can offer to
fetch the raw timeline; the default response is compact enough to
render fast.

A strategy with `spec=None` (LLM verdict was REFUSE / NEEDS_HUMAN /
NEEDS_INFO) can't be backtested — we reject with a 422 at the
request boundary rather than enqueueing a job that will fail.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from marketmind_shared.schemas import (
    BacktestResult,
    JobKind,
)
from pydantic import BaseModel, ConfigDict, Field

from marketmind_api.deps import DatabaseUrlDep, QueueDep
from marketmind_api.repo import (
    fetch_backtest_by_id,
    fetch_backtest_for_params,
    fetch_extraction_by_id,
    list_backtests_for_strategy,
)
from marketmind_api.routes.jobs import enqueue_job

# Mirror the worker's curve-downsampling target. We can't import the
# worker module from the API (Phase 0 boundary), so the constant is
# repeated here. If the worker ever changes the algorithm, copy the
# new logic over.
_CURVE_TARGET_POINTS: int = 500

router = APIRouter(tags=["backtests"])
log = structlog.get_logger(__name__)


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestRequest(_StrictResponse):
    start: datetime
    end: datetime
    initial_capital: float = Field(default=10_000.0, gt=0.0, le=1e12)


class BacktestStartedResponse(_StrictResponse):
    """POST /strategies/{id}/backtest response.

    `from_cache=True` means a backtest with these exact params already
    exists; the frontend should immediately GET /backtests/{id} instead
    of polling /jobs/{job_id}.
    """

    job_id: str
    from_cache: bool
    backtest_id: str | None = None


class BacktestSummary(_StrictResponse):
    """One row in GET /strategies/{id}/backtests."""

    backtest_id: UUID
    created_at: datetime
    start: datetime
    end: datetime
    initial_capital: float
    result: BacktestResult


class BacktestListResponse(_StrictResponse):
    items: list[BacktestSummary]
    limit: int
    offset: int


# ---- Helpers --------------------------------------------------------------


def _downsample_curve_in_result(result: BacktestResult) -> BacktestResult:
    """Return a BacktestResult with the strategy + benchmark curves
    shrunk to ≤ _CURVE_TARGET_POINTS.

    Pydantic models are frozen; we go through model_copy with
    update= so the rest of the model is preserved.
    """
    eq = list(result.run.equity_curve)
    bm = list(result.benchmark.equity_curve)
    new_eq = _downsample(eq)
    new_bm = _downsample(bm)
    if new_eq is eq and new_bm is bm:
        return result
    new_run = result.run.model_copy(update={"equity_curve": new_eq})
    new_bench = result.benchmark.model_copy(update={"equity_curve": new_bm})
    return result.model_copy(update={"run": new_run, "benchmark": new_bench})


def _downsample[T](points: list[T]) -> list[T]:
    """Mirror of workers.backtest.downsample but local-only — keeps the
    API independent of the worker package per the Phase 0 boundary.
    """
    target = _CURVE_TARGET_POINTS
    n = len(points)
    if n <= target:
        return points

    out: list[T] = [points[0]]
    middle = points[1:-1]
    if middle:
        buckets = max(2, (target - 2) // 2)
        step = len(middle) / buckets
        for b in range(buckets):
            lo = int(b * step)
            hi = int((b + 1) * step) if b < buckets - 1 else len(middle)
            if lo >= hi:
                continue
            chunk = middle[lo:hi]
            mn = min(chunk, key=_value_of)
            mx = max(chunk, key=_value_of)
            mn_ts = _ts_of(mn)
            mx_ts = _ts_of(mx)
            if mn_ts == mx_ts:
                out.append(mn)
            elif mn_ts < mx_ts:
                out.append(mn)
                out.append(mx)
            else:
                out.append(mx)
                out.append(mn)
    out.append(points[-1])

    deduped: list[T] = []
    prev_ts: datetime | None = None
    for p in out:
        ts = _ts_of(p)
        if prev_ts != ts:
            deduped.append(p)
            prev_ts = ts
    return deduped


def _ts_of(p: object) -> datetime:
    ts = getattr(p, "timestamp", None)
    assert isinstance(ts, datetime)
    return ts


def _value_of(p: object) -> float:
    v = getattr(p, "value", None)
    assert isinstance(v, float | int)
    return float(v)


# ---- POST /strategies/{strategy_id}/backtest -------------------------------


@router.post(
    "/strategies/{strategy_id}/backtest",
    response_model=BacktestStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_backtest(
    strategy_id: UUID,
    body: BacktestRequest,
    queue: QueueDep,
    database_url: DatabaseUrlDep,
) -> BacktestStartedResponse:
    extraction = fetch_extraction_by_id(database_url, strategy_id)
    if extraction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"strategy {strategy_id} not found",
        )
    if extraction.spec is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"strategy {strategy_id} has no spec "
                f"(verdict={extraction.report.verdict}); cannot backtest"
            ),
        )
    if body.end <= body.start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end must be strictly after start",
        )

    existing = fetch_backtest_for_params(
        database_url,
        strategy_id=strategy_id,
        start_ts=body.start,
        end_ts=body.end,
        initial_capital=body.initial_capital,
    )
    if existing is not None:
        backtest_id, _ = existing
        log.info(
            "backtest_idempotent_hit",
            strategy_id=str(strategy_id),
            backtest_id=str(backtest_id),
        )
        return BacktestStartedResponse(
            job_id="",
            from_cache=True,
            backtest_id=str(backtest_id),
        )

    rq_job = enqueue_job(
        queue,
        JobKind.BACKTEST,
        {
            "strategy_id": str(strategy_id),
            "start_iso": body.start.isoformat(),
            "end_iso": body.end.isoformat(),
            "initial_capital": body.initial_capital,
        },
        # vbt can be slow on multi-year backtests with high-bar-
        # count timeframes (1h × 6y ≈ 50k bars). 600s covers the
        # worst observed end-to-end including the parquet fetch.
        job_timeout=600,
    )
    log.info(
        "backtest_enqueued",
        strategy_id=str(strategy_id),
        job_id=rq_job.id,
        start=body.start.isoformat(),
        end=body.end.isoformat(),
    )
    return BacktestStartedResponse(
        job_id=rq_job.id,
        from_cache=False,
        backtest_id=None,
    )


# ---- GET /backtests/{backtest_id} ------------------------------------------


@router.get(
    "/backtests/{backtest_id}",
    response_model=BacktestResult,
)
def get_backtest(backtest_id: UUID, database_url: DatabaseUrlDep) -> BacktestResult:
    row = fetch_backtest_by_id(database_url, backtest_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"backtest {backtest_id} not found",
        )
    _, result, _ = row
    return _downsample_curve_in_result(result)


# ---- GET /strategies/{strategy_id}/backtests -------------------------------


@router.get(
    "/strategies/{strategy_id}/backtests",
    response_model=BacktestListResponse,
)
def list_strategy_backtests(
    strategy_id: UUID,
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BacktestListResponse:
    rows = list_backtests_for_strategy(database_url, strategy_id, limit=limit, offset=offset)
    items = [
        BacktestSummary(
            backtest_id=row["backtest_id"],
            created_at=row["created_at"],
            start=row["start_ts"],
            end=row["end_ts"],
            initial_capital=row["initial_capital"],
            result=_downsample_curve_in_result(row["result"]),
        )
        for row in rows
    ]
    return BacktestListResponse(items=items, limit=limit, offset=offset)


__all__ = [
    "BacktestListResponse",
    "BacktestRequest",
    "BacktestStartedResponse",
    "BacktestSummary",
    "router",
]
