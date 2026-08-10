"""Run a single task through the agent pipeline with durable persistence."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import AgentLike, TokenHandler
from app.agents.coder import CoderAgent
from app.agents.pipeline import PipelineAgent
from app.agents.planner import PlannerAgent
from app.agents.planning import TaskPlan, format_plan
from app.core.config import Settings
from app.database.models.enums import MessageRole, TaskStatus
from app.database.models.message import Message
from app.database.models.task import Task
from app.database.repositories.message import MessageRepository
from app.database.repositories.task import TaskRepository
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider
from app.orchestrator.broker import EventBroker
from app.orchestrator.cancellation import CancellationRegistry, TaskCancelled
from app.retrieval.context import ContextAssembler, format_context

EventKind = Literal[
    "started",
    "planned",
    "message",
    "approved",
    "rejected",
    "completed",
    "failed",
    "cancelled",
]

TaskEventHandler = Callable[["OrchestratorEvent"], Awaitable[None] | None]
ExecutorFactory = Callable[[Path], ToolExecutor]

_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


@dataclass(frozen=True)
class OrchestratorEvent:
    """A lifecycle event emitted while running a task.

    Attributes:
        task_id: The task this event belongs to.
        kind: ``started``, ``planned``, ``message`` (a transcript entry was
            persisted), ``approved``, ``rejected``, ``completed``, ``failed``,
            or ``cancelled``.
        message: The assistant message on ``completed`` events, or the
            persisted transcript entry on ``message`` events.
        detail: The goal (``started``), formatted plan (``planned``), final
            answer (``completed``), or formatted error (``failed``).
        ordinal: Transcript ordinal of ``message`` on ``message`` events.
    """

    task_id: uuid.UUID
    kind: EventKind
    message: ChatMessage | None = None
    detail: str | None = None
    ordinal: int | None = None


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

    Every produced message is persisted to the transcript as it is emitted
    (each commit is immediately visible to concurrent readers) and an event
    is published for it, so clients can stream a task's progress live while
    still being able to replay the transcript from the database at any time.

    Events are delivered to the optional ``on_event`` callback (sync or
    async) and/or the optional ``event_broker`` so callers can stream status
    transitions to a UI without coupling the orchestrator to a transport.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMProvider,
        settings: Settings,
        on_event: TaskEventHandler | None = None,
        on_token: TokenHandler | None = None,
        stream: bool = False,
        event_broker: EventBroker | None = None,
        cancellations: CancellationRegistry | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._settings = settings
        self._on_event = on_event
        self._on_token = on_token
        self._stream = stream
        self._event_broker = event_broker
        self._cancellations = cancellations
        self._executor_factory = executor_factory or self._default_executor

    def _default_executor(self, workspace_dir: Path) -> ToolExecutor:
        return ToolExecutor.build(workspace_dir=workspace_dir, settings=self._settings)

    def _build_agent(
        self,
        task: Task,
        executor: ToolExecutor,
        on_message: Callable[[ChatMessage], Awaitable[None] | None],
    ) -> AgentLike:
        """Build the agent that runs ``task``, dispatching on its agent type.

        ``coder`` runs the single coder loop; ``pipeline`` runs the composed
        planner → coder → reviewer → tester pipeline over the same transcript.
        Unknown types raise a ``ValueError`` that the caller turns into a
        failed task.
        """
        should_cancel = self._cancel_checked(task.id)
        if task.agent_type == "pipeline":
            return PipelineAgent(
                llm=self._llm,
                executor=executor,
                max_tokens=self._settings.llm_max_tokens,
                temperature=self._settings.llm_temperature,
                on_message=on_message,
                on_token=self._on_token,
                stream=self._stream,
                should_cancel=should_cancel,
                max_passes=self._settings.pipeline_max_passes,
                max_repairs=self._settings.test_max_repairs,
                test_command=self._settings.test_command,
            )
        if task.agent_type == "coder":
            return CoderAgent(
                llm=self._llm,
                executor=executor,
                max_tokens=self._settings.llm_max_tokens,
                temperature=self._settings.llm_temperature,
                on_message=on_message,
                on_token=self._on_token,
                stream=self._stream,
                should_cancel=should_cancel,
            )
        raise ValueError(f"unsupported agent_type: {task.agent_type}")

    async def run_task(self, task_id: uuid.UUID) -> Task:
        """Run ``task_id`` to completion and return its final state.

        Each invocation is one *attempt*: the task is locked, its status is
        guarded (terminal tasks return unchanged; running tasks raise), and
        ``attempt`` is incremented and persisted with the RUNNING transition.
        A task whose plan requires human approval cannot run until the plan is
        approved, and a rejected plan cannot run at all. Status plus the
        incrementally persisted transcript form a durable checkpoint: a failed
        run can be retried and a crashed run is never silently lost.
        Cancellation is cooperative via the optional ``cancellations``
        registry, checked between steps.
        """
        async with self._session_factory() as session:
            task = await TaskRepository(session).get_for_run(task_id, for_update=True)
            if task is None:
                raise ValueError(f"no such task: {task_id}")
            if task.status in _TERMINAL_STATUSES:
                await self._discard_cancel(task.id)
                return task
            if task.status == TaskStatus.RUNNING:
                raise ValueError(f"task already running: {task_id}")
            if task.plan_needs_approval and task.plan_approved is None:
                raise ValueError("task plan awaits approval before it can run")
            if task.plan_approved is False:
                raise ValueError("task plan was rejected")

            task.status = TaskStatus.RUNNING
            task.attempt += 1
            task.started_at = _utcnow()
            await session.commit()

            await self._emit(
                OrchestratorEvent(task_id=task.id, kind="started", detail=task.goal),
            )

            workspace_dir = Path(task.session.workspace.repo_path)
            append = await self._transcript_appender(session, task)

            try:
                executor = self._executor_factory(workspace_dir)
                agent = self._build_agent(task, executor, append)
                initial_messages = await self._assemble_initial_messages(session, task)
                result = await agent.run(
                    task.goal,
                    initial_messages=initial_messages,
                )
            except TaskCancelled:
                return await self._cancel(session, task)
            except Exception as exc:
                return await self._fail(session, task, exc)

            if self._cancel_requested(task.id):
                return await self._cancel(session, task)

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
            await self._discard_cancel(task.id)
            return task

    async def plan_task(self, task_id: uuid.UUID) -> Task:
        """Run the planner over ``task_id`` and persist the plan artifact.

        The task is locked and flipped to ``planning`` while the planner runs;
        afterwards it returns to ``pending`` with ``plan`` set,
        ``plan_needs_approval`` reflecting whether the plan writes files or
        uses destructive operations, and ``plan_approved`` reset to ``None``
        so the execution gate (:meth:`run_task`) blocks until the plan is
        approved or rejected. The planner's transcript is appended to the
        session and a ``planned`` event carries the formatted plan.
        """
        async with self._session_factory() as session:
            task = await TaskRepository(session).get_for_run(task_id, for_update=True)
            if task is None:
                raise ValueError(f"no such task: {task_id}")
            if task.status in _TERMINAL_STATUSES:
                await self._discard_cancel(task.id)
                return task
            if task.status == TaskStatus.RUNNING:
                raise ValueError(f"task already running: {task_id}")

            task.status = TaskStatus.PLANNING
            await session.commit()

            workspace_dir = Path(task.session.workspace.repo_path)
            append = await self._transcript_appender(session, task)

            try:
                executor = self._executor_factory(workspace_dir)
                planner = PlannerAgent(
                    llm=self._llm,
                    executor=executor,
                    max_tokens=self._settings.llm_max_tokens,
                    temperature=self._settings.llm_temperature,
                    on_message=append,
                    on_token=self._on_token,
                    stream=self._stream,
                    should_cancel=self._cancel_checked(task.id),
                )
                initial_messages = await self._assemble_context(session, task)
                result = await planner.plan(
                    task.goal,
                    initial_messages=initial_messages,
                )
            except TaskCancelled:
                raise
            except Exception as exc:
                task.status = TaskStatus.PENDING
                await session.commit()
                raise ValueError(f"planning failed: {exc}") from exc

            task.status = TaskStatus.PENDING
            await TaskRepository(session).set_plan(
                task,
                plan=result.plan.to_dict(),
                needs_approval=result.plan.needs_approval,
            )
            await self._emit(
                OrchestratorEvent(
                    task_id=task.id,
                    kind="planned",
                    detail=format_plan(result.plan),
                ),
            )
            await self._discard_cancel(task.id)
            return task

    async def approve_task(self, task_id: uuid.UUID) -> Task:
        """Record human sign-off on a task's plan so it may run."""
        async with self._session_factory() as session:
            task = await self._approvable_task(session, task_id)
            await TaskRepository(session).set_approval(task, approved=True)
        await self._emit(OrchestratorEvent(task_id=task_id, kind="approved"))
        return task

    async def reject_task(self, task_id: uuid.UUID) -> Task:
        """Record human rejection of a task's plan, blocking execution."""
        async with self._session_factory() as session:
            task = await self._approvable_task(session, task_id)
            await TaskRepository(session).set_approval(task, approved=False)
        await self._emit(OrchestratorEvent(task_id=task_id, kind="rejected"))
        return task

    async def _approvable_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
    ) -> Task:
        """Fetch a task whose plan is awaiting a human decision."""
        task = await TaskRepository(session).get_for_run(task_id, for_update=True)
        if task is None:
            raise ValueError(f"no such task: {task_id}")
        if task.status in _TERMINAL_STATUSES:
            raise ValueError(f"task already finished: {task.status.value}")
        if task.plan is None or not task.plan_needs_approval:
            raise ValueError("task has no plan awaiting approval")
        return task

    async def _transcript_appender(
        self,
        session: AsyncSession,
        task: Task,
    ) -> Callable[[ChatMessage], Awaitable[None]]:
        """Return an async callback that persists and announces messages.

        Ordinals continue from the session's transcript so concurrent stages
        and retries keep a single sequential numbering.
        """
        ordinal = await MessageRepository(session).max_ordinal(task.session_id)

        async def _append(message: ChatMessage) -> None:
            nonlocal ordinal
            ordinal += 1
            await self._persist_message(session, task, message, ordinal)
            await self._emit(
                OrchestratorEvent(
                    task_id=task.id,
                    kind="message",
                    message=message,
                    ordinal=ordinal,
                ),
            )

        return _append

    async def _assemble_context(
        self,
        session: AsyncSession,
        task: Task,
    ) -> Sequence[ChatMessage]:
        """Retrieve a bounded context window for the task's goal.

        Returns an empty sequence when retrieval is disabled or the workspace
        has no matching chunks, so the agent loop runs exactly as before.
        """
        if not self._settings.retrieval_enabled:
            return ()
        window = await ContextAssembler.from_session(
            session,
            max_chunks=self._settings.retrieval_max_chunks,
            max_chars=self._settings.retrieval_max_chars,
        ).assemble(task.session.workspace.id, task.goal)
        text = format_context(window)
        if not text:
            return ()
        return [ChatMessage(role=ChatRole.USER, content=text)]

    async def _assemble_initial_messages(
        self,
        session: AsyncSession,
        task: Task,
    ) -> Sequence[ChatMessage]:
        """Seed an execution run with retrieval context and the approved plan.

        The retrieval window comes first, then the task's plan rendered as a
        directive, so the executor agent works from both the code context and
        the human-approved plan.
        """
        messages = list(await self._assemble_context(session, task))
        if task.plan is not None:
            plan = TaskPlan.from_dict(task.plan)
            messages.append(
                ChatMessage(
                    role=ChatRole.USER,
                    content=f"Follow this approved plan:\n{format_plan(plan)}",
                )
            )
        return messages

    async def retry_task(self, task_id: uuid.UUID) -> Task:
        """Reset a terminal task so it can be run again.

        The task must be terminal (completed, failed, or cancelled) and must
        not have exhausted ``max_attempts``. Its result, error, token counts,
        and timestamps are cleared, the status returns to ``pending``, and
        any pending cancellation is dropped so the next run starts fresh.
        The caller schedules the new run (:meth:`run_task`), which consumes
        another attempt. The prior transcript is preserved as durable
        history; new messages append to it.
        """
        async with self._session_factory() as session:
            task = await TaskRepository(session).get_for_run(task_id, for_update=True)
            if task is None:
                raise ValueError(f"no such task: {task_id}")
            if task.status not in _TERMINAL_STATUSES:
                raise ValueError(f"task is not in a retryable state: {task.status.value}")
            if task.attempt >= task.max_attempts:
                raise ValueError(f"max attempts ({task.max_attempts}) reached")
            task.status = TaskStatus.PENDING
            task.result = None
            task.error = None
            task.input_tokens = 0
            task.output_tokens = 0
            task.started_at = None
            task.finished_at = None
            await session.commit()
            await self._discard_cancel(task.id)
            return task

    async def _fail(self, session: AsyncSession, task: Task, exc: Exception) -> Task:
        if self._cancel_requested(task.id):
            return await self._cancel(session, task)
        task.status = TaskStatus.FAILED
        task.error = f"{type(exc).__name__}: {exc}"
        task.finished_at = _utcnow()
        await session.commit()
        await self._emit(
            OrchestratorEvent(task_id=task.id, kind="failed", detail=task.error),
        )
        await self._discard_cancel(task.id)
        return task

    async def _cancel(self, session: AsyncSession, task: Task) -> Task:
        """Finalize a cancelled run, keeping any earlier terminal transition.

        The cancel endpoint may already have persisted ``cancelled`` (crash-
        safe); this only persists it again if the in-session status is stale
        and then announces the transition so SSE clients close.
        """
        if task.status != TaskStatus.CANCELLED:
            task.status = TaskStatus.CANCELLED
            task.finished_at = _utcnow()
            await session.commit()
        await self._emit(
            OrchestratorEvent(task_id=task.id, kind="cancelled", detail=task.error),
        )
        await self._discard_cancel(task.id)
        return task

    def _cancel_checked(self, task_id: uuid.UUID) -> Callable[[], bool]:
        """Return the agent's cooperative cancellation predicate."""
        return lambda: self._cancel_requested(task_id)

    def _cancel_requested(self, task_id: uuid.UUID) -> bool:
        if self._cancellations is None:
            return False
        return self._cancellations.is_requested(task_id)

    async def _discard_cancel(self, task_id: uuid.UUID) -> None:
        if self._cancellations is not None:
            await self._cancellations.discard(task_id)

    async def _persist_message(
        self,
        session: AsyncSession,
        task: Task,
        message: ChatMessage,
        ordinal: int,
    ) -> None:
        """Persist one transcript entry and commit it immediately.

        Committing per message (rather than batching at the end) makes each
        entry visible to concurrent readers, which is what lets SSE clients
        replay the transcript while a task is still running.
        """
        session.add(
            Message(
                session_id=task.session_id,
                task_id=task.id,
                role=MessageRole(message.role.value),
                content=message.content,
                ordinal=ordinal,
                tool_call_id=message.tool_call_id,
                tool_calls=self._serialize_tool_calls(message.tool_requests),
            )
        )
        await session.commit()

    @staticmethod
    def _serialize_tool_calls(
        tool_requests: Sequence[ToolRequest],
    ) -> list[dict[str, Any]] | None:
        calls = [request.model_dump() for request in tool_requests]
        return calls or None

    async def _emit(self, event: OrchestratorEvent) -> None:
        if self._event_broker is not None:
            await self._event_broker.publish(event)
        if self._on_event is None:
            return
        result = self._on_event(event)
        if result is not None:
            await result
