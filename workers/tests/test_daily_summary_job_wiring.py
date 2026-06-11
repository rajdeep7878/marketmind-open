"""Daily summary — 00:05 tick wiring (unit, monkeypatched).

`tick_summary_daily` now generates the structured report alongside the
existing one-line activity-feed alert. These tests confirm the report
generation is wired in *and* is best-effort — a generation failure must
not stop the alert or the re-enqueue that keeps the tick alive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from marketmind_workers.trader import jobs


class _FakeSettings:
    database_url = "postgresql://test/db"


class _FakeQueue:
    def enqueue_at(self, *args: Any, **kwargs: Any) -> None:
        return None


def _patch_common(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]],
) -> None:
    def _settings() -> _FakeSettings:
        return _FakeSettings()

    def _build(_db: str) -> str:
        return "one-line body"

    def _emit(*_args: Any, **kwargs: Any) -> None:
        calls.append(("alert", kwargs.get("subject")))

    def _queue(_settings_arg: object) -> _FakeQueue:
        return _FakeQueue()

    monkeypatch.setattr(jobs, "get_trader_settings", _settings)
    monkeypatch.setattr(jobs, "_build_daily_summary", _build)
    monkeypatch.setattr(jobs, "_emit_alert", _emit)
    monkeypatch.setattr(jobs, "_connect_queue", _queue)


def test_tick_summary_daily_generates_report_and_emits_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    _patch_common(monkeypatch, calls)

    def _generate(_db: str, _now: datetime) -> tuple[None, None, None]:
        calls.append(("report", None))
        return None, None, None

    monkeypatch.setattr(jobs, "generate_and_write", _generate)
    jobs.tick_summary_daily()

    # Both happen: the structured report AND the activity-feed alert.
    assert ("report", None) in calls
    assert ("alert", "Daily summary") in calls


def test_tick_summary_daily_survives_report_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    _patch_common(monkeypatch, calls)

    def _boom(_db: str, _now: datetime) -> tuple[None, None, None]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(jobs, "generate_and_write", _boom)

    # Must not raise — the report step is best-effort.
    jobs.tick_summary_daily()

    # The activity-feed alert is still emitted despite the report failure.
    assert ("alert", "Daily summary") in calls
