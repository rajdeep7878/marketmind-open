"""Migration-runner tests.

The unit slice in this file does NOT need a real Postgres — it
monkeypatches psycopg.connect with a recording fake so the discovery
+ ordering + dedupe logic is exercised independently. The full apply
loop against real Postgres is in tests/test_db_integration.py
(marked @pytest.mark.integration).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from marketmind_workers.db import migrations

# ---- migration discovery ----------------------------------------------------


def test_migrations_directory_resolves_to_repo_root() -> None:
    assert migrations.MIGRATIONS_DIR.name == "migrations"
    assert migrations.MIGRATIONS_DIR.parent.name == "db"
    # The Phase 2.1 migration must be present.
    sql_files = sorted(p.name for p in migrations.MIGRATIONS_DIR.glob("*.sql"))
    assert "0001_phase2_content_tables.sql" in sql_files


def test_list_migrations_sorted(tmp_path: Path) -> None:
    (tmp_path / "0002.sql").write_text("-- noop")
    (tmp_path / "0001.sql").write_text("-- noop")
    (tmp_path / "ignore.txt").write_text("not a migration")
    listed = migrations._list_migrations(tmp_path)
    assert [p.name for p in listed] == ["0001.sql", "0002.sql"]


def test_list_migrations_empty_dir(tmp_path: Path) -> None:
    assert migrations._list_migrations(tmp_path) == []


def test_list_migrations_missing_dir() -> None:
    assert migrations._list_migrations(Path("/does/not/exist")) == []


# ---- apply_migrations with a fake connection -------------------------------


class _FakeCursor:
    def __init__(self, recorder: list[tuple[str, Any]]) -> None:
        self._recorder = recorder
        self._last_query: str | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self._last_query = query
        self._recorder.append((query, params))

    def fetchall(self) -> list[tuple[str]]:
        # Used only by `_already_applied`.
        return []


class _FakeTransaction:
    def __enter__(self) -> _FakeTransaction:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.calls)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_apply_migrations_applies_each_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0002_b.sql").write_text("SELECT 2;")

    fake = _FakeConn()
    monkeypatch.setattr(migrations.psycopg, "connect", lambda _url: fake)

    applied = migrations.apply_migrations("postgresql://x", directory=tmp_path)
    assert applied == ["0001_a.sql", "0002_b.sql"]

    executed_queries = [q for q, _ in fake.calls]
    # Each migration's body should have been executed.
    assert any("SELECT 1" in q for q in executed_queries)
    assert any("SELECT 2" in q for q in executed_queries)
    # And each one recorded in _schema_migrations.
    record_calls = [params for q, params in fake.calls if "INSERT INTO _schema_migrations" in q]
    assert record_calls == [("0001_a.sql",), ("0002_b.sql",)]


def test_apply_migrations_skips_already_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "0001.sql").write_text("SELECT 1;")
    (tmp_path / "0002.sql").write_text("SELECT 2;")

    class _PreappliedCursor(_FakeCursor):
        def fetchall(self) -> list[tuple[str]]:
            return [("0001.sql",)]

    class _PreappliedConn(_FakeConn):
        def cursor(self) -> _FakeCursor:
            return _PreappliedCursor(self.calls)

    fake = _PreappliedConn()
    monkeypatch.setattr(migrations.psycopg, "connect", lambda _url: fake)

    applied = migrations.apply_migrations("postgresql://x", directory=tmp_path)
    assert applied == ["0002.sql"]


def test_apply_migrations_empty_dir_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def should_not_connect(_url: str) -> Any:
        raise AssertionError("should not connect when no migrations found")

    monkeypatch.setattr(migrations.psycopg, "connect", should_not_connect)
    assert migrations.apply_migrations("postgresql://x", directory=tmp_path) == []
