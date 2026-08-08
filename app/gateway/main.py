"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.container import Container
from app.core.logging import configure_logging, get_logger
from app.gateway.routes import health, tasks

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and wire the FastAPI application.

    Args:
        settings: Optional settings; defaults to the cached instance.

    Returns:
        A fully configured FastAPI application.
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_logs=resolved.json_logs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = Container.build(resolved)
        app.state.container = container
        logger.info("application_started", environment=resolved.app_env)
        yield
        await container.aclose()
        logger.info("application_stopped")

    app = FastAPI(
        title=resolved.app_name,
        version="0.1.0",
        debug=resolved.debug,
        lifespan=lifespan,
    )
    app.include_router(health.router, prefix=resolved.api_prefix)
    app.include_router(tasks.router, prefix=resolved.api_prefix)
    return app


app = create_app()
