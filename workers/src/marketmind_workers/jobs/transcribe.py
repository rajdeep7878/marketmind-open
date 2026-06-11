"""RQ job: transcribe the audio belonging to a previously-ingested
YouTube content row.

The job takes a `content_id` (a UUID string from ingested_content.id)
and the language tag. It looks up the YouTubeContent JSON, transcribes
the on-disk audio file, and persists the Transcript.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from marketmind_shared.schemas import YouTubeContent

from marketmind_workers.config import get_settings
from marketmind_workers.db import fetch_content, save_transcript
from marketmind_workers.services.transcribe import transcribe_audio

log = structlog.get_logger(__name__)


def run(content_id: str, language: str = "en") -> dict[str, Any]:
    settings = get_settings()
    cid = UUID(content_id)
    log.info("transcribe_starting", content_id=content_id, language=language)

    content = fetch_content(str(settings.database_url), cid)
    if content is None:
        raise ValueError(f"no ingested_content row for id={content_id}")
    if not isinstance(content, YouTubeContent):
        raise ValueError(
            f"transcribe only supports YouTubeContent; got source_type={content.source_type}",
        )

    transcript = transcribe_audio(
        content.audio_path,
        language=language,
        data_dir=settings.data_dir,
    )
    transcript_id = save_transcript(str(settings.database_url), cid, transcript)

    log.info(
        "transcribe_complete",
        content_id=content_id,
        transcript_id=str(transcript_id),
        segments=len(transcript.segments),
    )
    return {
        "content_id": content_id,
        "transcript_id": str(transcript_id),
        "language": transcript.language,
        "duration_seconds": transcript.duration_seconds,
        "n_segments": len(transcript.segments),
    }
