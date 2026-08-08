"""Integration tests for the repository layer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.enums import MessageRole, SessionStatus, TaskStatus
from app.database.models.message import Message
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories import (
    MessageRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    WorkspaceRepository,
)

pytestmark = pytest.mark.integration


async def _create_user(db_session: AsyncSession, *, email: str = "dev@example.com") -> User:
    return await UserRepository(db_session).add(User(email=email, full_name="Dev User"))


async def _create_workspace(
    db_session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    name: str = "toy-repo",
) -> Workspace:
    return await WorkspaceRepository(db_session).add(
        Workspace(owner_id=owner_id, name=name, repo_path=f"/workspaces/{name}")
    )


async def _create_session(
    db_session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Session:
    return await SessionRepository(db_session).add(
        Session(workspace_id=workspace_id, user_id=user_id, title="Fix bug")
    )


async def test_user_repository_crud(db_session: AsyncSession) -> None:
    """A user can be created, looked up, listed, and deleted."""
    repo = UserRepository(db_session)
    user = await repo.add(User(email="alice@example.com", full_name="Alice"))
    assert user.id is not None
    assert user.is_active is True

    fetched = await repo.get(user.id)
    assert fetched is not None and fetched.email == "alice@example.com"

    by_email = await repo.get_by_email("alice@example.com")
    assert by_email is not None and by_email.id == user.id

    active = await repo.list_active()
    assert any(u.id == user.id for u in active)

    await repo.delete(user)
    assert await repo.get(user.id) is None


async def test_email_is_unique(db_session: AsyncSession) -> None:
    """Inserting a duplicate email must violate the unique index."""
    repo = UserRepository(db_session)
    await repo.add(User(email="dup@example.com"))
    with pytest.raises(IntegrityError):
        await repo.add(User(email="dup@example.com"))


async def test_workspace_and_session_relationship(db_session: AsyncSession) -> None:
    """Workspaces list by owner; sessions list by workspace."""
    user = await _create_user(db_session)
    ws = await _create_workspace(db_session, user.id)
    assert ws.id is not None

    owned = await WorkspaceRepository(db_session).list_by_owner(user.id)
    assert [w.id for w in owned] == [ws.id]

    session = await _create_session(db_session, ws.id, user.id)
    assert session.status == SessionStatus.IDLE
    assert session.meta == {}

    sessions = await SessionRepository(db_session).list_by_workspace(ws.id)
    assert [s.id for s in sessions] == [session.id]

    running = await SessionRepository(db_session).count_by_status(ws.id, SessionStatus.RUNNING)
    assert running == 0


async def test_task_hierarchy(db_session: AsyncSession) -> None:
    """Parent tasks own their subtask trees."""
    user = await _create_user(db_session)
    ws = await _create_workspace(db_session, user.id)
    session = await _create_session(db_session, ws.id, user.id)

    task_repo = TaskRepository(db_session)
    parent = await task_repo.add(
        Task(session_id=session.id, agent_type="planner", goal="Refactor service")
    )
    child = await task_repo.add(
        Task(
            session_id=session.id,
            parent_task_id=parent.id,
            agent_type="coder",
            goal="Implement the refactor",
        )
    )
    assert parent.status == TaskStatus.PENDING

    children = await task_repo.list_children(parent.id)
    assert [t.id for t in children] == [child.id]

    by_session = await task_repo.list_by_session(session.id)
    assert {t.id for t in by_session} == {parent.id, child.id}


async def test_message_transcript_ordering(db_session: AsyncSession) -> None:
    """Messages persist per session in chronological order."""
    user = await _create_user(db_session)
    ws = await _create_workspace(db_session, user.id)
    session = await _create_session(db_session, ws.id, user.id)

    repo = MessageRepository(db_session)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    await repo.add_many(
        [
            Message(session_id=session.id, role=MessageRole.USER, content="first", created_at=t0),
            Message(
                session_id=session.id,
                role=MessageRole.ASSISTANT,
                content="second",
                created_at=t1,
            ),
        ]
    )

    messages = await repo.list_by_session(session.id)
    assert [m.content for m in messages] == ["first", "second"]
    assert messages[0].role == MessageRole.USER
    assert messages[1].role == MessageRole.ASSISTANT


async def test_workspace_cascade_deletes_sessions(db_session: AsyncSession) -> None:
    """Deleting a workspace cascades to its sessions."""
    user = await _create_user(db_session)
    ws = await _create_workspace(db_session, user.id)
    session = await _create_session(db_session, ws.id, user.id)

    await WorkspaceRepository(db_session).delete(ws)
    assert await SessionRepository(db_session).get(session.id) is None
