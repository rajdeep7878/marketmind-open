"""Read-only Postgres helpers for the API.

The mirror write-helpers live in `workers/db/repo.py` because workers
own the persistence; the API only needs to read. Keeping these two
functions duplicated (rather than importing from `marketmind_workers`)
preserves the boundary established in Phase 0: the API does NOT import
worker code, so a future change in worker internals can't break the
API at import time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from marketmind_shared.schemas import (
    BacktestResult,
    ExtractionReport,
    ExtractionResult,
    OverfittingAnalysis,
    StrategySpec,
    Transcript,
)
from marketmind_shared.schemas.content import IngestedContent
from pydantic import TypeAdapter

_INGESTED_ADAPTER = TypeAdapter(IngestedContent)


def _connect(database_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(database_url)


def fetch_content(database_url: str, content_id: UUID) -> IngestedContent | None:
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content_json FROM ingested_content WHERE id = %s",
            (str(content_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _INGESTED_ADAPTER.validate_python(row[0])


def fetch_transcript_for_content(
    database_url: str,
    content_id: UUID,
) -> Transcript | None:
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT language, full_text, segments_json, duration_seconds, model_name
            FROM transcripts
            WHERE content_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(content_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        language, full_text, segments_json, duration_seconds, model_name = row
        return Transcript.model_validate(
            {
                "language": language,
                "full_text": full_text,
                "segments": segments_json,
                "duration_seconds": duration_seconds,
                "model_name": model_name,
            },
        )


def fetch_transcript_id_for_content(
    database_url: str,
    content_id: UUID,
) -> UUID | None:
    """Return the most recent transcript_id for a content row, if any."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM transcripts WHERE content_id = %s ORDER BY created_at DESC LIMIT 1",
            (str(content_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return UUID(str(row[0]))


def fetch_extraction_for_transcript(
    database_url: str,
    transcript_id: UUID,
) -> tuple[UUID, ExtractionResult] | None:
    """Return (extraction_id, ExtractionResult) for the latest extraction
    of a transcript, or None if no extraction has been persisted yet.
    """
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, spec_json, warnings_json
            FROM extracted_strategies
            WHERE transcript_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(transcript_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        extraction_id, spec_json, report_json = row
        spec = StrategySpec.model_validate(spec_json) if spec_json else None
        report = ExtractionReport.model_validate(report_json)
        return UUID(str(extraction_id)), ExtractionResult(spec=spec, report=report)


def fetch_extraction_by_id(
    database_url: str,
    extraction_id: UUID,
) -> ExtractionResult | None:
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT spec_json, warnings_json FROM extracted_strategies WHERE id = %s",
            (str(extraction_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        spec_json, report_json = row
        spec = StrategySpec.model_validate(spec_json) if spec_json else None
        report = ExtractionReport.model_validate(report_json)
        return ExtractionResult(spec=spec, report=report)


def list_extractions(
    database_url: str,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """List recent extractions, newest first.

    Returns dicts shaped for the API's StrategySummary response:
    {extraction_id, source_url, result}. Joins through transcripts to
    ingested_content for the source URL so the list view is one query.
    """
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT es.id,
                   COALESCE(ic.source_url, ''),
                   es.spec_json,
                   es.warnings_json,
                   es.created_at
            FROM extracted_strategies AS es
            JOIN transcripts AS t ON t.id = es.transcript_id
            JOIN ingested_content AS ic ON ic.id = t.content_id
            ORDER BY es.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for ext_id, source_url, spec_json, report_json, created_at in rows:
        spec = StrategySpec.model_validate(spec_json) if spec_json else None
        report = ExtractionReport.model_validate(report_json)
        out.append(
            {
                "extraction_id": UUID(str(ext_id)),
                "source_url": source_url,
                "created_at": created_at,
                "result": ExtractionResult(spec=spec, report=report),
            },
        )
    return out


def fetch_backtest_for_params(
    database_url: str,
    *,
    strategy_id: UUID,
    start_ts: datetime,
    end_ts: datetime,
    initial_capital: float,
) -> tuple[UUID, BacktestResult] | None:
    """Idempotency probe used by POST /strategies/{id}/backtest."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_json
            FROM backtest_results
            WHERE strategy_id = %s
              AND start_ts = %s
              AND end_ts = %s
              AND initial_capital = %s
            """,
            (str(strategy_id), start_ts, end_ts, initial_capital),
        )
        row = cur.fetchone()
        if row is None:
            return None
        backtest_id, result_json = row
        return UUID(str(backtest_id)), BacktestResult.model_validate(result_json)


def fetch_backtest_by_id(
    database_url: str,
    backtest_id: UUID,
) -> tuple[UUID, BacktestResult, datetime] | None:
    """Return (strategy_id, BacktestResult, created_at) for the row."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT strategy_id, result_json, created_at FROM backtest_results WHERE id = %s",
            (str(backtest_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        strategy_id, result_json, created_at = row
        return (
            UUID(str(strategy_id)),
            BacktestResult.model_validate(result_json),
            created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at,
        )


def list_backtests_for_strategy(
    database_url: str,
    strategy_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """List backtests for one strategy, newest first.

    Returns dicts shaped for BacktestSummary: id, created_at, the
    result (with full equity curve). The route downsamples the curve
    before responding so we don't ship millions of points per row.
    """
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_json, created_at, start_ts, end_ts, initial_capital
            FROM backtest_results
            WHERE strategy_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (str(strategy_id), limit, offset),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for bt_id, result_json, created_at, start_ts, end_ts, initial_capital in rows:
        result = BacktestResult.model_validate(result_json)
        out.append(
            {
                "backtest_id": UUID(str(bt_id)),
                "created_at": created_at.replace(tzinfo=UTC)
                if created_at.tzinfo is None
                else created_at,
                "start_ts": start_ts.replace(tzinfo=UTC) if start_ts.tzinfo is None else start_ts,
                "end_ts": end_ts.replace(tzinfo=UTC) if end_ts.tzinfo is None else end_ts,
                "initial_capital": initial_capital,
                "result": result,
            },
        )
    return out


def fetch_overfitting_by_id(
    database_url: str,
    analysis_id: UUID,
) -> tuple[UUID, OverfittingAnalysis, datetime] | None:
    """Return (backtest_id, OverfittingAnalysis, created_at)."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT backtest_id, walk_forward_json, parameter_sweep_json,
                   monte_carlo_json, deflated_sharpe_json,
                   composite_score_json, compute_seconds, created_at
            FROM overfitting_analyses WHERE id = %s
            """,
            (str(analysis_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        bt_id, wf, sw, mc, ds, cs, compute_s, created_at = row
        analysis = OverfittingAnalysis.model_validate(
            {
                "schema_version": "1.0",
                "walk_forward": wf,
                "parameter_sweep": sw,
                "monte_carlo": mc,
                "deflated_sharpe": ds,
                "composite": cs,
                "compute_seconds": compute_s,
            },
        )
        return (
            UUID(str(bt_id)),
            analysis,
            created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at,
        )


def fetch_overfitting_for_backtest(
    database_url: str,
    backtest_id: UUID,
) -> tuple[UUID, OverfittingAnalysis] | None:
    """Idempotency probe + UI shortcut: latest analysis for a backtest."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, walk_forward_json, parameter_sweep_json,
                   monte_carlo_json, deflated_sharpe_json,
                   composite_score_json, compute_seconds
            FROM overfitting_analyses
            WHERE backtest_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(backtest_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        aid, wf, sw, mc, ds, cs, compute_s = row
        analysis = OverfittingAnalysis.model_validate(
            {
                "schema_version": "1.0",
                "walk_forward": wf,
                "parameter_sweep": sw,
                "monte_carlo": mc,
                "deflated_sharpe": ds,
                "composite": cs,
                "compute_seconds": compute_s,
            },
        )
        return UUID(str(aid)), analysis


__all__ = [
    "fetch_backtest_by_id",
    "fetch_backtest_for_params",
    "fetch_content",
    "fetch_extraction_by_id",
    "fetch_extraction_for_transcript",
    "fetch_overfitting_by_id",
    "fetch_overfitting_for_backtest",
    "fetch_transcript_for_content",
    "fetch_transcript_id_for_content",
    "list_backtests_for_strategy",
    "list_extractions",
]
