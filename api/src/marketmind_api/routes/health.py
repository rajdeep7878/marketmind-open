"""Health endpoint.

Separate liveness (`/health`) from readiness intentionally: `/health`
just confirms the process is alive. Phase 6 will add `/ready` that
gates on dependency availability for k8s-style orchestrators.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel
from redis.exceptions import RedisError

from marketmind_api.deps import PgPingDep, RedisDep, SettingsDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    environment: str
    postgres: Literal["ok", "down"]
    redis: Literal["ok", "down"]


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
)
def health(
    settings: SettingsDep,
    redis: RedisDep,
    pg_ok: PgPingDep,
) -> HealthResponse:
    try:
        redis_ok = bool(redis.ping())
    except RedisError:
        redis_ok = False

    overall: Literal["ok", "degraded"] = "ok" if (pg_ok and redis_ok) else "degraded"
    return HealthResponse(
        status=overall,
        environment=settings.environment,
        postgres="ok" if pg_ok else "down",
        redis="ok" if redis_ok else "down",
    )
