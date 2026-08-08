"""Run a single task through the agent pipeline with durable persistence."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coder import CoderAgent
from app.core.config import Settings
from app.database.models.enums import MessageRole, TaskStatus
from app.database.models.message import Message
from app.database.models.task import Task
from app.database.repositories.message import MessageRepository
from app.database.repositories.task import TaskRepository
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ToolRequest
from app.llm.protocol import LLMProvider

EventKind = Literal["started", "completed", "failed"]

TaskEventHandler = Callable[["OrchestratorEvent"], Awaitable[None] | None]
ExecutorFactory = Callable[[Path], ToolExecutor]


@dataclass(frozen=True)
class OrchestratorEvent:
    """A lifecycle event emitted while running a task.

    Attributes:
        task_id: The task this event belongs to.
        kind: One of ``started``, ``completed``, or ``failed``.
        message: The final assistant message on ``completed`` events.
        detail: The goal (``started``), final answer (``completed``), or
            formatted error (``failed``).
    """

    task_id: uuid.UUID
    kind: EventKind
    message: ChatMessage | None = None
    detail: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Orchestrator:
    """Persistence-bound runner for a single task.

    ``run_task`` owns the task's lifecycle: it flips the status to RUNNING,
    steers the goal through a :class:`CoderAgent` bound to the session's
    workspace, appends every produced message to the transcript, and
    finalizes the task with the answer, token totals, and timestamps.

    A failed run is not raised; the task is marked ``FAILED`` with its error
    captured so callers can inspect or surface the failure, keeping the run
    endpoint and polling model uniform.

    Events are delivered to the optional ``on_event`` callback (sync or
    async) so callers can stream status transitions to a UI without coupling
    the orchestrator to a transport.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMProvider,
        settings: Settings,
        on_event: TaskEventHandler | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._settings = settings
        self._on_event = on_event
        self._executor_factory = executor_factory or self._default_executor

    def _default_executor(self, workspace_dir: Path) -> ToolExecutor:
        return ToolExecutor.build(workspace_dir=workspace_dir, settings=self._settings)

    async def run_task(self, task_id: uuid.UUID) -> Task:
        """Run ``task_id`` to completion and return its final state."""
        async with self._session_factory() as session:
            task = await TaskRepository(session).get_for_run(task_id)
            if task is None:
                raise ValueError(f"no such task: {task_id}")

            workspace_dir = Path(task.session.workspace.repo_path)
            await self._emit(
                OrchestratorEvent(task_id=task.id, kind="started", detail=task.goal),
            )

            task.status = TaskStatus.RUNNING
            task.started_at = _utcnow()
            await session.commit()

            executor = self._executor_factory(workspace_dir)
            agent = CoderAgent(
                llm=self._llm,
                executor=executor,
                max_tokens=self._settings.llm_max_tokens,
                temperature=self._settings.llm_temperature,
            )
            try:
                result = await agent.run(task.goal)
            except Exception as exc:
                return await self._fail(session, task, exc)

            await self._persist_transcript(session, task, result.messages)
            task.status = TaskStatus.COMPLETED
            task.result = result.answer
            task.input_tokens = result.input_tokens
            task.output_tokens = result.output_tokens
            task.finished_at = _utcnow()
            await session.commit()

            await self._emit(
                OrchestratorEvent(
                    task_id=task.id,
                    kind="completed",
                    message=result.messages[-1] if result.messages else None,
                    detail=result.answer,
                ),
            )
            return task

    async def _fail(self, session: AsyncSession, task: Task, exc: Exception) -> Task:
        task.status = TaskStatus.FAILED
        task.error = f"{type(exc).__name__}: {exc}"
        task.finished_at = _utcnow()
        await session.commit()
        await self._emit(
            OrchestratorEvent(task_id=task.id, kind="failed", detail=task.error),
        )
        return task

    async def _persist_transcript(
        self,
        session: AsyncSession,
        task: Task,
        messages: Sequence[ChatMessage],
    ) -> None:
        repository = MessageRepository(session)
        ordinal = await repository.max_ordinal(task.session_id)
        rows = [
            Message(
                session_id=task.session_id,
                task_id=task.id,
                role=MessageRole(message.role.value),
                content=message.content,
                ordinal=ordinal + index + 1,
                tool_call_id=message.tool_call_id,
                tool_calls=self._serialize_tool_calls(message.tool_requests),
            )
            for index, message in enumerate(messages)
        ]
        await repository.add_many(rows)

    @staticmethod
    def _serialize_tool_calls(
        tool_requests: Sequence[ToolRequest],
    ) -> list[dict[str, Any]] | None:
        calls = [request.model_dump() for request in tool_requests]
        return calls or None

    async def _emit(self, event: OrchestratorEvent) -> None:
        if self._on_event is None:
            return
        result = self._on_event(event)
        if result is not None:
            await result
