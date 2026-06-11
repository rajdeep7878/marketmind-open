"""CRUD helpers for the Phase 2.1 + 2.2 tables.

Each function opens its own connection — these are called from RQ job
callables that don't share a process-wide pool. When job volume grows
enough to matter, switch to psycopg_pool here without changing the
public signatures.

Pydantic models are the public-facing types; the JSON columns are
populated from `model_dump(mode="json")` (which serializes datetimes
as ISO strings and Path to str). Reads go through `model_validate`
on the JSON column so the same validation rules apply on the way out.
"""

from __future__ import annotations

import json
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
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

_INGESTED_ADAPTER = TypeAdapter(IngestedContent)


def _connect(database_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(database_url)


def _model_to_json(model: Any) -> dict[str, Any]:
    """Serialize a Pydantic model to a JSON-safe dict.

    `mode="json"` produces ISO datetimes and string paths; `by_alias`
    isn't needed because we don't use aliases. We round-trip through
    `json.loads(model_dump_json)` to get the same shape we'd get on the
    wire — Pydantic's model_dump(mode="json") doesn't fully match in
    Path handling.
    """
    return json.loads(model.model_dump_json())


def save_content(
    database_url: str,
    content: IngestedContent,
) -> UUID:
    """Persist an `IngestedContent` and return its database id."""
    # Discriminated unions still expose `source_type` on each variant.
    payload = _model_to_json(content)
    source_type = payload["source_type"]
    # source_url is the canonical lookup key; raw_text has no URL so
    # we store NULL there.
    source_url: str | None
    if source_type == "youtube":
        source_url = payload.get("video_id")
        if source_url is not None:
            source_url = f"https://www.youtube.com/watch?v={source_url}"
    elif source_type == "article":
        source_url = payload.get("url")
    else:
        source_url = None

    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingested_content (source_type, source_url, content_json)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (source_type, source_url, Jsonb(payload)),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


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


def save_transcript(
    database_url: str,
    content_id: UUID,
    transcript: Transcript,
) -> UUID:
    payload = _model_to_json(transcript)
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transcripts (
                content_id, language, full_text, segments_json,
                duration_seconds, model_name
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(content_id),
                transcript.language,
                transcript.full_text,
                Jsonb(payload["segments"]),
                transcript.duration_seconds,
                transcript.model_name,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def fetch_transcript_for_content(
    database_url: str,
    content_id: UUID,
) -> Transcript | None:
    """Return the most recent transcript for the given content row."""
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


def fetch_transcript_with_id_for_content(
    database_url: str,
    content_id: UUID,
) -> tuple[UUID, Transcript] | None:
    """Like `fetch_transcript_for_content` but also returns the row id.

    The extract job needs the transcript_id to populate
    `extracted_strategies.transcript_id`; the API doesn't, so we keep
    the existing single-return helper alongside this one.
    """
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, language, full_text, segments_json, duration_seconds, model_name
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
        tr_id, language, full_text, segments_json, duration_seconds, model_name = row
        transcript = Transcript.model_validate(
            {
                "language": language,
                "full_text": full_text,
                "segments": segments_json,
                "duration_seconds": duration_seconds,
                "model_name": model_name,
            },
        )
        return UUID(str(tr_id)), transcript


def fetch_content_id_for_transcript(
    database_url: str,
    transcript_id: UUID,
) -> UUID | None:
    """Look up the `content_id` linked to a transcript row."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content_id FROM transcripts WHERE id = %s",
            (str(transcript_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return UUID(str(row[0]))


def fetch_transcript_by_id(
    database_url: str,
    transcript_id: UUID,
) -> Transcript | None:
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT language, full_text, segments_json, duration_seconds, model_name
            FROM transcripts WHERE id = %s
            """,
            (str(transcript_id),),
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


def save_extraction(
    database_url: str,
    transcript_id: UUID,
    result: ExtractionResult,
) -> UUID:
    """Persist a successful or refused extraction.

    Writes one row into `extracted_strategies`:
      - spec_json: the StrategySpec serialization, or null
      - warnings_json: the report's extraction-notes-like content
        (we persist the full report here so the API can reconstruct
        the ExtractionResult on read without joining additional tables)
    """
    spec_dict: dict[str, Any] | None = (
        _model_to_json(result.spec) if result.spec is not None else None
    )
    report_dict = _model_to_json(result.report)
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extracted_strategies (transcript_id, spec_json, warnings_json)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                str(transcript_id),
                Jsonb(spec_dict) if spec_dict is not None else None,
                Jsonb(report_dict),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def save_extraction_cost(
    database_url: str,
    extracted_strategy_id: UUID,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    estimated_usd: float,
) -> UUID:
    """Append one row to `extraction_costs` for the given extraction."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_costs (
                extracted_strategy_id, model, input_tokens, output_tokens,
                cached_tokens, cache_write_tokens, estimated_usd
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(extracted_strategy_id),
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                cache_write_tokens,
                estimated_usd,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def save_extraction_with_cost(
    database_url: str,
    transcript_id: UUID,
    result: ExtractionResult,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    estimated_usd: float,
) -> UUID:
    """Atomic write: persist the extraction row + its cost row in one tx.

    Replaces the previous "call save_extraction then save_extraction_cost"
    sequence in the extract_strategy job. The old shape silently lost
    cost records whenever the extraction insert raised — and that
    failure mode actually triggered in smoke-test-4 (the spec_json
    NOT NULL bug), leaving real dollars of Anthropic spend with no
    persistent audit trail.

    The cost row is the more important record of the two for accounting
    purposes — we always paid for the API call — so a single transaction
    that commits both or neither is the safer invariant.
    """
    spec_dict: dict[str, Any] | None = (
        _model_to_json(result.spec) if result.spec is not None else None
    )
    report_dict = _model_to_json(result.report)

    with _connect(database_url) as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extracted_strategies (transcript_id, spec_json, warnings_json)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                str(transcript_id),
                Jsonb(spec_dict) if spec_dict is not None else None,
                Jsonb(report_dict),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        extraction_id = UUID(str(row[0]))
        cur.execute(
            """
            INSERT INTO extraction_costs (
                extracted_strategy_id, model, input_tokens, output_tokens,
                cached_tokens, cache_write_tokens, estimated_usd
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(extraction_id),
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                cache_write_tokens,
                estimated_usd,
            ),
        )
    return extraction_id


def fetch_extraction_by_id(
    database_url: str,
    extracted_strategy_id: UUID,
) -> tuple[UUID, ExtractionResult] | None:
    """Return (transcript_id, ExtractionResult) for an extracted_strategy row."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT transcript_id, spec_json, warnings_json "
            "FROM extracted_strategies WHERE id = %s",
            (str(extracted_strategy_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        transcript_id, spec_json, report_json = row
        spec = StrategySpec.model_validate(spec_json) if spec_json else None
        report = ExtractionReport.model_validate(report_json)
        return UUID(str(transcript_id)), ExtractionResult(spec=spec, report=report)


def fetch_extraction_for_transcript(
    database_url: str,
    transcript_id: UUID,
) -> tuple[UUID, ExtractionResult] | None:
    """Return (extraction_id, ExtractionResult) for the latest extraction
    of a transcript. Used to make POST /content/{id}/extract idempotent.
    """
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, spec_json, warnings_json
            FROM extracted_strategies
            WHERE transcript_id = %s
            ORDER BY created_at DESC
            LIMIT 1
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


def list_extractions(
    database_url: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[tuple[UUID, str, ExtractionResult]]:
    """Return up to `limit` extractions, newest first.

    Returned shape per row: (extraction_id, source_url_or_empty,
    ExtractionResult). The source URL is joined from `ingested_content`
    so the list view doesn't need a second round-trip.
    """
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT es.id,
                   COALESCE(ic.source_url, ''),
                   es.spec_json,
                   es.warnings_json
            FROM extracted_strategies AS es
            JOIN transcripts AS t ON t.id = es.transcript_id
            JOIN ingested_content AS ic ON ic.id = t.content_id
            ORDER BY es.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
    out: list[tuple[UUID, str, ExtractionResult]] = []
    for ext_id, source_url, spec_json, report_json in rows:
        spec = StrategySpec.model_validate(spec_json) if spec_json else None
        report = ExtractionReport.model_validate(report_json)
        out.append((UUID(str(ext_id)), source_url, ExtractionResult(spec=spec, report=report)))
    return out


def save_backtest_result(
    database_url: str,
    *,
    strategy_id: UUID,
    start_ts: datetime,
    end_ts: datetime,
    initial_capital: float,
    result: BacktestResult,
) -> UUID:
    """Persist a BacktestResult.

    The unique index on (strategy_id, start_ts, end_ts, initial_capital)
    ensures that a duplicate insert raises `UniqueViolation`; callers
    should use `fetch_backtest_for_params` first.
    """
    payload = _model_to_json(result)
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_results (
                strategy_id, start_ts, end_ts, initial_capital, result_json,
                data_fetch_seconds, compute_seconds
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(strategy_id),
                start_ts,
                end_ts,
                initial_capital,
                Jsonb(payload),
                result.data_fetch_seconds,
                result.compute_seconds,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def fetch_backtest_result_by_id(
    database_url: str,
    backtest_id: UUID,
) -> tuple[UUID, BacktestResult, datetime] | None:
    """Return (strategy_id, BacktestResult, created_at) or None."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT strategy_id, result_json, created_at
            FROM backtest_results
            WHERE id = %s
            """,
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


def fetch_backtest_for_params(
    database_url: str,
    *,
    strategy_id: UUID,
    start_ts: datetime,
    end_ts: datetime,
    initial_capital: float,
) -> tuple[UUID, BacktestResult] | None:
    """Idempotency probe: return the existing backtest row for these
    exact params (if any), else None. The API uses this to short-circuit
    POST /strategies/{id}/backtest without re-running the engine.
    """
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


def list_backtests_for_strategy(
    database_url: str,
    strategy_id: UUID,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[tuple[UUID, BacktestResult, datetime]]:
    """List backtests for one strategy, newest first."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_json, created_at
            FROM backtest_results
            WHERE strategy_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (str(strategy_id), limit, offset),
        )
        rows = cur.fetchall()
    out: list[tuple[UUID, BacktestResult, datetime]] = []
    for backtest_id, result_json, created_at in rows:
        out.append(
            (
                UUID(str(backtest_id)),
                BacktestResult.model_validate(result_json),
                created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at,
            ),
        )
    return out


def save_overfitting_analysis(
    database_url: str,
    *,
    backtest_id: UUID,
    analysis: OverfittingAnalysis,
) -> UUID:
    """Persist an OverfittingAnalysis. Unique on backtest_id."""
    wf = _model_to_json(analysis.walk_forward)
    sw = _model_to_json(analysis.parameter_sweep)
    mc = _model_to_json(analysis.monte_carlo)
    ds = _model_to_json(analysis.deflated_sharpe)
    cs = _model_to_json(analysis.composite)
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO overfitting_analyses (
                backtest_id, walk_forward_json, parameter_sweep_json,
                monte_carlo_json, deflated_sharpe_json,
                composite_score_json, compute_seconds
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(backtest_id),
                Jsonb(wf),
                Jsonb(sw),
                Jsonb(mc),
                Jsonb(ds),
                Jsonb(cs),
                analysis.compute_seconds,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def fetch_overfitting_analysis_by_id(
    database_url: str,
    analysis_id: UUID,
) -> tuple[UUID, OverfittingAnalysis, datetime] | None:
    """Return (backtest_id, OverfittingAnalysis, created_at) or None."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT backtest_id, walk_forward_json, parameter_sweep_json,
                   monte_carlo_json, deflated_sharpe_json,
                   composite_score_json, compute_seconds, created_at
            FROM overfitting_analyses
            WHERE id = %s
            """,
            (str(analysis_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        backtest_id, wf, sw, mc, ds, cs, compute_s, created_at = row
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
            UUID(str(backtest_id)),
            analysis,
            created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at,
        )


def fetch_overfitting_analysis_for_backtest(
    database_url: str,
    backtest_id: UUID,
) -> tuple[UUID, OverfittingAnalysis] | None:
    """Idempotency probe for POST /backtests/{id}/overfitting."""
    with _connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, walk_forward_json, parameter_sweep_json,
                   monte_carlo_json, deflated_sharpe_json,
                   composite_score_json, compute_seconds
            FROM overfitting_analyses
            WHERE backtest_id = %s
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
    "fetch_backtest_for_params",
    "fetch_backtest_result_by_id",
    "fetch_content",
    "fetch_content_id_for_transcript",
    "fetch_extraction_by_id",
    "fetch_extraction_for_transcript",
    "fetch_overfitting_analysis_by_id",
    "fetch_overfitting_analysis_for_backtest",
    "fetch_transcript_by_id",
    "fetch_transcript_for_content",
    "fetch_transcript_with_id_for_content",
    "list_backtests_for_strategy",
    "list_extractions",
    "save_backtest_result",
    "save_content",
    "save_extraction",
    "save_extraction_cost",
    "save_overfitting_analysis",
    "save_transcript",
]
