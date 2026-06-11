"""Daily summary — read_latest + CLI (pure, tmp-dir, no DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from marketmind_workers.observability.daily_summary import _main, read_latest
from marketmind_workers.observability.models import (
    BotHealth,
    DailySummary,
    EquitySummary,
)


def _write_fixture(out: Path, date: str) -> None:
    summary = DailySummary(
        date=date,
        generated_at=datetime(2026, 5, 22, 0, 5, tzinfo=UTC),
        bot_health=BotHealth(status="HEALTHY", heartbeat_fresh=True),
        equity=EquitySummary(),
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / f"daily-summary-{date}.json").write_text(
        json.dumps(summary.model_dump(mode="json")), encoding="utf-8",
    )


def test_read_latest_returns_none_when_empty(tmp_path: Path) -> None:
    assert read_latest(out=tmp_path) is None
    assert read_latest(out=tmp_path / "does-not-exist") is None


def test_read_latest_renders_newest(tmp_path: Path) -> None:
    _write_fixture(tmp_path, "2026-05-20")
    _write_fixture(tmp_path, "2026-05-22")
    text = read_latest(out=tmp_path)
    assert text is not None
    assert "=== MarketMind Daily Summary — 2026-05-22 ===" in text
    assert "2026-05-20" not in text


def test_cli_latest_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # output_dir() resolves to $DATA_DIR/daily-summaries.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _write_fixture(tmp_path / "daily-summaries", "2026-05-22")
    assert _main(["--latest"]) == 0
    assert "MarketMind Daily Summary — 2026-05-22" in capsys.readouterr().out


def test_cli_latest_flag_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert _main(["--latest"]) == 1
    assert "No daily summary reports found" in capsys.readouterr().err
