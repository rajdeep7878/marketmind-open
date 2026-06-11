"""RQ job: run LLM extraction against a previously-transcribed item.

Inputs:
  - transcript_id (uuid str): row id in `transcripts`

Outputs (return dict):
  - extraction_id (uuid str): row id in `extracted_strategies`
  - verdict (str): the ExtractionVerdict value
  - estimated_usd (float): per-extraction cost

Side effects:
  - Persists the extraction to `extracted_strategies`
  - Appends a row to `extraction_costs` with token + dollar usage
  - If an extraction already exists for this transcript, returns the
    existing id without re-extracting (idempotent path)
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

import structlog
from marketmind_shared.rate_limits import daily_cost_key
from marketmind_shared.schemas.content import ExtractionInput, YouTubeContent
from redis import Redis

from marketmind_workers.config import get_settings
from marketmind_workers.db import (
    fetch_content,
    fetch_content_id_for_transcript,
    fetch_extraction_for_transcript,
    fetch_transcript_by_id,
    save_extraction_with_cost,
)
from marketmind_workers.services.extract import extract_strategy

log = structlog.get_logger(__name__)

# 25-hour TTL on the daily-cost key — slightly longer than 24h so a key
# created near UTC midnight isn't briefly missing while the new day's
# key is being created. The API treats `None` as 0 anyway, so this is
# belt-and-braces.
_DAILY_COST_TTL_SECONDS = 25 * 60 * 60


def _record_daily_cost(redis_url: str, estimated_usd: float) -> None:
    """Increment the per-day Anthropic spend counter in Redis.

    Stored in USD cents to keep INCRBY integer-safe. The API reads this
    counter on every /content/ingest to decide whether to refuse new
    work. Failure here is logged but never raised — the extraction
    itself already succeeded and we don't want a Redis hiccup to fail
    the job.
    """
    if estimated_usd <= 0:
        return
    cents = max(1, math.ceil(estimated_usd * 100))
    try:
        redis = Redis.from_url(redis_url, decode_responses=False)
        key = daily_cost_key()
        new_total = redis.incrby(key, cents)
        # Set TTL only on first write — TTL is reset on second write
        # too, but always to the same 25h, so that's harmless.
        redis.expire(key, _DAILY_COST_TTL_SECONDS)
        log.info(
            "daily_cost_recorded",
            estimated_usd=estimated_usd,
            cents_added=cents,
            new_total_cents=int(new_total),  # type: ignore[arg-type]
        )
    except Exception as exc:
        log.warning(
            "daily_cost_record_failed",
            estimated_usd=estimated_usd,
            error=str(exc),
        )


def run(transcript_id: str) -> dict[str, Any]:
    settings = get_settings()
    database_url = str(settings.database_url)
    tr_id = UUID(transcript_id)

    log.info("extract_strategy_starting", transcript_id=transcript_id)

    # Idempotent fast path: an extraction already exists for this transcript.
    existing = fetch_extraction_for_transcript(database_url, tr_id)
    if existing is not None:
        ext_id, result = existing
        log.info(
            "extract_strategy_cache_hit",
            transcript_id=transcript_id,
            extraction_id=str(ext_id),
            verdict=str(result.report.verdict),
        )
        return {
            "extraction_id": str(ext_id),
            "verdict": str(result.report.verdict),
            "estimated_usd": 0.0,
            "from_cache": True,
        }

    transcript = fetch_transcript_by_id(database_url, tr_id)
    if transcript is None:
        raise ValueError(f"no transcript row for id={transcript_id}")

    content_id = fetch_content_id_for_transcript(database_url, tr_id)
    if content_id is None:
        raise ValueError(f"transcript {transcript_id} has no linked ingested_content row")

    content = fetch_content(database_url, content_id)
    if content is None:
        raise ValueError(f"ingested_content {content_id} missing for transcript {transcript_id}")

    # Build the source bundle the extraction service expects.
    if isinstance(content, YouTubeContent):
        source_url = f"https://www.youtube.com/watch?v={content.video_id}"
        source_type: Any = "youtube"
    elif content.source_type == "article":
        # ArticleContent has `url`; pyright narrows via the literal discriminator.
        source_url = getattr(content, "url", "")
        source_type = "article"
    else:
        source_url = ""
        source_type = "raw_text"

    source = ExtractionInput(
        source_url=source_url,
        source_type=source_type,
        transcript=transcript,
    )

    result, usage = extract_strategy(transcript, source)
    # Single transaction: extraction row + cost row commit or fail
    # together. Two-call ordering (save_extraction then
    # save_extraction_cost) lost cost data when the extraction insert
    # raised — happened once in smoke-test-4 (spec_json NOT NULL bug).
    extraction_id = save_extraction_with_cost(
        database_url,
        tr_id,
        result,
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        estimated_usd=usage.estimated_usd,
    )

    _record_daily_cost(str(settings.redis_url), usage.estimated_usd)

    log.info(
        "extract_strategy_complete",
        transcript_id=transcript_id,
        extraction_id=str(extraction_id),
        verdict=str(result.report.verdict),
        estimated_usd=usage.estimated_usd,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
    )
    return {
        "extraction_id": str(extraction_id),
        "verdict": str(result.report.verdict),
        "estimated_usd": usage.estimated_usd,
        "from_cache": False,
    }
