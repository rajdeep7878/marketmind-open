"""RQ job: ingest an article URL and persist the resulting content row.

Also writes a synthetic `transcripts` row carrying the article body
as `full_text` so the existing extract pipeline (which is keyed on
transcript_id) works without a separate transcribe step. Articles
have no audio to transcribe; the trafilatura output is the
"transcript" for downstream purposes. `model_name="trafilatura"`
serves as the provenance marker that distinguishes article-derived
transcripts from faster-whisper YouTube transcripts.
"""

from __future__ import annotations

from typing import Any

import structlog
from marketmind_shared.schemas import Transcript

from marketmind_workers.config import get_settings
from marketmind_workers.db import save_content, save_transcript
from marketmind_workers.services.ingest import ingest_article

log = structlog.get_logger(__name__)


def run(url: str) -> dict[str, Any]:
    settings = get_settings()
    log.info("ingest_article_starting", url=url)
    content = ingest_article(url, data_dir=settings.data_dir)
    database_url = str(settings.database_url)
    content_id = save_content(database_url, content)
    # `duration_seconds=1.0` is a synthetic value — articles have no
    # audio duration. The Transcript schema enforces `gt=0.0`, so we
    # pick the smallest valid sentinel. Consumers of the transcript
    # (the extraction service) only read `full_text`; the duration is
    # surfaced in `/trader/strategies` views but is meaningless here.
    transcript = Transcript(
        language="en",
        full_text=content.text,
        segments=[],
        duration_seconds=1.0,
        model_name="trafilatura",
    )
    transcript_id = save_transcript(database_url, content_id, transcript)
    log.info(
        "ingest_article_complete",
        url=url,
        content_id=str(content_id),
        transcript_id=str(transcript_id),
        chars=len(content.text),
    )
    return {
        "content_id": str(content_id),
        "url": content.url,
        "title": content.title,
        "chars": len(content.text),
    }
