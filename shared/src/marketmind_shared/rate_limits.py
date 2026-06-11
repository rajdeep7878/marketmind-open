"""Shared Redis-key helpers for rate limiting and daily cost tracking.

The API enforces the limits; the worker records the spend that the cap
checks against. Both must agree on key names — defining them here is
the cheapest way to keep them aligned without one service importing
internals from another.

Keys (deliberately namespaced under `marketmind:` so they coexist with
RQ's own keys in the same Redis):
  - marketmind:ratelimit:ingest:{ip}    — per-IP /content/ingest count
  - marketmind:cost:daily:{YYYY-MM-DD}  — cumulative Anthropic spend in
                                          USD cents for the given UTC day
"""

from __future__ import annotations

from datetime import UTC, datetime

RATE_LIMIT_INGEST_PREFIX = "marketmind:ratelimit:ingest"
RATE_LIMIT_REJECTIONS_PREFIX = "marketmind:ratelimit:rejections:daily"
COST_DAILY_PREFIX = "marketmind:cost:daily"


def ingest_rate_limit_key(ip: str) -> str:
    """Redis key for the per-IP /content/ingest counter."""
    return f"{RATE_LIMIT_INGEST_PREFIX}:{ip}"


def _utc_day(now: datetime | None) -> str:
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def daily_cost_key(now: datetime | None = None) -> str:
    """Redis key for today's cumulative Anthropic spend in USD cents.

    `now` is overridable for tests; default is current UTC time.
    """
    return f"{COST_DAILY_PREFIX}:{_utc_day(now)}"


def daily_ratelimit_rejection_key(now: datetime | None = None) -> str:
    """Redis key for today's count of 429 rate-limit rejections."""
    return f"{RATE_LIMIT_REJECTIONS_PREFIX}:{_utc_day(now)}"


__all__ = [
    "COST_DAILY_PREFIX",
    "RATE_LIMIT_INGEST_PREFIX",
    "RATE_LIMIT_REJECTIONS_PREFIX",
    "daily_cost_key",
    "daily_ratelimit_rejection_key",
    "ingest_rate_limit_key",
]
