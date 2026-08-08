"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.logging import get_logger

router = APIRouter(tags=["health"])

logger = get_logger(__name__)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is alive."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness probe. Verifies PostgreSQL and Redis connectivity.

    Returns 200 only when every dependency is reachable, 503 otherwise so
    load balancers and orchestrators can drain the instance.
    """
    container = request.app.state.container
    checks: dict[str, str] = {}

    try:
        async with container.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.error("readiness_check_failed", dependency="database", error=str(exc))
        checks["database"] = "unavailable"

    try:
        await container.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.error("readiness_check_failed", dependency="redis", error=str(exc))
        checks["redis"] = "unavailable"

    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
