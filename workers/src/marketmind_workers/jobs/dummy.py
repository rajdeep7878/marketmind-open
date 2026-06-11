"""Phase 0 dummy job — proves the API -> Redis -> worker -> result loop.

Replaced in Phase 2 by real ingestion jobs (transcribe, extract, etc.).
The signature stays simple kwargs: RQ serializes them with pickle and
strict types here would only obscure where validation should live
(which is at the API boundary, against the shared Pydantic models).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def run(message: str) -> dict[str, Any]:
    log.info("dummy_job_started", message=message)
    # Brief sleep so the GET /jobs/{id} can plausibly observe "started".
    time.sleep(0.5)
    result = {
        "echoed": message,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    log.info("dummy_job_finished", message=message)
    return result
