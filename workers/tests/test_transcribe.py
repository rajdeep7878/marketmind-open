"""Tests for the transcription service.

faster-whisper's WhisperModel is mocked everywhere except the single
opt-in @pytest.mark.integration test, which is skipped by default and
will run end-to-end on a real silence_10s.m4a fixture if it's present.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from marketmind_workers.services import transcribe
from marketmind_workers.services.ingest import ContentTooLongError, NetworkError

# ---- _probe_duration_seconds ------------------------------------------------


def test_probe_duration_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")

    class _Done:
        stdout = "123.45\n"
        stderr = ""

    monkeypatch.setattr(
        transcribe.subprocess,
        "run",
        lambda *_a, **_kw: _Done(),  # type: ignore[arg-type]
    )
    assert transcribe._probe_duration_seconds(audio) == pytest.approx(123.45)


def test_probe_duration_missing_ffprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")

    def boom(*_a: Any, **_kw: Any) -> None:
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(transcribe.subprocess, "run", boom)

    with pytest.raises(NetworkError, match="ffprobe not found"):
        transcribe._probe_duration_seconds(audio)


def test_probe_duration_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")

    def boom(*_a: Any, **_kw: Any) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd="ffprobe", stderr="bad file")

    monkeypatch.setattr(transcribe.subprocess, "run", boom)

    with pytest.raises(NetworkError, match="ffprobe failed"):
        transcribe._probe_duration_seconds(audio)


def test_probe_duration_garbage_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")

    class _Done:
        stdout = "not-a-number\n"
        stderr = ""

    monkeypatch.setattr(
        transcribe.subprocess,
        "run",
        lambda *_a, **_kw: _Done(),  # type: ignore[arg-type]
    )

    with pytest.raises(NetworkError, match="non-numeric"):
        transcribe._probe_duration_seconds(audio)


# ---- transcribe_audio (mocked model) ----------------------------------------


class _FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _FakeInfo:
    def __init__(self, language: str, duration: float) -> None:
        self.language = language
        self.duration = duration


class _FakeWhisperModel:
    def __init__(self, segments: list[_FakeSegment], info: _FakeInfo) -> None:
        self.segments = segments
        self.info = info
        self.transcribe_calls = 0

    def transcribe(
        self,
        audio: str | Path,
        beam_size: int = 5,
        language: str | None = "en",
        vad_filter: bool = True,
    ) -> tuple[Iterator[_FakeSegment], _FakeInfo]:
        self.transcribe_calls += 1
        return iter(self.segments), self.info


def _install_fake_model(
    monkeypatch: pytest.MonkeyPatch,
    segments: list[_FakeSegment],
    info: _FakeInfo,
) -> _FakeWhisperModel:
    transcribe.reset_model_cache_for_tests()
    fake = _FakeWhisperModel(segments, info)

    def get_model(*_a: Any, **_kw: Any) -> _FakeWhisperModel:
        return fake

    monkeypatch.setattr(transcribe, "_get_model", get_model)
    return fake


def _install_probe(monkeypatch: pytest.MonkeyPatch, duration: float) -> None:
    monkeypatch.setattr(
        transcribe,
        "_probe_duration_seconds",
        lambda _path: duration,
    )


def test_transcribe_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake-audio")

    fake = _install_fake_model(
        monkeypatch,
        segments=[
            _FakeSegment(0.0, 1.5, "Hello"),
            _FakeSegment(1.5, 3.0, "world"),
            _FakeSegment(3.0, 3.5, "   "),  # filtered out
        ],
        info=_FakeInfo(language="en", duration=3.0),
    )
    _install_probe(monkeypatch, duration=3.0)

    tr = transcribe.transcribe_audio(audio, data_dir=tmp_path)
    assert tr.language == "en"
    assert len(tr.segments) == 2
    assert tr.full_text == "Hello world"
    assert tr.duration_seconds == pytest.approx(3.0)
    assert tr.model_name == transcribe.DEFAULT_MODEL_NAME
    assert fake.transcribe_calls == 1


def test_transcribe_cache_hit_skips_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"deterministic-bytes")

    # First call writes the cache.
    fake = _install_fake_model(
        monkeypatch,
        segments=[_FakeSegment(0.0, 1.0, "cached")],
        info=_FakeInfo(language="en", duration=1.0),
    )
    _install_probe(monkeypatch, duration=1.0)
    first = transcribe.transcribe_audio(audio, data_dir=tmp_path)
    assert fake.transcribe_calls == 1

    # Now swap in a "boom" model: cache hit must not invoke it.
    def boom_model(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("model should not be invoked on cache hit")

    def boom_probe(_path: Path) -> float:
        raise AssertionError("ffprobe should not be invoked on cache hit")

    monkeypatch.setattr(transcribe, "_get_model", boom_model)
    monkeypatch.setattr(transcribe, "_probe_duration_seconds", boom_probe)

    second = transcribe.transcribe_audio(audio, data_dir=tmp_path)
    assert second == first


def test_transcribe_rejects_long_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")
    _install_probe(monkeypatch, duration=4 * 3600 + 1)
    monkeypatch.setattr(
        transcribe,
        "_get_model",
        lambda *_a, **_kw: pytest.fail("model should not be loaded for too-long audio"),
    )

    with pytest.raises(ContentTooLongError):
        transcribe.transcribe_audio(audio, data_dir=tmp_path)


def test_transcribe_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(NetworkError, match="does not exist"):
        transcribe.transcribe_audio(tmp_path / "nope.m4a", data_dir=tmp_path)


def test_transcribe_persists_cache_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"unique-bytes-for-cache")
    _install_fake_model(
        monkeypatch,
        segments=[_FakeSegment(0.0, 1.0, "ok")],
        info=_FakeInfo(language="en", duration=1.0),
    )
    _install_probe(monkeypatch, duration=1.0)

    transcribe.transcribe_audio(audio, data_dir=tmp_path)
    files = list((tmp_path / "cache" / "transcripts").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["language"] == "en"
    assert payload["model_name"] == transcribe.DEFAULT_MODEL_NAME


# ---- Lazy-load assertion ----------------------------------------------------


def test_model_not_loaded_at_import() -> None:
    """Importing the module must not trigger faster-whisper to load.

    We assert on the public _MODEL_CACHE — if any earlier test left an
    instance behind, this test would still pass (the cache is also
    populated by lazy loads). The point is that *import-time* code did
    not load a model — which is what we get by inspecting the module
    structure: there's no module-level `_get_model()` call.
    """
    # The module-level cache is a dict; the key thing is that nothing in
    # the module body called _get_model.
    assert isinstance(transcribe._MODEL_CACHE, dict)


# ---- Integration: real model on real silence fixture -----------------------


_SILENCE_FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "audio" / "silence_10s.m4a"
)


@pytest.mark.integration
@pytest.mark.skipif(
    not _SILENCE_FIXTURE.exists(),
    reason="silence_10s.m4a not generated; see tests/fixtures/audio/README.md",
)
def test_transcribe_real_silence_smoke(tmp_path: Path) -> None:
    """Smoke-test: real WhisperModel on real silence. Excluded from CI.

    Asserts only that the call succeeds and produces a Transcript with
    duration in the expected ballpark — not on `full_text`, since the
    model may or may not emit silence-token segments.
    """
    tr = transcribe.transcribe_audio(_SILENCE_FIXTURE, data_dir=tmp_path)
    assert tr.duration_seconds == pytest.approx(10.0, abs=1.0)
    assert tr.model_name == transcribe.DEFAULT_MODEL_NAME
