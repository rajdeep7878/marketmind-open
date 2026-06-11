"""File-based migration runner.

Applies the `.sql` files in the migrations directory in lexicographic
order. Already-applied filenames are recorded in `_schema_migrations`
so re-runs are no-ops. The runner is intentionally tiny — no DSL, no
codegen, no down-migrations. When that becomes painful (likely Phase
3+) swap this for yoyo-migrations or alembic.

The migrations directory is resolved at import time via
`_resolve_migrations_dir()`, which prefers the wheel-bundled copy
(shipped as `marketmind_workers/_migrations/` via the hatch
force-include in workers/pyproject.toml) and falls back to the
repo-relative `infra/db/migrations/` for editable / host installs.
Without the wheel-bundle path the worker container's lookup landed
at `/opt/venv/lib/infra/db/migrations`, which doesn't exist.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any, Final

import psycopg
import structlog

log = structlog.get_logger(__name__)


def _resolve_migrations_dir() -> Path:
    """Find the migrations directory across install layouts.

    Two locations, tried in order:
      1. `marketmind_workers/_migrations/` inside the installed
         package — populated by hatch's force-include during wheel
         build. Works in Docker (uv sync --no-editable).
      2. `infra/db/migrations/` relative to the source tree — works
         for editable installs (`uv sync` from the repo root), which
         is what tests and host-side dev runs use.

    Returns whichever directory actually contains .sql files. If
    neither does, returns the option-2 path so the warning log line
    is informative.
    """
    try:
        bundled = resources.files("marketmind_workers").joinpath("_migrations")
        # files() returns a Traversable. Convert to filesystem Path
        # so the rest of the module can use Path.glob() etc. For real
        # on-disk resources, str() gives the path.
        if bundled.is_dir() and any(c.name.endswith(".sql") for c in bundled.iterdir()):
            return Path(str(bundled))
    except (FileNotFoundError, ModuleNotFoundError, AttributeError, OSError):
        # Any failure in the package-data lookup -> fall through to
        # the repo-relative path. We never want the lookup itself to
        # crash the worker.
        pass

    # `infra/db/migrations/` relative to the repo root. parents[4] is
    # the repo root because this file lives at
    # workers/src/marketmind_workers/db/migrations.py.
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "infra" / "db" / "migrations"


MIGRATIONS_DIR: Final[Path] = _resolve_migrations_dir()

_SCHEMA_TABLE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS _schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _list_migrations(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.sql"))


def _already_applied(conn: psycopg.Connection[Any]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_TABLE_DDL)  # type: ignore[arg-type]
        cur.execute("SELECT filename FROM _schema_migrations")  # type: ignore[arg-type]
        return {row[0] for row in cur.fetchall()}


def apply_migrations(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration not yet recorded as applied.

    Returns the list of filenames newly applied (may be empty). Each
    migration runs in its own transaction so a syntax error in #3
    doesn't roll back #2.
    """
    files = _list_migrations(directory)
    if not files:
        log.warning("migrations_directory_empty", directory=str(directory))
        return []

    applied: list[str] = []
    with psycopg.connect(database_url) as conn:
        existing = _already_applied(conn)
        conn.commit()
        for path in files:
            if path.name in existing:
                continue
            sql = path.read_text()
            log.info("migration_applying", filename=path.name)
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql)  # type: ignore[arg-type]
                cur.execute(
                    "INSERT INTO _schema_migrations (filename) VALUES (%s)",  # type: ignore[arg-type]
                    (path.name,),
                )
            applied.append(path.name)
            log.info("migration_applied", filename=path.name)
    return applied


__all__ = ["MIGRATIONS_DIR", "apply_migrations"]
