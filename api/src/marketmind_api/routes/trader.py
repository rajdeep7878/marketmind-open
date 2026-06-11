"""Trader read-only HTTP routes (Step 11).

Every route under `/trader` is GET-only and surfaces state the
worker side persists. The API process does NOT import worker
code (Phase 0 architecture); the read helpers in
`marketmind_api.trader.read` run all SQL.

Endpoints:
  - GET /trader/portfolio/current
  - GET /trader/portfolio/equity_curve
  - GET /trader/positions/open
  - GET /trader/positions/closed
  - GET /trader/signals/recent
  - GET /trader/orders/recent
  - GET /trader/fills/recent
  - GET /trader/strategies
  - GET /trader/risk/status
  - GET /trader/drift/latest
  - GET /trader/alerts/recent
  - GET /trader/audit/recent

Admin POST endpoints live in `routes/trader_admin.py` to keep the
auth surface visually separated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from marketmind_shared.schemas.trader import (
    Alert,
    AuditLog,
    DriftMetric,
    LoopName,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PortfolioSnapshot,
    RunStatus,
    Signal,
)
from pydantic import BaseModel, ConfigDict, Field

from marketmind_api.deps import DatabaseUrlDep
from marketmind_api.trader import read

router = APIRouter(prefix="/trader", tags=["trader"])
log = structlog.get_logger(__name__)


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- Response wrappers -----------------------------------------------------


class EquityCurvePoint(_StrictResponse):
    ts: datetime
    equity: Decimal


class EquityCurveResponse(_StrictResponse):
    points: list[EquityCurvePoint]


class PaperPositionList(_StrictResponse):
    items: list[PaperPosition]


class PaperPositionListPaginated(_StrictResponse):
    items: list[PaperPosition]
    limit: int
    offset: int


class SignalList(_StrictResponse):
    items: list[Signal]
    limit: int
    offset: int


class OrderList(_StrictResponse):
    items: list[PaperOrder]
    limit: int
    offset: int


class FillList(_StrictResponse):
    items: list[PaperFill]
    limit: int
    offset: int


class AlertList(_StrictResponse):
    items: list[Alert]
    limit: int
    offset: int


class AuditList(_StrictResponse):
    items: list[AuditLog]
    limit: int
    offset: int


class StrategyVersionSummary(_StrictResponse):
    """One row in GET /trader/strategies.

    Combines version fields with the latest drift_health (if any).
    Returned as strings + simple types so the response is stable
    even as drift / version fields evolve.
    """

    id: str
    strategy_id: str
    version: int
    template: str
    symbols: list[str]
    timeframes: list[str]
    risk_pct: str
    enabled: bool
    approved_for_paper: bool
    created_at: datetime
    latest_drift_health: str | None = None
    latest_drift_ts: datetime | None = None
    latest_drift_window: str | None = None


class StrategyVersionList(_StrictResponse):
    items: list[StrategyVersionSummary]


class DriftList(_StrictResponse):
    items: list[DriftMetric]


class BotRunSummary(_StrictResponse):
    """Subset of the latest `trader_bot_runs` row the health endpoint
    surfaces. Excludes `worker_id` (forensic-only) so the response
    stays minimal.
    """

    id: UUID
    loop_name: LoopName
    status: RunStatus
    started_at: datetime
    last_heartbeat_at: datetime
    notes: str


class HealthResponse(_StrictResponse):
    """`GET /trader/health` — two complementary freshness signals
    so the dashboard's "Bot status" indicator can detect both:

      - `latest_run.last_heartbeat_at` — written by every phase of
        every cycle. Stale heartbeat = the runner is stuck or dead.

      - `last_snapshot_ts` — written by the snapshot phase only.
        If this lags but heartbeat is fresh, the snapshot phase
        specifically is broken.

    The `now` field carries the server's current UTC time so the
    client can compute freshness against the server's clock,
    sidestepping local-clock skew.
    """

    latest_run: BotRunSummary | None
    last_snapshot_ts: datetime | None
    now: datetime


class RiskStatusResponse(_StrictResponse):
    """GET /trader/risk/status. Current portfolio state + kill-switch
    flag + the recent risk-event tail.
    """

    cash: Decimal | None = None
    equity: Decimal | None = None
    drawdown_pct: Decimal | None = None
    peak_equity: Decimal | None = None
    kill_switch_tripped: bool = Field(
        default=False,
        description=(
            "True if the latest snapshot's drawdown_pct exceeded the trader's "
            "max drawdown threshold per the risk manager's check #1. False "
            "otherwise — including when no snapshot exists yet."
        ),
    )
    last_snapshot_ts: datetime | None = None
    recent_risk_events: list[dict[str, Any]]


# ---- Portfolio -------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
def get_health(database_url: DatabaseUrlDep) -> HealthResponse:
    """Bot liveness signals used by the dashboard's status strip.

    Combines the latest `trader_bot_runs` row (heartbeat) with the
    latest snapshot timestamp. A single call covers both failure
    modes: silent process death (heartbeat ages out) and broken-
    snapshot-phase (heartbeat fresh but snapshot stale).
    """
    run_tuple = read.fetch_latest_bot_run(database_url)
    snap = read.fetch_latest_snapshot(database_url)
    latest_run: BotRunSummary | None = None
    if run_tuple is not None:
        run_id, loop_name, status_val, started_at, last_hb, notes = run_tuple
        latest_run = BotRunSummary(
            id=run_id,
            loop_name=loop_name,
            status=status_val,
            started_at=started_at,
            last_heartbeat_at=last_hb,
            notes=notes,
        )
    return HealthResponse(
        latest_run=latest_run,
        last_snapshot_ts=snap.ts if snap is not None else None,
        now=datetime.now(UTC),
    )


@router.get(
    "/portfolio/current",
    response_model=PortfolioSnapshot | None,
)
def get_portfolio_current(database_url: DatabaseUrlDep) -> PortfolioSnapshot | None:
    """Most recent `trader_portfolio_snapshots` row, or None if the
    bot has never run a snapshot yet. The Step 12 runner writes one
    snapshot per signal-execution cycle, so this is always at most
    one cycle stale.
    """
    return read.fetch_latest_snapshot(database_url)


@router.get(
    "/portfolio/equity_curve",
    response_model=EquityCurveResponse,
)
def get_portfolio_equity_curve(
    database_url: DatabaseUrlDep,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> EquityCurveResponse:
    """Time series of (ts, equity) from the snapshot table, asc.
    Optional `since`/`until` bounds (inclusive).
    """
    pairs = read.fetch_equity_curve(database_url, since=since, until=until)
    return EquityCurveResponse(
        points=[EquityCurvePoint(ts=ts, equity=eq) for ts, eq in pairs],
    )


# ---- Positions -------------------------------------------------------------


@router.get("/positions/open", response_model=PaperPositionList)
def get_positions_open(database_url: DatabaseUrlDep) -> PaperPositionList:
    """All OPEN paper positions, newest entry first."""
    return PaperPositionList(items=read.fetch_open_positions(database_url))


@router.get("/positions/closed", response_model=PaperPositionListPaginated)
def get_positions_closed(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaperPositionListPaginated:
    """Most-recently-closed paper positions."""
    items = read.fetch_closed_positions(database_url, limit=limit, offset=offset)
    return PaperPositionListPaginated(items=items, limit=limit, offset=offset)


# ---- Signals / orders / fills ---------------------------------------------


@router.get("/signals/recent", response_model=SignalList)
def get_signals_recent(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SignalList:
    """Most-recent non-HOLD signals. HOLDs are intentionally not
    persisted — they would dominate this list (see signal_engine.py).
    """
    items = read.fetch_recent_signals(database_url, limit=limit, offset=offset)
    return SignalList(items=items, limit=limit, offset=offset)


@router.get("/orders/recent", response_model=OrderList)
def get_orders_recent(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderList:
    items = read.fetch_recent_orders(database_url, limit=limit, offset=offset)
    return OrderList(items=items, limit=limit, offset=offset)


@router.get("/fills/recent", response_model=FillList)
def get_fills_recent(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FillList:
    items = read.fetch_recent_fills(database_url, limit=limit, offset=offset)
    return FillList(items=items, limit=limit, offset=offset)


# ---- Strategies ------------------------------------------------------------


@router.get("/strategies", response_model=StrategyVersionList)
def get_strategies(database_url: DatabaseUrlDep) -> StrategyVersionList:
    """All strategy versions with their latest drift health (if any)."""
    rows = read.fetch_strategy_versions_with_latest_drift(database_url)
    return StrategyVersionList(
        items=[StrategyVersionSummary.model_validate(row) for row in rows],
    )


# ---- Risk + drift + alerts + audit ----------------------------------------


@router.get("/risk/status", response_model=RiskStatusResponse)
def get_risk_status(database_url: DatabaseUrlDep) -> RiskStatusResponse:
    """Portfolio state from the latest snapshot + a tail of risk
    events so the operator can see what triggered any blocks.

    `kill_switch_tripped` is derived from the snapshot's
    `drawdown_pct` against the trader's max threshold — but the
    API doesn't know that threshold (it's a worker setting). So
    this endpoint reports drawdown_pct + recent_risk_events and
    lets the operator decide. A KILL_SWITCH event in
    recent_risk_events is the canonical signal.
    """
    snapshot = read.fetch_latest_snapshot(database_url)
    events = read.fetch_recent_risk_events(database_url, limit=10)
    kill_switch = any(e["event_type"] == "kill_switch" for e in events)
    if snapshot is None:
        return RiskStatusResponse(
            kill_switch_tripped=kill_switch,
            recent_risk_events=events,
        )
    return RiskStatusResponse(
        cash=snapshot.cash,
        equity=snapshot.equity,
        drawdown_pct=snapshot.drawdown_pct,
        peak_equity=snapshot.peak_equity,
        kill_switch_tripped=kill_switch,
        last_snapshot_ts=snapshot.ts,
        recent_risk_events=events,
    )


@router.get("/drift/latest", response_model=DriftList)
def get_drift_latest(database_url: DatabaseUrlDep) -> DriftList:
    return DriftList(items=read.fetch_latest_drift_per_version(database_url))


@router.get("/alerts/recent", response_model=AlertList)
def get_alerts_recent(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlertList:
    items = read.fetch_recent_alerts(database_url, limit=limit, offset=offset)
    return AlertList(items=items, limit=limit, offset=offset)


@router.get("/audit/recent", response_model=AuditList)
def get_audit_recent(
    database_url: DatabaseUrlDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditList:
    items = read.fetch_recent_audit(database_url, limit=limit, offset=offset)
    return AuditList(items=items, limit=limit, offset=offset)


__all__ = ["router"]


# Silence unused-import noise: HTTPException + status are imported
# eagerly because admin routes share this module's import block style.
# Use them in trader_admin.py.
_ = HTTPException, status
