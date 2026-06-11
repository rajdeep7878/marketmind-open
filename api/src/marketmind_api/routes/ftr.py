"""FTR (Phase D) read-only research endpoints.

All wording neutral and descriptive; performance numbers are always net of
the named cost profile. No promotional language — verdicts are research
records, not recommendations.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from marketmind_api.deps import DatabaseUrlDep

router = APIRouter(prefix="/ftr", tags=["ftr"])


class VerdictRow(BaseModel):
    run_stamp: str
    strategy_id: str
    venue_profile: str
    uk_execution_feasible: bool
    verdict: str
    failed_gates: list[str]
    n_trials: int
    metrics: dict[str, Any]
    artifact_path: str | None
    created_at: str


def _rows(db_url: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql, params)  # type: ignore[arg-type]
        return list(cur.fetchall())


@router.get("/verdicts")
def verdicts(db_url: DatabaseUrlDep) -> dict[str, Any]:
    """Full strategy x venue-profile verdict matrix with failed gates."""
    rows = _rows(
        db_url,
        """
        SELECT run_stamp, strategy_id, venue_profile, uk_execution_feasible,
               verdict, failed_gates, n_trials, metrics, artifact_path,
               created_at::text
        FROM ftr_verdicts
        ORDER BY created_at DESC, strategy_id, venue_profile
        """,
    )
    out = [
        VerdictRow(
            run_stamp=r[0],
            strategy_id=r[1],
            venue_profile=r[2],
            uk_execution_feasible=r[3],
            verdict=r[4],
            failed_gates=r[5],
            n_trials=r[6],
            metrics=r[7],
            artifact_path=r[8],
            created_at=r[9],
        ).model_dump()
        for r in rows
    ]
    deploy_eligible = sorted(
        {v["strategy_id"] for v in out if v["verdict"] == "PASS" and v["uk_execution_feasible"]}
    )
    return {"verdicts": out, "deploy_eligible_for_paper": deploy_eligible}


@router.get("/rejections")
def rejections(db_url: DatabaseUrlDep) -> dict[str, Any]:
    """Every REJECTED / INSUFFICIENT_DATA verdict with reasons."""
    rows = _rows(
        db_url,
        """
        SELECT run_stamp, strategy_id, venue_profile, verdict, failed_gates,
               metrics, created_at::text
        FROM ftr_verdicts
        WHERE verdict IN ('REJECTED', 'INSUFFICIENT_DATA')
        ORDER BY created_at DESC
        """,
    )
    return {
        "rejections": [
            {
                "run_stamp": r[0],
                "strategy_id": r[1],
                "venue_profile": r[2],
                "verdict": r[3],
                "failed_gates": r[4],
                "metrics": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]
    }


@router.get("/paper/equity")
def paper_equity(db_url: DatabaseUrlDep, limit: int = Query(default=500, le=5000)) -> dict[str, Any]:
    rows = _rows(
        db_url,
        """
        SELECT ts_utc::text, cash::float8, positions_value::float8,
               equity::float8, gross_exposure_pct
        FROM ftr_equity_snapshots ORDER BY ts_utc DESC LIMIT %s
        """,
        (limit,),
    )
    return {
        "snapshots": [
            {
                "ts_utc": r[0],
                "cash": r[1],
                "positions_value": r[2],
                "equity": r[3],
                "gross_exposure_pct": r[4],
            }
            for r in reversed(rows)
        ]
    }


@router.get("/paper/positions")
def paper_positions(db_url: DatabaseUrlDep) -> dict[str, Any]:
    rows = _rows(
        db_url,
        """
        SELECT strategy_id, symbol, qty::float8, avg_entry_price::float8,
               opened_at::text, closed_at::text
        FROM ftr_positions ORDER BY opened_at DESC LIMIT 200
        """,
    )
    return {
        "positions": [
            {
                "strategy_id": r[0],
                "symbol": r[1],
                "qty": r[2],
                "avg_entry_price": r[3],
                "opened_at": r[4],
                "closed_at": r[5],
            }
            for r in rows
        ]
    }


@router.get("/paper/decisions")
def paper_decisions(
    db_url: DatabaseUrlDep, limit: int = Query(default=100, le=2000)
) -> dict[str, Any]:
    """Decision stream INCLUDING skips — the full audit trail."""
    rows = _rows(
        db_url,
        """
        SELECT ts_utc::text, strategy_id, symbol, action, qty::float8,
               expected_move_bps, expected_cost_bps, confidence,
               reason_codes, model_version
        FROM ftr_decisions ORDER BY ts_utc DESC LIMIT %s
        """,
        (limit,),
    )
    return {
        "decisions": [
            {
                "ts_utc": r[0],
                "strategy_id": r[1],
                "symbol": r[2],
                "action": r[3],
                "qty": r[4],
                "expected_move_bps": r[5],
                "expected_cost_bps": r[6],
                "confidence": r[7],
                "reason_codes": r[8],
                "model_version": r[9],
            }
            for r in rows
        ]
    }


@router.get("/walkforward/{strategy_id}")
def walkforward(strategy_id: str, db_url: DatabaseUrlDep) -> dict[str, Any]:
    """Latest verdict metrics per profile for one strategy (stitched OOS)."""
    rows = _rows(
        db_url,
        """
        SELECT DISTINCT ON (venue_profile)
               venue_profile, verdict, metrics, n_trials, created_at::text
        FROM ftr_verdicts WHERE strategy_id = %s
        ORDER BY venue_profile, created_at DESC
        """,
        (strategy_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"no verdicts for {strategy_id!r}")
    return {
        "strategy_id": strategy_id,
        "profiles": [
            {
                "venue_profile": r[0],
                "verdict": r[1],
                "metrics": r[2],
                "n_trials": r[3],
                "created_at": r[4],
            }
            for r in rows
        ],
    }


@router.get("/cost-vs-edge/{strategy_id}")
def cost_vs_edge(strategy_id: str, db_url: DatabaseUrlDep) -> dict[str, Any]:
    """Cost vs gross-edge per profile: the binding-constraint view."""
    rows = _rows(
        db_url,
        """
        SELECT DISTINCT ON (venue_profile)
               venue_profile, verdict, metrics
        FROM ftr_verdicts WHERE strategy_id = %s
        ORDER BY venue_profile, created_at DESC
        """,
        (strategy_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"no verdicts for {strategy_id!r}")
    out = []
    for profile, verdict, metrics in rows:
        out.append(
            {
                "venue_profile": profile,
                "verdict": verdict,
                "net_total_return": metrics.get("net_total_return"),
                "gross_total_return": metrics.get("gross_total_return"),
                "cost_paid_frac": metrics.get("cost_paid_frac"),
                "cost_over_gross_edge": metrics.get("cost_over_gross_edge"),
                "trades_per_day": metrics.get("trades_per_day"),
            }
        )
    return {"strategy_id": strategy_id, "profiles": out}
