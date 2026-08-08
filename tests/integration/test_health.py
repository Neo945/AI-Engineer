"""Integration tests for health endpoints.

Requires PostgreSQL and Redis on localhost (``make up``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.gateway.main import create_app


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """Yield an ASGI test client with the application lifespan running."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.integration
async def test_healthz(client: AsyncClient) -> None:
    """The liveness endpoint always reports ok."""
    response = await client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
async def test_readyz_reports_dependencies(client: AsyncClient) -> None:
    """The readiness endpoint must reach PostgreSQL and Redis."""
    response = await client.get("/api/v1/readyz")
    body = response.json()
    assert response.status_code == 200
    assert body["checks"] == {"database": "ok", "redis": "ok"}
