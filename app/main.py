"""FastAPI application factory and lifespan (startup/shutdown of shared resources)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.routes import admin, health, programs, quotes
from app.cache.redis import create_redis
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.observability.logging import configure_logging
from app.observability.middleware import RequestContextMiddleware

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = create_engine(settings)
    redis = create_redis(settings)
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = redis
    logger.info("api_started", environment=settings.environment, version=__version__)
    try:
        yield
    finally:
        await redis.aclose()
        await engine.dispose()
        logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    app = FastAPI(
        title="Loyalty Points Transfer Engine",
        version=__version__,
        description="Bi-directional point transfers between card and travel loyalty programs.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(programs.router)
    app.include_router(quotes.router)
    app.include_router(admin.router)
    return app


app = create_app()
