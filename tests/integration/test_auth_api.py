"""Integration tests for authentication and resource ownership.

These exercise registration, login, token handling, user-scoped workspace
and session CRUD, and the ownership checks guarding every domain resource.
They require PostgreSQL and Redis on localhost (``make up``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.workspace import Workspace
from app.database.repositories.task import TaskRepository
from app.database.repositories.user import UserRepository
from app.gateway.main import create_app

pytestmark = pytest.mark.integration

_PASSWORD = "correct-horse-battery-staple"


async def _register_token(
    client: AsyncClient,
    email: str,
    password: str = _PASSWORD,
) -> str:
    """Register a user via the API and return their access token."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Ada Lovelace"},
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    """Yield an application with its lifespan running."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield an ASGI test client bound to ``app``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_register_returns_token_and_me_works(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "Ada@Example.com", "password": _PASSWORD, "full_name": "Ada Lovelace"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 86_400
    assert body["user"]["email"] == "ada@example.com"
    assert body["user"]["full_name"] == "Ada Lovelace"
    assert body["user"]["is_active"] is True

    me = await client.get("/api/v1/auth/me", headers=_headers(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "ada@example.com"


async def test_register_rejects_invalid_email(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": _PASSWORD},
    )
    assert response.status_code == 422


async def test_register_duplicate_email_conflicts(
    client: AsyncClient,
) -> None:
    await _register_token(client, "dup@example.com")
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "dup@example.com", "password": _PASSWORD},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "email already registered"


async def test_login_success(client: AsyncClient) -> None:
    await _register_token(client, "login@example.com")
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": _PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "login@example.com"
    me = await client.get("/api/v1/auth/me", headers=_headers(body["access_token"]))
    assert me.status_code == 200


async def test_login_wrong_password(client: AsyncClient) -> None:
    await _register_token(client, "login@example.com")
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": "not-the-password"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "incorrect email or password"


async def test_login_unknown_email(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": _PASSWORD},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "incorrect email or password"


async def test_me_requires_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "not authenticated"


async def test_me_rejects_invalid_token(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me",
        headers=_headers("not.a.real.token"),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid or expired token"


async def test_workspace_and_session_crud(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    token = await _register_token(client, "crud@example.com")
    headers = _headers(token)

    created = await client.post(
        "/api/v1/workspaces",
        json={"name": "acme", "repo_url": "https://example.com/acme.git"},
        headers=headers,
    )
    assert created.status_code == 201
    workspace_id = created.json()["id"]
    assert created.json()["owner_id"]
    assert created.json()["repo_url"] == "https://example.com/acme.git"

    listing = await client.get("/api/v1/workspaces", headers=headers)
    assert listing.status_code == 200
    assert [workspace["id"] for workspace in listing.json()] == [workspace_id]

    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["name"] == "acme"

    session = await client.post(
        f"/api/v1/workspaces/{workspace_id}/sessions",
        json={"title": "Fix the parser"},
        headers=headers,
    )
    assert session.status_code == 201
    session_id = session.json()["id"]
    assert session.json()["user_id"] == created.json()["owner_id"]

    sessions = await client.get(f"/api/v1/workspaces/{workspace_id}/sessions", headers=headers)
    assert sessions.status_code == 200
    assert [item["id"] for item in sessions.json()] == [session_id]

    session_detail = await client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert session_detail.status_code == 200
    assert session_detail.json()["title"] == "Fix the parser"

    user = await UserRepository(db_session).get_by_email("crud@example.com")
    assert user is not None
    assert [workspace.name for workspace in user.workspaces] == ["acme"]


async def test_workspace_and_session_ownership_enforced(
    client: AsyncClient,
) -> None:
    token_a = await _register_token(client, "owner@example.com")
    token_b = await _register_token(client, "intruder@example.com")
    headers_a = _headers(token_a)
    headers_b = _headers(token_b)

    created = await client.post(
        "/api/v1/workspaces",
        json={"name": "owner-repo"},
        headers=headers_a,
    )
    assert created.status_code == 201
    workspace_id = created.json()["id"]

    listing_b = await client.get("/api/v1/workspaces", headers=headers_b)
    assert listing_b.status_code == 200
    assert listing_b.json() == []

    detail_b = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=headers_b)
    assert detail_b.status_code == 403
    assert detail_b.json()["detail"] == "workspace does not belong to you"

    session_b = await client.post(
        f"/api/v1/workspaces/{workspace_id}/sessions",
        json={"title": "intruder session"},
        headers=headers_b,
    )
    assert session_b.status_code == 403

    session_a = await client.post(
        f"/api/v1/workspaces/{workspace_id}/sessions",
        json={"title": "owner session"},
        headers=headers_a,
    )
    assert session_a.status_code == 201
    session_id = session_a.json()["id"]

    session_detail_b = await client.get(f"/api/v1/sessions/{session_id}", headers=headers_b)
    assert session_detail_b.status_code == 403

    task_b = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Intrude"},
        headers=headers_b,
    )
    assert task_b.status_code == 403

    task_a = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Own work"},
        headers=headers_a,
    )
    assert task_a.status_code == 503
    assert "not configured" in task_a.json()["detail"]


async def test_task_ownership_enforced(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    token_a = await _register_token(client, "task-owner@example.com")
    token_b = await _register_token(client, "task-intruder@example.com")
    headers_a = _headers(token_a)
    headers_b = _headers(token_b)

    owner = await UserRepository(db_session).get_by_email("task-owner@example.com")
    assert owner is not None
    workspace = Workspace(owner_id=owner.id, name="repo")
    db_session.add(workspace)
    await db_session.flush()
    session = Session(workspace_id=workspace.id, user_id=owner.id)
    db_session.add(session)
    await db_session.flush()
    task = await TaskRepository(db_session).add(
        Task(session_id=session.id, agent_type="coder", goal="Owner's task")
    )
    await db_session.commit()

    visible = await client.get(f"/api/v1/tasks/{task.id}", headers=headers_a)
    assert visible.status_code == 200
    assert visible.json()["goal"] == "Owner's task"

    for path in ("plan", "approve", "reject", "run", "retry", "cancel"):
        response = await client.post(
            f"/api/v1/tasks/{task.id}/{path}",
            headers=headers_b,
        )
        assert response.status_code == 403, path
        assert response.json()["detail"] == "task does not belong to you"


async def test_change_password(
    client: AsyncClient,
) -> None:
    token = await _register_token(client, "pass@example.com", password="old-password")
    headers = _headers(token)

    wrong = await client.post(
        "/api/v1/auth/password",
        json={"current_password": "wrong-password", "new_password": "new-password"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "current password is incorrect"

    changed = await client.post(
        "/api/v1/auth/password",
        json={"current_password": "old-password", "new_password": "new-password"},
        headers=headers,
    )
    assert changed.status_code == 200

    old_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "pass@example.com", "password": "old-password"},
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "pass@example.com", "password": "new-password"},
    )
    assert new_login.status_code == 200
