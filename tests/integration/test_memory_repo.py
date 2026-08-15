"""Integration tests for the memory subsystem against real PostgreSQL.

These verify the memory_entries table, the repository CRUD/recall paths, and
the service-level remember/recall/clear against the running database. They
require the local infrastructure (``make up``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.fake_llm import FakeLLM

from app.core.container import Container
from app.database.models.enums import MemoryKind
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories.memory import MemoryRepository
from app.database.repositories.task import TaskRepository
from app.database.repositories.workspace import WorkspaceRepository
from app.executor.executor import ToolExecutor
from app.llm.protocol import LLMResponse
from app.memory.service import MemoryService
from app.orchestrator.orchestrator import Orchestrator, OrchestratorEvent
from app.tools.schemas import ToolSpec

pytestmark = pytest.mark.integration


class _EmptyRegistry:
    def specs(self) -> list[ToolSpec]:
        return []


class _StubSandboxes:
    async def close(self) -> None:
        return None


class _StubExecutor:
    def __init__(self) -> None:
        self.workspace_dir = Path("/workspace")
        self.registry = _EmptyRegistry()
        self.sandboxes = _StubSandboxes()

    async def execute(self, call: object) -> object:
        raise AssertionError("no tool calls expected")


async def _seed_workspace(db_session: AsyncSession, *, name: str = "mem-repo") -> Workspace:
    user = User(email=f"mem-{uuid.uuid4()}@example.com", full_name="Memory Test")
    db_session.add(user)
    await db_session.flush()
    return await WorkspaceRepository(db_session).add(
        Workspace(owner_id=user.id, name=name, repo_path=f"/workspaces/{name}")
    )


async def test_memory_repository_crud_and_count(db_session: AsyncSession) -> None:
    workspace = await _seed_workspace(db_session)
    repo = MemoryRepository(db_session)
    base = datetime(2026, 8, 1, tzinfo=UTC)

    first = await repo.add(
        type(repo).model(
            workspace_id=workspace.id,
            kind=MemoryKind.FACT,
            content="The auth layer pins to asyncpg.",
            source="cli",
            created_at=base,
        )
    )
    second = await repo.add(
        type(repo).model(
            workspace_id=workspace.id,
            kind=MemoryKind.DECISION,
            content="We chose pytest because it is async-first.",
            source="run",
            created_at=base + timedelta(seconds=1),
        )
    )

    assert first.id is not None
    assert await repo.count_for_workspace(workspace.id) == 2
    fetched = await repo.get(first.id)
    assert fetched is not None and fetched.content == "The auth layer pins to asyncpg."
    assert str(fetched.kind) == "fact"

    listed = await repo.list_for_workspace(workspace.id)
    assert [entry.id for entry in listed] == [second.id, first.id]

    decisions = await repo.list_for_workspace(workspace.id, kind=MemoryKind.DECISION)
    assert [entry.id for entry in decisions] == [second.id]

    hits = await repo.keyword_search(workspace.id, "asyncpg")
    assert [entry.id for entry in hits] == [first.id]

    assert await repo.delete_for_workspace(workspace.id) == 2
    assert await repo.count_for_workspace(workspace.id) == 0


async def test_memory_service_remember_recall_clear(db_session: AsyncSession) -> None:
    workspace = await _seed_workspace(db_session)
    service = MemoryService.from_session(db_session)

    await service.remember(
        workspace.id,
        content="Pins to asyncpg live in requirements.txt.",
        kind=MemoryKind.DECISION,
        source="review",
    )
    await service.remember(
        workspace.id,
        content="Run tests with `make test` on the host.",
        kind=MemoryKind.PREFERENCE,
    )
    await db_session.commit()

    assert await service.count(workspace.id) == 2

    recalled = await service.recall(workspace.id, "what do we pin and why")
    assert any("asyncpg" in entry.content for entry in recalled)
    assert recalled[0].content.startswith("Pins to asyncpg")

    pref_only = await service.list(workspace.id, kind=MemoryKind.PREFERENCE)
    assert len(pref_only) == 1
    assert "make test" in pref_only[0].content

    assert await service.clear(workspace.id) == 2
    assert await service.count(workspace.id) == 0


async def test_memory_entries_are_workspace_scoped(db_session: AsyncSession) -> None:
    workspace_a = await _seed_workspace(db_session, name="repo-a")
    workspace_b = await _seed_workspace(db_session, name="repo-b")
    service = MemoryService.from_session(db_session)

    await service.remember(workspace_a.id, content="A-only fact about asyncpg.")
    await service.remember(workspace_b.id, content="B-only fact about redis.")
    await db_session.commit()

    a_recall = await service.recall(workspace_a.id, "asyncpg")
    b_recall = await service.recall(workspace_b.id, "asyncpg")
    assert len(a_recall) == 1
    assert len(b_recall) == 0


async def test_memory_semantic_search_when_backfilled(db_session: AsyncSession) -> None:
    """Semantic recall ranks the nearest entry once embeddings exist."""
    workspace = await _seed_workspace(db_session)
    repo = MemoryRepository(db_session)

    near = await repo.add(
        type(repo).model(
            workspace_id=workspace.id,
            kind=MemoryKind.FACT,
            content="asyncpg driver is pinned in auth.",
            source="cli",
            embedding=[0.1] * 1536,
        )
    )
    far = await repo.add(
        type(repo).model(
            workspace_id=workspace.id,
            kind=MemoryKind.FACT,
            content="Tests run with pytest.",
            source="cli",
            embedding=[0.9] * 1536,
        )
    )
    query = [0.1] * 1536
    hits = await repo.semantic_search(workspace.id, query)
    assert [entry.id for entry in hits] == [near.id, far.id]


async def test_orchestrator_injects_recalled_memory_into_context(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A run's initial context includes the memory block recalled for the goal."""
    user = User(email="mem-orch@example.com")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(
        owner_id=user.id,
        name="repo",
        repo_path=str(tmp_path),
    )
    db_session.add(workspace)
    await db_session.flush()
    agent_session = Session(workspace_id=workspace.id, user_id=user.id)
    db_session.add(agent_session)
    await db_session.flush()
    task = Task(session_id=agent_session.id, agent_type="coder", goal="Fix the asyncpg pin")
    db_session.add(task)
    await db_session.flush()

    service = MemoryService.from_session(db_session)
    await service.remember(
        workspace.id,
        content="The auth layer pins to asyncpg.",
        kind=MemoryKind.DECISION,
        source="review",
    )
    await db_session.commit()

    fake = FakeLLM([LLMResponse(content="Fixed.", stop_reason="end_turn", model="fake-model")])
    events: list[OrchestratorEvent] = []
    orchestrator = Orchestrator(
        session_factory=container.session_factory,
        llm=fake,
        settings=container.settings,
        on_event=lambda event: events.append(event),
        executor_factory=lambda _dir: cast(ToolExecutor, _StubExecutor()),
    )

    await orchestrator.run_task(task.id)

    assert fake.calls, "the agent should have called the LLM"
    user_contents = [m.content for m in fake.calls[0]["messages"] if m.role.value == "user"]
    assert any("Project memory relevant to this task" in content for content in user_contents)
    assert any("asyncpg" in content for content in user_contents)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
    assert done is not None and done.status.value == "completed"
