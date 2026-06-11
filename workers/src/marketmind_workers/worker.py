"""RQ worker entrypoint.

Invoked as `python -m marketmind_workers.worker`. We don't use the `rq`
CLI directly because we want structlog set up before the worker starts
pulling jobs.
"""

from __future__ import annotations

import shutil
import sys

import structlog
from redis import Redis
from rq import Queue, Worker

from marketmind_workers.config import get_settings
from marketmind_workers.db import apply_migrations
from marketmind_workers.logging import configure_logging

# RQ resolves job callables by dotted string at execution time, so the
# worker process doesn't need to eagerly import each job module here.
# Each job's own module imports its service deps when first referenced.


def _check_ffmpeg(log: structlog.stdlib.BoundLogger) -> bool:
    """Return True if both ffmpeg and ffprobe are on PATH.

    yt-dlp uses ffmpeg to remux/convert downloaded audio; faster-whisper
    uses it (indirectly via av/torchaudio decoders) to read audio frames;
    our own transcription preflight uses ffprobe to read duration. A
    missing binary causes opaque errors deep in those libraries — fail
    fast at startup instead.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        log.info("ffmpeg_check_ok", ffmpeg=ffmpeg, ffprobe=ffprobe)
        return True
    log.error(
        "ffmpeg_missing",
        ffmpeg_present=bool(ffmpeg),
        ffprobe_present=bool(ffprobe),
        action=(
            "install ffmpeg via `brew install ffmpeg` (macOS) "
            "or `apt-get install ffmpeg` (Debian/Ubuntu)"
        ),
    )
    return False


def main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, environment=settings.environment)
    log = structlog.get_logger(__name__)

    if not _check_ffmpeg(log):
        return 1

    # Apply pending migrations before picking up jobs. Idempotent (each
    # migration is recorded in _schema_migrations) so this is safe to
    # run on every worker boot.
    try:
        applied = apply_migrations(str(settings.database_url))
        if applied:
            log.info("migrations_applied", count=len(applied), files=applied)
        else:
            log.info("migrations_up_to_date")
    except Exception:
        log.exception("migrations_failed")
        return 1

    # health_check_interval pings idle connections every 30s so that RQ's
    # minutes-long blocking BLPOP doesn't let the Docker NAT silently drop
    # the connection — that silent drop is what produced the "Redis
    # connection timeout, quitting..." restart every ~6 minutes on this
    # worker. trader_worker doesn't hit it because its in-process scheduler
    # (with_scheduler=True) does Redis ops every few seconds and keeps the
    # connection warm; this worker is scheduler-less, so it idles too long.
    redis = Redis.from_url(
        str(settings.redis_url),
        decode_responses=False,
        health_check_interval=30,
        socket_keepalive=True,
        retry_on_timeout=True,
    )
    queue = Queue(name=settings.rq_queue_name, connection=redis)

    log.info(
        "worker_starting",
        queue=settings.rq_queue_name,
        environment=settings.environment,
    )

    worker = Worker(
        queues=[queue],
        connection=redis,
        name=None,  # let RQ generate a per-process name
    )
    # `with_scheduler=False` — Phase 0 has no scheduled jobs; revisit
    # when we add periodic market-data refreshes in Phase 3.
    worker.work(with_scheduler=False, logging_level=settings.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
