"""Worker process configuration.

Mirrors api/config.py but scoped to what workers actually need. Sharing
a single Settings class across services would force every service to
declare every variable; per-service settings keeps each one minimal.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    redis_url: RedisDsn
    # Phase 2.1 brought persistence into the worker. Default to an
    # obviously-bogus URL so tests / phase-0-style smoke runs that don't
    # have postgres up still load Settings cleanly; the worker entrypoint
    # only attempts a real connection when about to apply migrations.
    database_url: PostgresDsn = Field(
        default="postgresql://marketmind:marketmind_dev@localhost:5432/marketmind",  # type: ignore[arg-type]
    )
    rq_queue_name: str = "default"
    data_dir: str = "/data"

    # Phase 2.2 will require this; safe-empty in 2.1 so a worker without
    # the key can still boot for ingest/transcribe.
    anthropic_api_key: str = ""

    # Base64-encoded Netscape-format cookies.txt for yt-dlp. Empty by
    # default (local dev runs from a residential IP and doesn't need
    # cookies). Required on Railway and other cloud hosts because
    # datacenter IPs trip YouTube's bot-detection. Rotation cadence:
    # ~2–4 weeks. See docs/deployment/env-vars.md for the export +
    # encoding command. NOTE: ingest.py reads this from os.environ
    # directly to avoid a hard import-time dependency on settings
    # construction; declaring it here keeps the env-var inventory
    # discoverable.
    youtube_cookies_b64: str = ""


@lru_cache(maxsize=1)
def get_settings() -> WorkerSettings:
    return WorkerSettings()  # type: ignore[call-arg]
