"""FastAPI app factory + uvicorn entry point.

Import path: `marketmind_api.main:app`. The `create_app()` factory exists
so tests can build fresh instances with custom dependency overrides.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from marketmind_api.config import get_settings
from marketmind_api.logging import configure_logging
from marketmind_api.routes.admin import router as admin_router
from marketmind_api.routes.backtests import router as backtests_router
from marketmind_api.routes.content import router as content_router
from marketmind_api.routes.ftr import router as ftr_router
from marketmind_api.routes.health import router as health_router
from marketmind_api.routes.jobs import router as jobs_router
from marketmind_api.routes.overfitting import router as overfitting_router
from marketmind_api.routes.strategies import router as strategies_router
from marketmind_api.routes.trader import router as trader_router
from marketmind_api.routes.trader_admin import router as trader_admin_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, environment=settings.environment)
    log = structlog.get_logger(__name__)
    log.info("api_starting", environment=settings.environment)
    yield
    log.info("api_stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="MarketMind AI API",
        version="0.0.1",
        # OpenAPI docs gated on non-prod by default; toggle in Phase 6.
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # CORS allow-list is env-driven via CORS_ORIGINS (comma-separated).
    # In dev the default covers `next dev` on the host; in production
    # the deployed web origin(s) must be set explicitly — leaving the
    # var unset there silently allows nothing and every browser POST
    # gets a preflight 400.
    #
    # Methods + headers are listed explicitly rather than wildcarded so
    # the policy reads as intentional rather than "I forgot to think
    # about it". Extend the lists if a new verb / header is needed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(content_router)
    app.include_router(strategies_router)
    app.include_router(backtests_router)
    app.include_router(overfitting_router)
    app.include_router(admin_router)
    app.include_router(trader_router)
    app.include_router(ftr_router)
    app.include_router(trader_admin_router)
    return app


app = create_app()
