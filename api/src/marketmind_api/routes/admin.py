"""/admin/* — observability endpoints, gated by HTTP basic auth.

Phase 5.2a scaffolding. The page in the Next.js app at /admin/stats
calls these endpoints server-side; the same basic-auth credentials
the browser supplied get forwarded so the API still validates them
itself (defence in depth — direct API access is also gated).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import psycopg
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from marketmind_shared.rate_limits import daily_cost_key, daily_ratelimit_rejection_key
from pydantic import BaseModel, ConfigDict, Field
from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import FailedJobRegistry

from marketmind_api.config import Settings
from marketmind_api.deps import DatabaseUrlDep, QueueDep, RedisDep, SettingsDep

router = APIRouter(prefix="/admin", tags=["admin"])
log = structlog.get_logger(__name__)

_basic = HTTPBasic(realm="MarketMind admin", auto_error=False)


def _verify_admin(
    creds: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
    settings: SettingsDep,
) -> str:
    """Return the validated admin username, or raise 401/503.

    503 — admin credentials aren't configured in this environment, so
    the admin surface is disabled. Better than silently accepting any
    credentials with empty defaults.
    401 — credentials missing or wrong. Always emit
    ``WWW-Authenticate: Basic`` so the browser prompts.
    """
    if not settings.admin_username or not settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "admin_disabled", "message": "Admin credentials not configured."},
        )
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="MarketMind admin"'},
        )
    # secrets.compare_digest avoids timing leaks on the username + pw.
    user_ok = secrets.compare_digest(creds.username, settings.admin_username)
    pw_ok = secrets.compare_digest(creds.password, settings.admin_password)
    if not (user_ok and pw_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="MarketMind admin"'},
        )
    return creds.username


AdminUserDep = Annotated[str, Depends(_verify_admin)]


class _Buckets(BaseModel):
    """Three rolling windows used for every counter on the dashboard."""

    today: int = Field(description="UTC day of the request")
    week: int = Field(description="trailing 7 days")
    total: int = Field(description="all-time")


class _SpendBuckets(BaseModel):
    today_usd: float
    week_usd: float
    total_usd: float


class _ErrorItem(BaseModel):
    job_id: str
    ended_at: datetime | None
    kind: str | None
    exception: str


class _CostCap(BaseModel):
    current_usd: float
    cap_usd: float
    cap_gbp: float
    gbp_usd_rate: float
    fraction_used: float = Field(ge=0.0)


class AdminStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    submissions: _Buckets
    spend: _SpendBuckets
    cost_cap: _CostCap
    errors_24h_count: int
    recent_errors: list[_ErrorItem]
    ratelimit_rejections_today: int


# ---- Postgres helpers (read-only, single-query each) -----------------------


def _fetch_submission_buckets(database_url: str) -> tuple[int, int, int]:
    """Count rows in ``ingested_content`` for (today, past 7 days, all-time)."""
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)

    def _scalar(row: tuple[Any, ...] | None) -> int:
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM ingested_content WHERE created_at >= %s",
            (today_start,),
        )
        today = _scalar(cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) FROM ingested_content WHERE created_at >= %s",
            (week_start,),
        )
        week = _scalar(cur.fetchone())
        cur.execute("SELECT COUNT(*) FROM ingested_content")
        total = _scalar(cur.fetchone())
    return today, week, total


def _fetch_spend_buckets(database_url: str) -> tuple[float, float, float]:
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(estimated_usd), 0) FROM extraction_costs WHERE created_at >= %s",
            (today_start,),
        )
        row = cur.fetchone()
        today = float(row[0]) if row and row[0] is not None else 0.0
        cur.execute(
            "SELECT COALESCE(SUM(estimated_usd), 0) FROM extraction_costs WHERE created_at >= %s",
            (week_start,),
        )
        row = cur.fetchone()
        week = float(row[0]) if row and row[0] is not None else 0.0
        cur.execute("SELECT COALESCE(SUM(estimated_usd), 0) FROM extraction_costs")
        row = cur.fetchone()
        total = float(row[0]) if row and row[0] is not None else 0.0
    return today, week, total


# ---- RQ failed-job helpers -------------------------------------------------


def _recent_errors(
    queue: Queue,
    redis: Redis,
    *,
    limit: int,
) -> tuple[int, list[_ErrorItem]]:
    """Return (count-in-last-24h, the most-recent ``limit`` items).

    The "24h count" is derived from the same FailedJobRegistry — we
    pull all failed ids, hydrate each, and filter by ``ended_at >=
    now - 24h``. With RQ's default failed-registry TTL of a few hours
    this list stays small enough that hydrating each is cheap.
    """
    registry = FailedJobRegistry(queue=queue)
    failed_ids: list[str] = list(registry.get_job_ids())
    if not failed_ids:
        return 0, []

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    items: list[_ErrorItem] = []
    count_24h = 0
    # Newest-first; FailedJobRegistry returns in score order (oldest
    # first) so reverse.
    for job_id in reversed(failed_ids):
        try:
            job = Job.fetch(job_id, connection=redis)
        except Exception as exc:
            log.debug("recent_errors_fetch_failed", job_id=job_id, error=str(exc))
            continue
        ended_at = job.ended_at
        if ended_at is not None and ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=UTC)
        if ended_at is not None and ended_at >= cutoff:
            count_24h += 1
        if len(items) < limit:
            exc = (job.exc_info or "").strip().splitlines()
            exc_summary = exc[-1] if exc else "<no traceback recorded>"
            kind_raw = job.meta.get("marketmind:kind")
            items.append(
                _ErrorItem(
                    job_id=job.id,
                    ended_at=ended_at,
                    kind=str(kind_raw) if kind_raw is not None else None,
                    exception=exc_summary[:500],
                ),
            )
    return count_24h, items


# ---- Redis helpers ---------------------------------------------------------


def _read_int(redis: Redis, key: str) -> int:
    raw = redis.get(key)
    if raw is None:
        return 0
    if isinstance(raw, (bytes, bytearray, memoryview, str)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return 0


def _cost_cap_summary(redis: Redis, settings: Settings) -> _CostCap:
    cap_usd = settings.daily_cost_cap_gbp * settings.gbp_usd_rate
    current_cents = _read_int(redis, daily_cost_key())
    current_usd = current_cents / 100.0
    fraction = (current_usd / cap_usd) if cap_usd > 0 else 0.0
    return _CostCap(
        current_usd=round(current_usd, 4),
        cap_usd=round(cap_usd, 4),
        cap_gbp=settings.daily_cost_cap_gbp,
        gbp_usd_rate=settings.gbp_usd_rate,
        fraction_used=round(fraction, 4),
    )


# ---- endpoint --------------------------------------------------------------


@router.get("/stats", response_model=AdminStats)
def get_admin_stats(
    _admin: AdminUserDep,
    database_url: DatabaseUrlDep,
    redis: RedisDep,
    queue: QueueDep,
    settings: SettingsDep,
) -> AdminStats:
    sub_today, sub_week, sub_total = _fetch_submission_buckets(database_url)
    spend_today, spend_week, spend_total = _fetch_spend_buckets(database_url)
    err_count_24h, recent = _recent_errors(queue, redis, limit=5)
    cap = _cost_cap_summary(redis, settings)
    rl_today = _read_int(redis, daily_ratelimit_rejection_key())

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC),
        "submissions": {"today": sub_today, "week": sub_week, "total": sub_total},
        "spend": {
            "today_usd": round(spend_today, 4),
            "week_usd": round(spend_week, 4),
            "total_usd": round(spend_total, 4),
        },
        "cost_cap": cap.model_dump(),
        "errors_24h_count": err_count_24h,
        "recent_errors": [e.model_dump() for e in recent],
        "ratelimit_rejections_today": rl_today,
    }
    return AdminStats.model_validate(payload)


__all__ = ["AdminStats", "router"]
