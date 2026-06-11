"""Postgres access layer for workers.

Two responsibilities:

  - `migrations`: apply `infra/db/migrations/*.sql` against the live DB
    at worker startup. Idempotent: each migration is `CREATE ... IF NOT
    EXISTS` style, and the runner records applied filenames in a
    `_schema_migrations` table so already-applied files are skipped.
  - `repo`: read/write helpers for ingested_content / transcripts /
    extracted_strategies. Thin SQL — no ORM. Phase 3 may switch to
    SQLAlchemy core if the queries grow knottier.

The API also reads from these tables (via psycopg directly) for the
GET /content/* endpoints; that read path lives in api/.
"""

from marketmind_workers.db.migrations import (
    MIGRATIONS_DIR,
    apply_migrations,
)
from marketmind_workers.db.repo import (
    fetch_backtest_for_params,
    fetch_backtest_result_by_id,
    fetch_content,
    fetch_content_id_for_transcript,
    fetch_extraction_by_id,
    fetch_extraction_for_transcript,
    fetch_overfitting_analysis_by_id,
    fetch_overfitting_analysis_for_backtest,
    fetch_transcript_by_id,
    fetch_transcript_for_content,
    fetch_transcript_with_id_for_content,
    list_backtests_for_strategy,
    list_extractions,
    save_backtest_result,
    save_content,
    save_extraction,
    save_extraction_cost,
    save_extraction_with_cost,
    save_overfitting_analysis,
    save_transcript,
)

__all__ = [
    "MIGRATIONS_DIR",
    "apply_migrations",
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
    "save_extraction_with_cost",
    "save_overfitting_analysis",
    "save_transcript",
]
