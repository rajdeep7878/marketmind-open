"""Parity test: every trader StrEnum's value set matches its DB CHECK constraint.

Drift between the Python enum and the DB CHECK is a silent class of
bug: a typo'd value either fails at insert time (raising a
check_violation that surfaces as a 500 in production) or — when the
mismatch is in the other direction — gets silently accepted as a
value the application code never expected. Both are bad.

The mapping below is the single source of truth for which Python
enum corresponds to which `(table, column)` CHECK. Adding a new
(StrEnum, CHECK) pair to the trader requires a new line here.

The test parses the migration SQL files at runtime rather than
querying a live Postgres so it can run as a unit test (no
testcontainers, no fixture dependency). Migration files are read
from `infra/db/migrations/` relative to the repo root, which works
for editable installs (`uv sync` at the host) — the only environment
where tests run.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Final

from marketmind_shared.schemas.trader import (
    AlertChannel,
    HealthStatus,
    LoopName,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventType,
    RunStatus,
    Severity,
    SignalKind,
    TemplateName,
)

# Resolve the migrations dir relative to this test file. parents[2]
# walks up from `workers/tests/<file>.py` to the repo root, then
# down to `infra/db/migrations/`. The migrations runner uses the
# same lookup pattern for its repo-relative fallback path.
_MIGRATIONS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "infra" / "db" / "migrations"


# (table, column) -> StrEnum class. Both `trader_risk_events.severity`
# and `trader_alerts.severity` map to the same Severity enum — the
# duplicate entry ensures both columns are checked even though they
# share a Python type. New columns require a new entry here AND a
# corresponding CHECK clause in the matching migration file.
_ENUM_TO_CHECK: Final[dict[tuple[str, str], type[StrEnum]]] = {
    ("trader_strategy_versions", "template"): TemplateName,
    ("trader_signals", "signal"): SignalKind,
    ("trader_paper_orders", "side"): OrderSide,
    ("trader_paper_orders", "order_type"): OrderType,
    ("trader_paper_orders", "status"): OrderStatus,
    ("trader_paper_positions", "side"): PositionSide,
    ("trader_paper_positions", "status"): PositionStatus,
    ("trader_risk_events", "event_type"): RiskEventType,
    ("trader_risk_events", "severity"): Severity,
    ("trader_alerts", "channel"): AlertChannel,
    ("trader_alerts", "severity"): Severity,
    ("trader_drift_metrics", "health_status"): HealthStatus,
    ("trader_bot_runs", "loop_name"): LoopName,
    ("trader_bot_runs", "status"): RunStatus,
}


def _read_trader_migrations() -> str:
    """Concatenate every trader migration — v1 (`*_trader_v1_*`) and v2
    (`*_trader_v2_*`, e.g. 0012's `spec`-template CHECK widening) — into
    one source blob, in numeric (filename) order.
    """
    return "\n".join(p.read_text() for p in sorted(_MIGRATIONS_DIR.glob("*_trader_v*_*.sql")))


_SINGLE_QUOTED: Final[re.Pattern[str]] = re.compile(r"'([^']*)'")


def _create_table_check_for(table: str, column: str, source: str) -> set[str] | None:
    """Initial CHECK from the `CREATE TABLE` block, or None if absent."""
    table_re = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\((?P<body>.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    table_match = table_re.search(source)
    if table_match is None:
        return None
    body = table_match.group("body")
    col_re = re.compile(
        rf"{re.escape(column)}\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*"
        rf"{re.escape(column)}\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
        re.IGNORECASE | re.DOTALL,
    )
    col_match = col_re.search(body)
    if col_match is None:
        return None
    return set(_SINGLE_QUOTED.findall(col_match.group("values")))


def _alter_table_replacement_check(
    table: str, column: str, source: str,
) -> set[str] | None:
    """Latest `ALTER TABLE <table> ... ADD CONSTRAINT ... CHECK (<column> IN (...))`
    in the source, or None if no such ALTER exists.

    We look for the LAST occurrence because migrations run in
    numeric order and a DROP+ADD pair replaces the previous CHECK
    with the new one; the trailing ADD is the effective set.
    """
    alter_re = re.compile(
        rf"ALTER\s+TABLE\s+{re.escape(table)}\b[^;]*?"
        rf"ADD\s+CONSTRAINT\s+\w+\s+"
        rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\((?P<values>[^)]*)\)\s*\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    last_match: re.Match[str] | None = None
    for match in alter_re.finditer(source):
        last_match = match
    if last_match is None:
        return None
    return set(_SINGLE_QUOTED.findall(last_match.group("values")))


def _check_values_for(table: str, column: str) -> set[str]:
    """Effective CHECK values for `(table, column)` after all migrations run.

    Resolution order:
      1. Locate the initial `CREATE TABLE <table>` CHECK clause.
      2. If any `ALTER TABLE <table> ... ADD CONSTRAINT ... CHECK
         (<column> IN (...))` appears later in the migration text,
         it REPLACES the initial set (Postgres DROP+ADD semantics).
      3. The last ADD wins (so a future migration 0015 could
         further extend the set).
    """
    source = _read_trader_migrations()
    initial = _create_table_check_for(table, column, source)
    replacement = _alter_table_replacement_check(table, column, source)
    if replacement is not None:
        return replacement
    if initial is None:
        raise AssertionError(f"could not locate CHECK clause for {table!r}.{column!r}")
    return initial


def test_every_mapped_check_is_locatable_in_migrations() -> None:
    """Sanity gate: the regex finds a non-empty CHECK for each mapped entry.
    A False here usually means a migration file got renamed or a CHECK
    clause's syntax drifted from `TEXT NOT NULL CHECK (col IN (...))`.
    """
    for (table, column), enum_cls in _ENUM_TO_CHECK.items():
        values = _check_values_for(table, column)
        assert values, f"{table}.{column} ({enum_cls.__name__}): CHECK clause empty / unparseable"


def test_strenum_values_match_db_check_constraints_exactly() -> None:
    """The load-bearing assertion: every StrEnum's value set equals
    the corresponding CHECK's value set. If this fails, the diff in
    the assertion message names exactly which values are out of sync.
    """
    mismatches: list[str] = []
    for (table, column), enum_cls in _ENUM_TO_CHECK.items():
        python_values: set[str] = {m.value for m in enum_cls}
        db_values: set[str] = _check_values_for(table, column)
        if python_values != db_values:
            only_python = sorted(python_values - db_values)
            only_db = sorted(db_values - python_values)
            mismatches.append(
                f"{table}.{column} ({enum_cls.__name__}): "
                f"only-in-Python={only_python}, only-in-DB={only_db}",
            )
    assert not mismatches, "StrEnum / DB CHECK drift:\n  " + "\n  ".join(mismatches)
