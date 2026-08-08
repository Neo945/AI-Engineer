"""Unit tests for the persistence models and enums.

These tests exercise metadata and Python-side defaults without touching the
database; repository behavior is covered by integration tests.
"""

from __future__ import annotations

import uuid

from sqlalchemy import DateTime, Uuid

from app.database.base import Base
from app.database.models import Session, Task, User, Workspace
from app.database.models.enums import MessageRole, SessionStatus, TaskStatus


def test_enum_values_are_lowercase() -> None:
    """Persisted enum values must match the migration's values."""
    assert {status.value for status in SessionStatus} == {
        "idle",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
    }
    assert TaskStatus.PENDING.value == "pending"
    assert MessageRole.ASSISTANT.value == "assistant"


def test_models_are_registered_in_metadata() -> None:
    """Every domain model must be present on the shared metadata."""
    for table_name in ("users", "workspaces", "sessions", "tasks", "messages", "code_chunks"):
        assert table_name in Base.metadata.tables


def test_uuid_primary_keys() -> None:
    """Primary keys are UUID columns with server-side defaults."""
    users = Base.metadata.tables["users"]
    id_column = users.c["id"]
    assert isinstance(id_column.type, Uuid)
    assert id_column.server_default is not None


def test_timestamps_are_timezone_aware() -> None:
    """Timestamp columns use timezone-aware datetime types."""
    users = Base.metadata.tables["users"]
    created_at = users.c["created_at"]
    assert isinstance(created_at.type, DateTime)
    assert created_at.type.timezone is True


def test_constructor_assigns_explicit_values() -> None:
    """Explicit constructor values are assigned before persistence.

    Column ``default`` values are applied at flush time, not construction
    time; those are verified by the repository integration tests.
    """
    user = User(email="a@b.dev", full_name="Ada")
    assert user.email == "a@b.dev"
    assert user.full_name == "Ada"

    workspace = Workspace(
        owner_id=uuid.uuid4(),
        name="repo",
        repo_path="/workspaces/repo",
        default_branch="trunk",
    )
    assert workspace.default_branch == "trunk"

    session = Session(workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), title="T")
    assert session.title == "T"

    task = Task(session_id=uuid.uuid4(), agent_type="coder", goal="Implement")
    assert task.agent_type == "coder"
