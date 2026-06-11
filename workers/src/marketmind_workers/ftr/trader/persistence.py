"""Postgres persistence for the FTR paper trader (psycopg, repo convention).

Crash safety: restart rebuilds broker + guard state from these tables
idempotently; duplicate decisions are suppressed by the
(strategy_id, symbol, bar_ts) unique key — re-evaluating a bar after a
restart is a no-op (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
import structlog

from marketmind_workers.ftr.strategies.records import DecisionRecord
from marketmind_workers.ftr.trader.paper_broker import PaperFill, Position

logger = structlog.get_logger(__name__)


def insert_decision(db_url: str, rec: DecisionRecord, bar_ts: datetime) -> int | None:
    """Insert one DecisionRecord; returns id or None if duplicate (idempotent)."""
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ftr_decisions
                (ts_utc, strategy_id, symbol, bar_ts, action, qty,
                 expected_move_bps, expected_cost_bps, confidence,
                 reason_codes, feature_snapshot_hash, model_version, git_sha)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (strategy_id, symbol, bar_ts) DO NOTHING
            RETURNING id
            """,
            (
                rec.ts_utc,
                rec.strategy_id,
                rec.symbol,
                bar_ts,
                rec.action.value,
                rec.qty,
                rec.expected_move_bps,
                rec.expected_cost_bps,
                rec.confidence,
                json.dumps([r.value for r in rec.reason_codes]),
                rec.feature_snapshot_hash,
                rec.model_version,
                rec.git_sha,
            ),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def insert_order_and_fill(
    db_url: str,
    *,
    decision_id: int | None,
    fill: PaperFill,
    strategy_id: str,
) -> None:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ftr_orders
                (decision_id, ts_utc, strategy_id, symbol, side, qty, status, venue_profile)
            VALUES (%s,%s,%s,%s,%s,%s,'filled',%s)
            RETURNING id
            """,
            (
                decision_id,
                fill.ts_utc,
                strategy_id,
                fill.symbol,
                fill.side,
                fill.qty,
                fill.venue_profile,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        order_id = int(row[0])
        cur.execute(
            """
            INSERT INTO ftr_fills
                (order_id, ts_utc, symbol, side, qty, reference_price,
                 fill_price, fee_paid, venue_profile)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                order_id,
                fill.ts_utc,
                fill.symbol,
                fill.side,
                fill.qty,
                fill.reference_price,
                fill.fill_price,
                fill.fee_paid,
                fill.venue_profile,
            ),
        )


def upsert_open_position(db_url: str, strategy_id: str, pos: Position) -> None:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ftr_positions (strategy_id, symbol, qty, avg_entry_price, opened_at)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (strategy_id, symbol, opened_at)
            DO UPDATE SET qty = EXCLUDED.qty, avg_entry_price = EXCLUDED.avg_entry_price
            """,
            (strategy_id, pos.symbol, pos.qty, pos.avg_entry_price, pos.opened_at),
        )


def close_position(db_url: str, strategy_id: str, symbol: str, closed_at: datetime) -> None:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ftr_positions SET closed_at = %s
            WHERE strategy_id = %s AND symbol = %s AND closed_at IS NULL
            """,
            (closed_at, strategy_id, symbol),
        )


def snapshot_equity(
    db_url: str,
    *,
    ts: datetime,
    cash: Decimal,
    positions_value: Decimal,
    gross_exposure_pct: float,
) -> None:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ftr_equity_snapshots
                (ts_utc, cash, positions_value, equity, gross_exposure_pct)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (ts_utc) DO NOTHING
            """,
            (ts, cash, positions_value, cash + positions_value, gross_exposure_pct),
        )


@dataclass(frozen=True)
class RecoveredState:
    cash: Decimal | None
    open_positions: list[Position]
    strategy_by_symbol: dict[str, str]


def recover_state(db_url: str, initial_cash: Decimal) -> RecoveredState:
    """Rebuild broker state from Postgres after a restart (idempotent)."""
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cash FROM ftr_equity_snapshots ORDER BY ts_utc DESC LIMIT 1"
        )
        row = cur.fetchone()
        cash = Decimal(str(row[0])) if row else None

        cur.execute(
            """
            SELECT strategy_id, symbol, qty, avg_entry_price, opened_at
            FROM ftr_positions WHERE closed_at IS NULL
            """
        )
        positions: list[Position] = []
        strategy_by_symbol: dict[str, str] = {}
        for strategy_id, symbol, qty, avg_px, opened_at in cur.fetchall():
            positions.append(
                Position(
                    symbol=symbol,
                    qty=Decimal(str(qty)),
                    avg_entry_price=Decimal(str(avg_px)),
                    opened_at=opened_at,
                )
            )
            strategy_by_symbol[symbol] = strategy_id
    logger.info(
        "ftr_state_recovered",
        cash=str(cash if cash is not None else initial_cash),
        open_positions=len(positions),
    )
    return RecoveredState(
        cash=cash, open_positions=positions, strategy_by_symbol=strategy_by_symbol
    )


def killswitch_engaged(db_url: str) -> bool:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT engaged FROM ftr_killswitch WHERE id = 1")
        row = cur.fetchone()
        return bool(row[0]) if row else False


def insert_verdict_rows(db_url: str, run_stamp: str, rows: list[dict[str, object]]) -> int:
    """Persist gate reports from a validation run into ftr_verdicts."""
    n = 0
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO ftr_verdicts
                    (run_stamp, strategy_id, venue_profile, uk_execution_feasible,
                     verdict, failed_gates, n_trials, metrics, artifact_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_stamp, strategy_id, venue_profile) DO NOTHING
                """,
                (
                    run_stamp,
                    r["strategy_id"],
                    r["venue_profile"],
                    r["uk_execution_feasible"],
                    r["verdict"],
                    json.dumps(r.get("failed_gates", [])),
                    r.get("n_trials", 0),
                    json.dumps(r.get("metrics", {})),
                    r.get("artifact_path"),
                ),
            )
            n += cur.rowcount
    return n
