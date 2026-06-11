"""Read-only Postgres helpers for the trader API surface.

Mirrors the existing `marketmind_api.repo` pattern — each helper
opens its own connection, executes one SELECT, and returns Pydantic
DTOs. The API process does NOT import worker code (Phase 0
architecture), so any computation logic needed beyond raw SQL
either lives here or is approximated.

Every helper returns either a Pydantic DTO from
`marketmind_shared.schemas.trader` (PortfolioSnapshot, PaperPosition,
…) or a small response-shaped dict the route handler converts.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from marketmind_shared.schemas.strategy_spec.common import Timeframe
from marketmind_shared.schemas.trader import (
    Alert,
    AlertChannel,
    AuditLog,
    DriftMetric,
    HealthStatus,
    LoopName,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PortfolioSnapshot,
    PositionSide,
    PositionStatus,
    RiskEventType,
    RunStatus,
    Severity,
    Signal,
    SignalKind,
)

# ---- Bot health (runner heartbeat) ----------------------------------------


def fetch_latest_bot_run(
    database_url: str,
) -> tuple[UUID, LoopName, RunStatus, datetime, datetime, str] | None:
    """Most-recent `trader_bot_runs` row, ordered by started_at DESC.

    Returns `(id, loop_name, status, started_at, last_heartbeat_at,
    notes)` or None if no rows exist. The status-strip's "Bot
    status" indicator uses this to detect the failure mode where
    the runner is alive enough to keep its heartbeat fresh but a
    specific phase (e.g. snapshot) is broken — and the
    snapshot-based freshness signal from `/trader/risk/status`
    would miss it.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, loop_name, status, started_at, last_heartbeat_at, notes
            FROM trader_bot_runs
            ORDER BY started_at DESC
            LIMIT 1
            """,
        )
        row = cur.fetchone()
    if row is None:
        return None
    return (
        UUID(str(row[0])),
        LoopName(row[1]),
        RunStatus(row[2]),
        row[3],
        row[4],
        row[5],
    )


# ---- Snapshots / portfolio -------------------------------------------------


def fetch_latest_snapshot(database_url: str) -> PortfolioSnapshot | None:
    """Most recent `trader_portfolio_snapshots` row, or None if none."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts, cash, equity, unrealised_pnl, realised_pnl_cumulative,
                   peak_equity, drawdown, drawdown_pct, open_positions_count,
                   per_strategy_breakdown, per_symbol_breakdown
            FROM trader_portfolio_snapshots
            ORDER BY ts DESC LIMIT 1
            """,
        )
        row = cur.fetchone()
    if row is None:
        return None
    return PortfolioSnapshot(
        id=row[0],
        ts=row[1],
        cash=row[2],
        equity=row[3],
        unrealised_pnl=row[4],
        realised_pnl_cumulative=row[5],
        peak_equity=row[6],
        drawdown=row[7],
        drawdown_pct=row[8],
        open_positions_count=row[9],
        per_strategy_breakdown=dict(row[10]) if row[10] else {},
        per_symbol_breakdown=dict(row[11]) if row[11] else {},
    )


def fetch_equity_curve(
    database_url: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[tuple[datetime, Decimal]]:
    """Equity-curve time-series. Filters applied in Python rather
    than SQL (pyright doesn't love f-string SQL); the snapshot
    table is small enough at v1 scale that over-fetching is fine.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ts, equity FROM trader_portfolio_snapshots ORDER BY ts ASC",
        )
        rows = cur.fetchall()
    if since is None and until is None:
        return rows
    return [
        (ts, equity)
        for ts, equity in rows
        if (since is None or ts >= since) and (until is None or ts <= until)
    ]


# ---- Positions -------------------------------------------------------------


def _row_to_paper_position(row: tuple[Any, ...]) -> PaperPosition:
    return PaperPosition(
        id=UUID(str(row[0])),
        strategy_version_id=UUID(str(row[1])),
        symbol=row[2],
        side=PositionSide(row[3]),
        entry_order_id=UUID(str(row[4])),
        exit_order_id=UUID(str(row[5])) if row[5] is not None else None,
        entry_price=row[6],
        entry_ts=row[7],
        exit_price=row[8],
        exit_ts=row[9],
        size=row[10],
        stop_price=row[11],
        take_profit_price=row[12],
        status=PositionStatus(row[13]),
        realised_pnl=row[14],
        realised_pnl_pct=row[15],
        close_reason=row[16],
    )


_PAPER_POSITION_COLS = (
    "id, strategy_version_id, symbol, side, entry_order_id, exit_order_id, "
    "entry_price, entry_ts, exit_price, exit_ts, size, stop_price, "
    "take_profit_price, status, realised_pnl, realised_pnl_pct, close_reason"
)


def fetch_open_positions(database_url: str) -> list[PaperPosition]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            # _PAPER_POSITION_COLS is a module-level constant of static
            # column names — no user input reaches this f-string.
            f"SELECT {_PAPER_POSITION_COLS} FROM trader_paper_positions "  # noqa: S608
            "WHERE status = 'OPEN' ORDER BY entry_ts DESC",
        )
        rows = cur.fetchall()
    return [_row_to_paper_position(r) for r in rows]


def fetch_closed_positions(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[PaperPosition]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            # _PAPER_POSITION_COLS is a module-level constant; limit /
            # offset come through the proper parameter binding.
            f"SELECT {_PAPER_POSITION_COLS} FROM trader_paper_positions "  # noqa: S608
            "WHERE status = 'CLOSED' ORDER BY exit_ts DESC NULLS LAST LIMIT %s OFFSET %s",
            (limit, offset),
        )
        rows = cur.fetchall()
    return [_row_to_paper_position(r) for r in rows]


# ---- Signals / orders / fills ---------------------------------------------


def fetch_recent_signals(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[Signal]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, strategy_version_id, symbol, timeframe, candle_close_ts,
                   signal, reason, indicators,
                   proposed_entry_price, proposed_stop_price,
                   proposed_take_profit_price, created_at, processed_at
            FROM trader_signals
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    return [
        Signal(
            id=UUID(str(r[0])),
            strategy_version_id=UUID(str(r[1])),
            symbol=r[2],
            timeframe=Timeframe(r[3]),
            candle_close_ts=r[4],
            signal=SignalKind(r[5]),
            reason=r[6],
            indicators=dict(r[7]) if r[7] else {},
            proposed_entry_price=r[8],
            proposed_stop_price=r[9],
            proposed_take_profit_price=r[10],
            created_at=r[11],
            processed_at=r[12],
        )
        for r in rows
    ]


def fetch_recent_orders(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[PaperOrder]:
    from marketmind_shared.schemas.trader import OrderSide, OrderStatus, OrderType

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, signal_id, strategy_version_id, symbol, side, order_type,
                   requested_size, requested_at, status, rejection_reason,
                   intended_fill_ts
            FROM trader_paper_orders
            ORDER BY requested_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    return [
        PaperOrder(
            id=UUID(str(r[0])),
            signal_id=UUID(str(r[1])),
            strategy_version_id=UUID(str(r[2])),
            symbol=r[3],
            side=OrderSide(r[4]),
            order_type=OrderType(r[5]),
            requested_size=r[6],
            requested_at=r[7],
            status=OrderStatus(r[8]),
            rejection_reason=r[9],
            intended_fill_ts=r[10],
        )
        for r in rows
    ]


def fetch_recent_fills(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[PaperFill]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, order_id, fill_ts, fill_price, size, fee,
                   slippage_bps_applied, notional
            FROM trader_paper_fills
            ORDER BY fill_ts DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    return [
        PaperFill(
            id=UUID(str(r[0])),
            order_id=UUID(str(r[1])),
            fill_ts=r[2],
            fill_price=r[3],
            size=r[4],
            fee=r[5],
            slippage_bps_applied=r[6],
            notional=r[7],
        )
        for r in rows
    ]


# ---- Strategies + drift ---------------------------------------------------


def fetch_strategy_versions_with_latest_drift(
    database_url: str,
) -> list[dict[str, Any]]:
    """List strategy versions with their latest drift health (if any).

    Returns dicts shaped for the API response, not Pydantic — the
    response combines version columns + a latest-drift summary
    that doesn't have a clean DTO yet. Route handler shapes the
    final response.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.strategy_id, v.version, v.template, v.symbols,
                   v.timeframes, v.risk_pct, v.enabled, v.approved_for_paper,
                   v.created_at,
                   d.health_status, d.ts AS drift_ts, d.window_label
            FROM trader_strategy_versions v
            LEFT JOIN LATERAL (
                SELECT health_status, ts, window_label
                FROM trader_drift_metrics
                WHERE strategy_version_id = v.id
                ORDER BY ts DESC LIMIT 1
            ) d ON TRUE
            ORDER BY v.created_at DESC
            """,
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "strategy_id": str(r[1]),
            "version": r[2],
            "template": r[3],
            "symbols": list(r[4]),
            "timeframes": list(r[5]),
            "risk_pct": str(r[6]),
            "enabled": r[7],
            "approved_for_paper": r[8],
            "created_at": r[9],
            "latest_drift_health": r[10],
            "latest_drift_ts": r[11],
            "latest_drift_window": r[12],
        }
        for r in rows
    ]


def fetch_latest_drift_per_version(database_url: str) -> list[DriftMetric]:
    """One DriftMetric row per strategy_version_id: the most recent."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (strategy_version_id)
                id, ts, strategy_version_id, window_label,
                paper_trade_count, paper_win_rate, paper_avg_return_per_trade,
                paper_current_drawdown_pct,
                backtest_trade_freq_per_week, backtest_win_rate,
                backtest_avg_return_per_trade, backtest_max_drawdown_pct,
                trade_freq_ratio, win_rate_delta, avg_return_delta,
                drawdown_ratio, health_status
            FROM trader_drift_metrics
            ORDER BY strategy_version_id, ts DESC
            """,
        )
        rows = cur.fetchall()
    return [
        DriftMetric(
            id=UUID(str(r[0])),
            ts=r[1],
            strategy_version_id=UUID(str(r[2])),
            window_label=r[3],
            paper_trade_count=r[4],
            paper_win_rate=r[5],
            paper_avg_return_per_trade=r[6],
            paper_current_drawdown_pct=r[7],
            backtest_trade_freq_per_week=r[8],
            backtest_win_rate=r[9],
            backtest_avg_return_per_trade=r[10],
            backtest_max_drawdown_pct=r[11],
            trade_freq_ratio=r[12],
            win_rate_delta=r[13],
            avg_return_delta=r[14],
            drawdown_ratio=r[15],
            health_status=HealthStatus(r[16]),
        )
        for r in rows
    ]


# ---- Risk + alerts + audit -------------------------------------------------


def fetch_recent_alerts(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[Alert]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts, channel, severity, subject, body, delivered, delivery_error
            FROM trader_alerts
            ORDER BY ts DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    return [
        Alert(
            id=UUID(str(r[0])),
            ts=r[1],
            channel=AlertChannel(r[2]),
            severity=Severity(r[3]),
            subject=r[4],
            body=r[5],
            delivered=r[6],
            delivery_error=r[7],
        )
        for r in rows
    ]


def fetch_recent_audit(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[AuditLog]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts, actor, event, entity_type, entity_id, payload
            FROM trader_audit_logs
            ORDER BY ts DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    return [
        AuditLog(
            id=r[0],
            ts=r[1],
            actor=r[2],
            event=r[3],
            entity_type=r[4],
            entity_id=r[5],
            payload=dict(r[6]) if r[6] else {},
        )
        for r in rows
    ]


def fetch_recent_risk_events(
    database_url: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """For GET /trader/risk/status. Returns the N most recent
    risk-event rows as dicts (response handler shapes the final form).
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, ts, event_type, severity, strategy_version_id, symbol, details
            FROM trader_risk_events
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "ts": r[1],
            "event_type": RiskEventType(r[2]).value,
            "severity": Severity(r[3]).value,
            "strategy_version_id": str(r[4]) if r[4] is not None else None,
            "symbol": r[5],
            "details": dict(r[6]) if r[6] else {},
        }
        for r in rows
    ]


# ---- Admin support ---------------------------------------------------------


def fetch_version_for_admin(
    database_url: str,
    version_id: UUID,
) -> dict[str, Any] | None:
    """Load a version's mutable + load-bearing fields for admin ops.

    Returns None if the row doesn't exist. `backtest_metrics` is
    returned as a Python dict so the approve_paper endpoint can
    validate the JSONB shape before flipping the flag.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, enabled, approved_for_paper, backtest_metrics
            FROM trader_strategy_versions
            WHERE id = %s
            """,
            (str(version_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "enabled": row[1],
        "approved_for_paper": row[2],
        "backtest_metrics": dict(row[3]) if row[3] else {},
    }


def update_version_flag(
    database_url: str,
    version_id: UUID,
    *,
    enabled: bool | None = None,
    approved_for_paper: bool | None = None,
) -> bool:
    """Update one or both mutable flags on a strategy version row.

    Returns True if a row was updated, False if version_id missing.
    The immutability trigger on trader_strategy_versions (migration
    0006) only permits these two columns + `notes` to change.
    """
    if enabled is None and approved_for_paper is None:
        return False
    sets: list[str] = []
    params: list[Any] = []
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(enabled)
    if approved_for_paper is not None:
        sets.append("approved_for_paper = %s")
        params.append(approved_for_paper)
    params.append(str(version_id))
    # `sets` only ever contains literal strings from the two-element
    # allowlist above ("enabled = %s" / "approved_for_paper = %s"); no
    # user input reaches the SET clause. All real values bind via %s.
    query = (
        "UPDATE trader_strategy_versions SET "  # noqa: S608
        + ", ".join(sets)
        + " WHERE id = %s"
    )
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(query, params)  # type: ignore[arg-type]
        rowcount = cur.rowcount
        conn.commit()
    return rowcount > 0
