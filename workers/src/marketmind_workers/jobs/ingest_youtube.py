"""RQ job: ingest a YouTube URL and persist the resulting content row.

Returns a dict (RQ pickles return values) with:
  - content_id (UUID as str): primary key in `ingested_content`
  - video_id, title, duration_seconds: convenience fields for the API
"""

from __future__ import annotations

from typing import Any

import structlog

from marketmind_workers.config import get_settings
from marketmind_workers.db import save_content
from marketmind_workers.services.ingest import ingest_youtube

log = structlog.get_logger(__name__)


def run(url: str) -> dict[str, Any]:
    settings = get_settings()
    log.info("ingest_youtube_starting", url=url)
    content = ingest_youtube(url, data_dir=settings.data_dir)
    content_id = save_content(str(settings.database_url), content)
    log.info(
        "ingest_youtube_complete",
        url=url,
        video_id=content.video_id,
        content_id=str(content_id),
    )
    return {
        "content_id": str(content_id),
        "video_id": content.video_id,
        "title": content.title,
        "duration_seconds": content.duration_seconds,
    }
