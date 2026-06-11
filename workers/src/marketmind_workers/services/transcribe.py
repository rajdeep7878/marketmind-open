"""Audio transcription via faster-whisper.

`transcribe_audio(audio_path)` returns a fully populated `Transcript`:

  - Cached at ${DATA_DIR}/cache/transcripts/{sha256_of_audio_bytes}.json
  - Cache hit: parse and return without instantiating WhisperModel
  - Cache miss: lazy-load WhisperModel("small") on first use, transcribe,
    persist
  - Pre-flight ffprobe check rejects audio longer than 4 hours before
    invoking Whisper (model_load + transcribe of a 4h clip is ~hours
    on CPU)

WhisperModel is intentionally NOT instantiated at import time — that
would download model weights on import, which (a) blows up CI cold
start and (b) makes a `python -c "import marketmind_workers"` smoke
test a multi-GB operation.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Final, Protocol, cast

import structlog
from marketmind_shared.schemas import Transcript, TranscriptSegment

from marketmind_workers.services.ingest import ContentTooLongError, NetworkError

log = structlog.get_logger(__name__)

MAX_AUDIO_SECONDS: Final[float] = 4 * 3600.0
DEFAULT_MODEL_NAME: Final[str] = "small"


class TranscriptionError(Exception):
    """Base for transcription failures distinct from ingestion errors."""


# ---- WhisperModel lazy loader ------------------------------------------------


class _WhisperLike(Protocol):
    """Subset of faster_whisper.WhisperModel.transcribe we depend on.

    The real return is `(SegmentIterator, TranscriptionInfo)`. We only
    iterate the segments and read `language` / `duration` on info.
    Protocol gives tests a small, stable mock surface.
    """

    def transcribe(
        self,
        audio: str | Path,
        beam_size: int = ...,
        language: str | None = ...,
        vad_filter: bool = ...,
    ) -> tuple[Any, Any]: ...


# Module-level cache. Worker process loads at most one instance per
# (model_name, compute_type) combo; in 2.1 we hardcode "small" + "int8"
# so there's effectively one slot.
_MODEL_CACHE: dict[tuple[str, str], _WhisperLike] = {}


def _get_model(model_name: str = DEFAULT_MODEL_NAME, compute_type: str = "int8") -> _WhisperLike:
    """Return a process-wide singleton WhisperModel.

    First call downloads weights to faster-whisper's default cache dir
    (~/.cache/huggingface/hub by default). Subsequent calls reuse the
    instance.
    """
    key = (model_name, compute_type)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    # Late import — the heavy ctranslate2 wheel only loads when we
    # actually need it. Tests for the unit path never trigger this.
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    instance = cast(
        "_WhisperLike", WhisperModel(model_name, device="cpu", compute_type=compute_type)
    )
    _MODEL_CACHE[key] = instance
    return instance


def reset_model_cache_for_tests() -> None:
    """Test-only escape hatch — clear the per-process model cache.

    Public (no leading underscore) because pyright in strict mode flags
    module-private functions referenced only via monkeypatch as unused.
    """
    _MODEL_CACHE.clear()


# ---- ffprobe duration check -------------------------------------------------


def _probe_duration_seconds(audio_path: Path) -> float:
    """Read media duration via ffprobe. Raises NetworkError on failure.

    Returning NetworkError (not TranscriptionError) because a failed
    probe usually means the audio file is corrupt or wasn't fully
    downloaded — the producing service is the one that should retry.
    """
    # Worker startup verified ffprobe is on PATH; the partial-path warning
    # (S607) is the explicit trade-off — we want to use the user's
    # ffmpeg install rather than pinning to a single absolute path.
    try:
        out = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise NetworkError(
            "ffprobe not found on PATH; install ffmpeg (`brew install ffmpeg`)",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise NetworkError(f"ffprobe failed on {audio_path}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetworkError(f"ffprobe timed out on {audio_path}") from exc

    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise NetworkError(
            f"ffprobe returned non-numeric duration {out.stdout!r} for {audio_path}",
        ) from exc


# ---- Cache ------------------------------------------------------------------


def _sha256_of_file(path: Path, chunk_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _cache_path(data_dir: str | Path, audio_sha: str) -> Path:
    return Path(data_dir) / "cache" / "transcripts" / f"{audio_sha}.json"


def _load_cached(path: Path) -> Transcript | None:
    if not path.exists():
        return None
    try:
        return Transcript.model_validate_json(path.read_text())
    except (OSError, ValueError):
        log.warning("transcript_cache_corrupt", path=str(path))
        return None


def _persist(path: Path, transcript: Transcript) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript.model_dump_json(indent=2))


# ---- Main API --------------------------------------------------------------


def transcribe_audio(
    audio_path: Path,
    *,
    language: str = "en",
    data_dir: str | Path = "/data",
    model_name: str = DEFAULT_MODEL_NAME,
) -> Transcript:
    """Transcribe `audio_path` with the faster-whisper `small` model.

    Cache hit / pre-flight check / model invocation are layered so the
    expensive step (model load + decode) only runs when nothing cheaper
    works.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise NetworkError(f"audio file does not exist: {audio_path}")

    sha = _sha256_of_file(audio_path)
    cache_path = _cache_path(data_dir, sha)
    cached = _load_cached(cache_path)
    if cached is not None:
        log.info("transcribe_cache_hit", audio_path=str(audio_path), sha=sha[:12])
        return cached

    duration_seconds = _probe_duration_seconds(audio_path)
    if duration_seconds > MAX_AUDIO_SECONDS:
        raise ContentTooLongError(
            f"audio duration {duration_seconds:.0f}s exceeds {MAX_AUDIO_SECONDS:.0f}s ceiling",
        )
    if duration_seconds <= 0:
        raise TranscriptionError(
            f"audio file reports non-positive duration ({duration_seconds:.2f}s)",
        )

    model = _get_model(model_name=model_name)
    log.info(
        "transcribe_starting",
        audio_path=str(audio_path),
        sha=sha[:12],
        duration_seconds=duration_seconds,
        model_name=model_name,
    )
    segments_iter, info = model.transcribe(
        audio_path,
        beam_size=5,
        language=language,
        vad_filter=True,
    )

    segments: list[TranscriptSegment] = []
    pieces: list[str] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start_seconds=float(seg.start),
                end_seconds=float(seg.end),
                text=text,
            ),
        )
        pieces.append(text)

    detected_language = getattr(info, "language", None) or language
    detected_duration = float(getattr(info, "duration", duration_seconds) or duration_seconds)

    transcript = Transcript(
        language=str(detected_language),
        full_text=" ".join(pieces),
        segments=segments,
        duration_seconds=detected_duration,
        model_name=model_name,
    )

    _persist(cache_path, transcript)
    log.info("transcribe_complete", sha=sha[:12], n_segments=len(segments))
    return transcript


__all__ = [
    "DEFAULT_MODEL_NAME",
    "MAX_AUDIO_SECONDS",
    "TranscriptionError",
    "transcribe_audio",
]
