#!/usr/bin/env python
"""Transfer demo strategies from a source Postgres to a destination.

Operational script for Phase 5.2b Option 5 — seed production with
pre-extracted demo data because YouTube bot-detection blocks fresh
extractions from Railway IPs.

Usage:
    LOCAL_DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \\
    PROD_DATABASE_URL=postgresql://USER:PW@HOST:PORT/DB \\
    uv run python scripts/transfer_demos.py [--dry-run] STRATEGY_ID [STRATEGY_ID ...]

Behaviour:
  - For each STRATEGY_ID, traces the FK chain
      extracted_strategies → transcripts → ingested_content
                          ↓
                  backtest_results → overfitting_analyses
    Reads every reachable row from the source DB.
  - Compares column lists for the five affected tables between
    source and destination; refuses to write on any mismatch.
  - Inserts rows into the destination in FK order, using
    `INSERT ... ON CONFLICT (id) DO NOTHING` — re-runs are safe.
  - `--dry-run` performs the read + schema check and reports what
    *would* be inserted; never touches the destination.

Not covered (by spec):
  - `extraction_costs` rows. Those have an FK on `extracted_strategies`
    but are purely operational telemetry (drive admin/stats spend
    totals). The homepage receipts don't depend on them, so they're
    intentionally skipped. If you ever need them, extend
    DEPENDENCIES_IN_ORDER and add a fetcher.

Safety:
  - PROD_DATABASE_URL must be set explicitly; no default. LOCAL has
    the compose default.
  - Refuses to run if the two URLs resolve to the same DSN (paranoia
    against copying-into-yourself).
  - Connection strings are masked in all stdout output — passwords
    never appear in the log.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# ---- table ordering --------------------------------------------------------

# Insert order respects FKs:
#   ingested_content   has no FKs out
#   transcripts        → ingested_content
#   extracted_strategies → transcripts
#   backtest_results   → extracted_strategies
#   overfitting_analyses → backtest_results
DEPENDENCIES_IN_ORDER: tuple[str, ...] = (
    "ingested_content",
    "transcripts",
    "extracted_strategies",
    "backtest_results",
    "overfitting_analyses",
)

# Columns whose value should be wrapped in Jsonb() before INSERT.
# Reading these from psycopg3 gives back dicts/lists; psycopg3 won't
# round-trip them back as JSONB without the explicit adapter.
_JSONB_COLUMNS_BY_TABLE: dict[str, frozenset[str]] = {
    "ingested_content": frozenset({"content_json"}),
    "transcripts": frozenset({"segments_json"}),
    "extracted_strategies": frozenset({"spec_json", "warnings_json"}),
    "backtest_results": frozenset({"result_json"}),
    "overfitting_analyses": frozenset(
        {
            "walk_forward_json",
            "parameter_sweep_json",
            "monte_carlo_json",
            "deflated_sharpe_json",
            "composite_score_json",
        }
    ),
}


# ---- bundles ---------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One row, captured as a column→value dict with its source table."""

    table: str
    data: dict[str, Any]

    @property
    def id(self) -> Any:
        return self.data["id"]


@dataclass
class TransferPlan:
    """The full set of rows we intend to write, keyed by table."""

    by_table: dict[str, list[Row]] = field(
        default_factory=lambda: {t: [] for t in DEPENDENCIES_IN_ORDER},
    )

    def add(self, row: Row) -> None:
        # Dedup by (table, id) — a single backtest can be referenced
        # by multiple overfitting analyses; without this we'd queue
        # the same row twice and lose nothing (ON CONFLICT covers it),
        # but the per-table counts would mislead.
        existing = {r.id for r in self.by_table[row.table]}
        if row.id not in existing:
            self.by_table[row.table].append(row)

    def total(self) -> int:
        return sum(len(rows) for rows in self.by_table.values())


@dataclass
class WriteReport:
    """Per-table counts after the destination INSERTs run."""

    inserted: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(DEPENDENCIES_IN_ORDER, 0),
    )
    skipped: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(DEPENDENCIES_IN_ORDER, 0),
    )


# ---- URL handling ----------------------------------------------------------


def _mask_url(dsn: str) -> str:
    """Replace any password component with `***` for logging."""
    parsed = urlparse(dsn)
    if not parsed.password:
        return dsn
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
    return urlunparse(parsed._replace(netloc=netloc))


def _same_dsn(a: str, b: str) -> bool:
    """Best-effort check that two URLs point at the same database.

    We compare hostname / port / database name. Username and password
    differences don't count — copying into the same DB with a different
    user is still a footgun.
    """
    pa, pb = urlparse(a), urlparse(b)
    return (
        (pa.hostname or "") == (pb.hostname or "")
        and (pa.port or 5432) == (pb.port or 5432)
        and (pa.path or "") == (pb.path or "")
    )


# ---- schema sanity check ---------------------------------------------------


def _columns(conn: psycopg.Connection[Any], table: str) -> list[tuple[str, str]]:
    """Return [(column_name, data_type), ...] in ordinal order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public'
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]


def _schema_diff(
    src: psycopg.Connection[Any],
    dst: psycopg.Connection[Any],
) -> list[str]:
    """Return a list of human-readable diff lines, empty if schemas match."""
    diffs: list[str] = []
    for table in DEPENDENCIES_IN_ORDER:
        src_cols = _columns(src, table)
        dst_cols = _columns(dst, table)
        if not dst_cols:
            diffs.append(f"  - {table}: missing in destination DB")
            continue
        if src_cols != dst_cols:
            src_set = dict(src_cols)
            dst_set = dict(dst_cols)
            only_src = sorted(set(src_set) - set(dst_set))
            only_dst = sorted(set(dst_set) - set(src_set))
            type_mismatch = sorted(
                c for c in set(src_set) & set(dst_set) if src_set[c] != dst_set[c]
            )
            for c in only_src:
                diffs.append(f"  - {table}.{c}: in source, missing in destination")
            for c in only_dst:
                diffs.append(f"  - {table}.{c}: in destination, missing in source")
            for c in type_mismatch:
                diffs.append(
                    f"  - {table}.{c}: type mismatch "
                    f"(source={src_set[c]}, destination={dst_set[c]})"
                )
            if not (only_src or only_dst or type_mismatch):
                # Column set equal but ordering differs — flag it because
                # INSERT-with-positional-args would have failed silently.
                diffs.append(f"  - {table}: column ordering differs between schemas")
    return diffs


# ---- read source -----------------------------------------------------------


def _fetch_one(
    conn: psycopg.Connection[Any],
    table: str,
    *,
    id_column: str,
    id_value: Any,
) -> dict[str, Any] | None:
    # Table / column names go through psycopg's sql.Identifier so they're
    # safely quoted even though they come from constants in this file.
    query = sql.SQL("SELECT * FROM {table} WHERE {col} = %s").format(
        table=sql.Identifier(table),
        col=sql.Identifier(id_column),
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (id_value,))
        row = cur.fetchone()
        return row if row else None


def _fetch_many(
    conn: psycopg.Connection[Any],
    table: str,
    *,
    fk_column: str,
    fk_value: Any,
) -> list[dict[str, Any]]:
    query = sql.SQL("SELECT * FROM {table} WHERE {col} = %s").format(
        table=sql.Identifier(table),
        col=sql.Identifier(fk_column),
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, (fk_value,))
        return list(cur.fetchall())


def _collect_chain(
    src: psycopg.Connection[Any],
    strategy_id: str,
) -> TransferPlan | None:
    """Walk the FK chain rooted at a strategy id. Returns None if the
    strategy doesn't exist in the source DB.
    """
    plan = TransferPlan()

    strat = _fetch_one(src, "extracted_strategies", id_column="id", id_value=strategy_id)
    if strat is None:
        return None

    transcript = _fetch_one(src, "transcripts", id_column="id", id_value=strat["transcript_id"])
    if transcript is None:
        # Schema declares this NOT NULL FK so it should always resolve;
        # if it doesn't, refusing rather than partially writing is safer.
        raise RuntimeError(
            f"strategy {strategy_id} references transcript "
            f"{strat['transcript_id']} which does not exist in source"
        )
    content = _fetch_one(src, "ingested_content", id_column="id", id_value=transcript["content_id"])
    if content is None:
        raise RuntimeError(
            f"transcript {transcript['id']} references content "
            f"{transcript['content_id']} which does not exist in source"
        )

    plan.add(Row("ingested_content", content))
    plan.add(Row("transcripts", transcript))
    plan.add(Row("extracted_strategies", strat))

    backtests = _fetch_many(src, "backtest_results", fk_column="strategy_id", fk_value=strategy_id)
    for bt in backtests:
        plan.add(Row("backtest_results", bt))
        analyses = _fetch_many(
            src, "overfitting_analyses", fk_column="backtest_id", fk_value=bt["id"]
        )
        for an in analyses:
            plan.add(Row("overfitting_analyses", an))

    return plan


# ---- write destination -----------------------------------------------------


def _prepared_values(row: Row) -> dict[str, Any]:
    """Wrap JSONB columns with `Jsonb(...)` for round-tripping."""
    jsonb_cols = _JSONB_COLUMNS_BY_TABLE[row.table]
    out: dict[str, Any] = {}
    for col, val in row.data.items():
        if col in jsonb_cols and val is not None:
            out[col] = Jsonb(val)
        else:
            out[col] = val
    return out


def _insert_row(conn: psycopg.Connection[Any], row: Row) -> bool:
    """Insert one row with ON CONFLICT DO NOTHING. Returns True if a row
    was inserted, False if it already existed and was skipped.
    """
    values = _prepared_values(row)
    columns = list(values.keys())
    query = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
    ).format(
        table=sql.Identifier(row.table),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        placeholders=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
    )
    with conn.cursor() as cur:
        cur.execute(query, values)
        return cur.rowcount == 1


def _write_plan(
    dst: psycopg.Connection[Any],
    plan: TransferPlan,
) -> WriteReport:
    report = WriteReport()
    # Single transaction so a failure mid-chain doesn't leave dangling
    # FKs in the destination. Idempotency is preserved: a re-run with
    # everything already present commits a no-op transaction.
    with dst.transaction():
        for table in DEPENDENCIES_IN_ORDER:
            for row in plan.by_table[table]:
                inserted = _insert_row(dst, row)
                if inserted:
                    report.inserted[table] += 1
                else:
                    report.skipped[table] += 1
    return report


# ---- CLI -------------------------------------------------------------------


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Transfer demo strategies (with their transcript / content / "
            "backtest / overfitting chain) from a source Postgres to a "
            "destination Postgres. See docs/operations/demo-transfer.md."
        ),
    )
    p.add_argument(
        "strategy_ids",
        metavar="STRATEGY_ID",
        nargs="+",
        help="One or more extracted_strategies.id UUIDs to transfer.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read source + sanity-check schema + print the plan, but do "
            "not write to the destination."
        ),
    )
    return p


def _resolve_dsns() -> tuple[str, str]:
    local = os.environ.get(
        "LOCAL_DATABASE_URL",
        "postgresql://marketmind:marketmind_dev@localhost:5432/marketmind",
    )
    prod = os.environ.get("PROD_DATABASE_URL")
    if not prod:
        print(
            "PROD_DATABASE_URL must be set (no default — set it explicitly).",
            file=sys.stderr,
        )
        sys.exit(2)
    if _same_dsn(local, prod):
        print(
            "LOCAL_DATABASE_URL and PROD_DATABASE_URL resolve to the same "
            "host/port/database — refusing to copy a database into itself.",
            file=sys.stderr,
        )
        sys.exit(2)
    return local, prod


def _print_plan_summary(strategy_id: str, plan: TransferPlan) -> None:
    print(f"\n[plan] strategy {strategy_id}:")
    for table in DEPENDENCIES_IN_ORDER:
        rows = plan.by_table[table]
        if rows:
            ids = ", ".join(str(r.id) for r in rows)
            print(f"  {table:<24} {len(rows):>2} row(s): {ids}")
        else:
            print(f"  {table:<24}  0 row(s)")


def _print_report(report: WriteReport) -> None:
    print("\n[report] destination write results:")
    print(f"  {'table':<24}  {'inserted':>10}  {'skipped':>10}")
    print(f"  {'-' * 24}  {'-' * 10}  {'-' * 10}")
    for table in DEPENDENCIES_IN_ORDER:
        print(f"  {table:<24}  {report.inserted[table]:>10}  {report.skipped[table]:>10}")


def main(argv: list[str] | None = None) -> int:
    args = _argparser().parse_args(argv)
    local_dsn, prod_dsn = _resolve_dsns()

    print(f"[source]      {_mask_url(local_dsn)}")
    print(f"[destination] {_mask_url(prod_dsn)}")
    print(f"[mode]        {'DRY RUN' if args.dry_run else 'WRITE'}")

    with psycopg.connect(local_dsn) as src, psycopg.connect(prod_dsn) as dst:
        diffs = _schema_diff(src, dst)
        if diffs:
            print("\n[schema] source and destination disagree:")
            for line in diffs:
                print(line)
            print(
                "\nRefusing to write. Apply the missing migrations to the "
                "destination first (the worker applies infra/db/migrations/*.sql "
                "at startup; run a worker against the destination, or apply "
                "the SQL files manually), then re-run."
            )
            return 1
        print("[schema] source and destination columns match for all 5 tables")

        # Build the plan first across all strategies so we can show
        # the user the full set before touching the destination.
        merged_plan = TransferPlan()
        missing: list[str] = []
        for sid in args.strategy_ids:
            plan = _collect_chain(src, sid)
            if plan is None:
                missing.append(sid)
                continue
            _print_plan_summary(sid, plan)
            for table in DEPENDENCIES_IN_ORDER:
                for row in plan.by_table[table]:
                    merged_plan.add(row)

        if missing:
            print(
                "\n[error] These strategy ids were not found in the source DB:",
                file=sys.stderr,
            )
            for sid in missing:
                print(f"  - {sid}", file=sys.stderr)
            print(
                "\nRefusing to write a partial transfer. Fix the ids and re-run.",
                file=sys.stderr,
            )
            return 1

        print(f"\n[plan] {merged_plan.total()} total row(s) across all 5 tables")

        if args.dry_run:
            print("[dry-run] No writes performed. Re-run without --dry-run to apply.")
            return 0

        report = _write_plan(dst, merged_plan)
        _print_report(report)
        print("\n[done] Transfer complete.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
