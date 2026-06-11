"""IP-based rate limiting + global daily cost cap for /content/ingest.

Two independent guards run on every accepted ingest:

1. ``check_ingest_rate_limit`` — caps the number of requests a single
   IP can make within a rolling 24h window. Implemented as a Redis
   ``INCR`` against a per-IP key whose TTL is set to 24h on first
   write. Returns the remaining count for the ``X-RateLimit-Remaining``
   header. Raises 429 once the configured threshold is reached.

2. ``check_daily_cost_cap`` — reads the cumulative Anthropic spend for
   the current UTC day (written by the worker after each extraction).
   Raises 503 once spend ≥ cap. Keeps the API in defence-in-depth mode:
   the cap is a circuit breaker, not a precise prediction of the next
   call's cost.

Client IP detection prefers ``X-Forwarded-For`` (first hop) when set,
falling back to the socket peer address. The deployment is expected to
sit behind a trusted reverse proxy (Railway) that rewrites XFF; in
dev-without-a-proxy the socket address is the right answer anyway.
"""

from __future__ import annotations

import math
from typing import Annotated, Final

import structlog
from fastapi import Depends, HTTPException, Request, status
from marketmind_shared.rate_limits import (
    daily_cost_key,
    daily_ratelimit_rejection_key,
    ingest_rate_limit_key,
)
from redis import Redis

from marketmind_api.deps import RedisDep, SettingsDep

log = structlog.get_logger(__name__)

_DAY_SECONDS: Final[int] = 24 * 60 * 60

# 25h TTL on daily-aggregate counters so a key created near UTC
# midnight isn't gone the moment the new day starts.
_DAILY_AGG_TTL_SECONDS: Final[int] = 25 * 60 * 60


def _record_ratelimit_rejection(redis: Redis) -> None:
    """INCR today's 429 rejection counter; ignore Redis errors.

    This is admin-dashboard telemetry; a Redis blip here should not
    influence the rejection response the client receives.
    """
    try:
        key = daily_ratelimit_rejection_key()
        redis.incr(key)
        redis.expire(key, _DAILY_AGG_TTL_SECONDS)
    except Exception as exc:
        log.debug("ratelimit_rejection_record_failed", error=str(exc))


def client_ip(request: Request) -> str:
    """Resolve the originating IP, preferring the proxy-set XFF header."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # XFF is a comma-separated chain; the leftmost entry is the
        # closest hop to the originating client.
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    # No socket info — only happens in synthetic tests with no client.
    return "unknown"


ClientIpDep = Annotated[str, Depends(client_ip)]


class RateLimitExceeded(HTTPException):
    """429 with the shape the homepage / CLI expect."""

    def __init__(self, *, limit: int) -> None:
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit",
                "message": (
                    f"You've reached today's free limit ({limit} analyses). Come back tomorrow."
                ),
            },
            headers={"X-RateLimit-Remaining": "0"},
        )


class DailyCostCapReached(HTTPException):
    """503 raised when cumulative Anthropic spend has hit the cap."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "daily_cap_reached",
                "message": ("Daily analysis budget reached. Resets tomorrow at midnight UTC."),
            },
        )


def _ttl_for_new_window(redis: Redis, key: str) -> int:
    """Return remaining TTL in seconds; (re)apply a 24h TTL if missing."""
    ttl = redis.ttl(key)
    # redis-py types ttl() as Any. Concrete: -2 = missing, -1 = no TTL,
    # otherwise positive seconds.
    if not isinstance(ttl, int) or ttl < 0:
        redis.expire(key, _DAY_SECONDS)
        return _DAY_SECONDS
    return ttl


def consume_ingest_quota(
    redis: Redis,
    ip: str,
    *,
    limit: int,
) -> int:
    """Increment the per-IP counter and return the remaining quota.

    Raises ``RateLimitExceeded`` once the IP has crossed ``limit`` for
    the current 24h window. The check is "post-increment ≤ limit", so a
    limit of 5 allows requests numbered 1..5 and rejects #6.

    ``limit <= 0`` disables the guard entirely: no Redis write, no
    rejection, and the returned remaining is ``-1`` so the
    ``X-RateLimit-Remaining`` header reads as a sentinel rather than a
    misleading countdown. Mirrors the ``cap_gbp=0`` escape hatch on
    ``check_daily_cost_cap``.

    Returns the number of additional requests still allowed for this IP
    in the current window (always >= 0; -1 when the guard is disabled).
    """
    if limit <= 0:
        return -1
    key = ingest_rate_limit_key(ip)
    current_raw = redis.incr(key)
    # redis-py types INCR as Any; concretely an int. Defensive cast.
    current = int(current_raw)  # type: ignore[arg-type]
    if current == 1:
        # First write in this window — give it a 24h TTL.
        redis.expire(key, _DAY_SECONDS)
    else:
        _ttl_for_new_window(redis, key)

    if current > limit:
        log.info("ingest_rate_limit_exceeded", ip=ip, count=current, limit=limit)
        _record_ratelimit_rejection(redis)
        raise RateLimitExceeded(limit=limit)
    return max(limit - current, 0)


def check_daily_cost_cap(
    redis: Redis,
    *,
    cap_gbp: float,
    gbp_usd_rate: float,
) -> tuple[int, int]:
    """Raise 503 if today's USD spend ≥ the GBP cap converted to USD cents.

    Returns ``(current_cents, cap_cents)`` for diagnostic logging /
    /admin/stats. The cap is checked **before** the next request is
    accepted — accepting one more request whose cost we don't yet know
    is the trade-off. In practice the next ingest doesn't spend until
    the extract step many seconds later, so this is fine.
    """
    cap_cents = max(0, math.floor(cap_gbp * gbp_usd_rate * 100))
    key = daily_cost_key()
    raw = redis.get(key)
    # redis-py types .get() as the union ResponseT (covers async clients)
    # — at runtime on a sync client it's bytes | None. Cast through a
    # local narrowing function so pyright accepts int() below.
    current = _coerce_int(raw)
    if current >= cap_cents > 0:
        log.info("daily_cost_cap_reached", current_usd_cents=current, cap_usd_cents=cap_cents)
        raise DailyCostCapReached()
    return current, cap_cents


def _coerce_int(raw: object) -> int:
    """Convert a sync-Redis GET result (bytes | str | None) to int.

    Anything that doesn't look like an integer string (including None
    and the Awaitable types in the static union that the sync client
    never returns) collapses to 0.
    """
    if raw is None:
        return 0
    if isinstance(raw, (bytes, bytearray, memoryview, str)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return 0


def enforce_ingest_guards(
    request: Request,
    redis: RedisDep,
    settings: SettingsDep,
    ip: ClientIpDep,
) -> int:
    """FastAPI dependency: rate-limit the IP and check the daily cost cap.

    Returns the remaining-quota integer so the route can stamp the
    ``X-RateLimit-Remaining`` header on success responses.
    """
    # Cost cap first — refusing on cap is more honest than letting one
    # last request through and dropping into a "your job failed" state.
    check_daily_cost_cap(
        redis,
        cap_gbp=settings.daily_cost_cap_gbp,
        gbp_usd_rate=settings.gbp_usd_rate,
    )
    remaining = consume_ingest_quota(
        redis,
        ip,
        limit=settings.rate_limit_ingest_per_day,
    )
    # Tag the request so middleware downstream can read it if needed.
    request.state.rate_limit_remaining = remaining
    return remaining


IngestGuardDep = Annotated[int, Depends(enforce_ingest_guards)]


__all__ = [
    "ClientIpDep",
    "DailyCostCapReached",
    "IngestGuardDep",
    "RateLimitExceeded",
    "check_daily_cost_cap",
    "client_ip",
    "consume_ingest_quota",
    "enforce_ingest_guards",
]
