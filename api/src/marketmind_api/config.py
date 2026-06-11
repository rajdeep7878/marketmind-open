"""Runtime configuration loaded from environment variables.

pydantic-settings reads from process env (and optionally a .env file at
the repo root in local dev — Docker injects vars directly). Strict types
mean a malformed REDIS_URL will fail at startup, not on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    api_host: str = "0.0.0.0"  # noqa: S104  # binding to all interfaces is intentional inside containers
    api_port: int = 8000

    database_url: PostgresDsn
    redis_url: RedisDsn

    data_dir: str = "/data"

    # Required from Phase 2 onward; allowed empty in Phase 0 so the
    # skeleton can boot without it. Validate non-empty when used.
    anthropic_api_key: str = Field(default="")

    # The RQ queue name workers listen on. Single queue in Phase 0;
    # we'll split by job kind once we have priority differences.
    rq_queue_name: str = "default"

    # --- Phase 5.2a: budget guards --------------------------------------
    # Per-IP limit on POST /content/ingest within a rolling 24h window.
    # 5 is a soft public floor — high enough that anyone evaluating the
    # tool can run a couple of analyses, low enough that a single
    # visitor can't burn the daily Anthropic budget themselves. Set to
    # 0 to disable the guard entirely (intended for local single-user
    # dev; do NOT set 0 in any deployed environment that takes traffic
    # from untrusted IPs).
    rate_limit_ingest_per_day: int = 5

    # Hard daily cap on Anthropic spend, in GBP. Once cumulative spend
    # for the current UTC day hits this number, /content/ingest starts
    # returning 503 until UTC midnight. Override per-environment.
    daily_cost_cap_gbp: float = 5.0

    # The Anthropic SDK reports USD; the cap is in GBP. We convert via
    # a fixed env-configurable rate rather than calling a live FX API:
    # the rate doesn't need to be precise (the cap is a budget guard,
    # not an accounting line) and a hard-coded fallback removes a
    # network dependency. Default ~ Bank of England mid-rate.
    gbp_usd_rate: float = 1.27

    # Admin /admin/stats credentials (HTTP basic). Both must be set
    # explicitly in any non-dev environment — empty values disable the
    # admin surface entirely (route returns 503).
    admin_username: str = ""
    admin_password: str = ""

    # Comma-separated CORS allow-list. Production sets this to the
    # deployed web origin(s); local default covers `next dev` on the
    # host and the same-origin case where the SDK / curl talks to the
    # API directly. Empty entries are dropped by `cors_origins_list()`.
    cors_origins: str = "http://localhost:3000,http://localhost:8000"

    def cors_origins_list(self) -> list[str]:
        """Split CORS_ORIGINS on commas; trim whitespace; drop empties."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # pydantic-settings reads env at runtime
