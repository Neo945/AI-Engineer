"""Unit tests for the gateway web UI route and template."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.gateway.routes import ui


def test_render_ui_substitutes_api_prefix() -> None:
    html = ui.render_ui("/api/v1")
    assert '"__API_PREFIX__"' not in html
    assert 'const API = "/api/v1";' in html
    assert ui.render_ui("/v2").count('const API = "/v2";') == 1


def test_render_ui_contains_core_elements() -> None:
    html = ui.render_ui("/api/v1")
    for marker in ("/auth/login", "/auth/register", "/workspaces", "/sessions", "/tasks"):
        assert marker in html
    assert "coding-agent" in html


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(ui.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_ui_route_serves_html(client: AsyncClient) -> None:
    response = await client.get("/ui")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "coding-agent" in response.text
    assert "__API_PREFIX__" not in response.text
