"""Startup-check unit tests for the worker entrypoint.

These exercise just the ffmpeg gate via shutil.which monkeypatching;
the full worker.work() loop is exercised by the opt-in e2e test in
tests/test_e2e_dummy_job.py.
"""

from __future__ import annotations

import structlog
from marketmind_workers.worker import _check_ffmpeg


def test_check_ffmpeg_ok(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "marketmind_workers.worker.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    assert _check_ffmpeg(structlog.get_logger("test")) is True


def test_check_ffmpeg_missing_both(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("marketmind_workers.worker.shutil.which", lambda name: None)
    assert _check_ffmpeg(structlog.get_logger("test")) is False


def test_check_ffmpeg_missing_ffprobe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def which(name: str) -> str | None:
        return "/usr/bin/ffmpeg" if name == "ffmpeg" else None

    monkeypatch.setattr("marketmind_workers.worker.shutil.which", which)
    assert _check_ffmpeg(structlog.get_logger("test")) is False
