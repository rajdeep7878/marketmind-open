"""RQ job: persist a raw-text submission as an ingested_content row.

No network, no audio, no model — this is the simplest of the four
ingestion paths. We still route it through the queue so the caller's
polling model is uniform (POST /content/ingest always returns a job
id; caller fetches GET /jobs/{id} for the result).

A synthetic `transcripts` row is also written, mirroring the article
ingest path: the submitted text IS the "transcript" for downstream
extraction. `model_name="raw_text"` distinguishes it from YouTube
faster-whisper output and article trafilatura output.
"""

from __future__ import annotations

from typing import Any

import structlog
from marketmind_shared.schemas import Transcript

from marketmind_workers.config import get_settings
from marketmind_workers.db import save_content, save_transcript
from marketmind_workers.services.ingest import ingest_raw_text

log = structlog.get_logger(__name__)


def run(text: str, label: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    content = ingest_raw_text(text, label=label)
    database_url = str(settings.database_url)
    content_id = save_content(database_url, content)
    transcript = Transcript(
        language="en",
        full_text=content.text,
        segments=[],
        duration_seconds=1.0,
        model_name="raw_text",
    )
    transcript_id = save_transcript(database_url, content_id, transcript)
    log.info(
        "ingest_raw_text_complete",
        content_id=str(content_id),
        transcript_id=str(transcript_id),
        chars=len(content.text),
    )
    return {
        "content_id": str(content_id),
        "label": content.label,
        "chars": len(content.text),
    }
