"""Integration tests for the orchestrator against real PostgreSQL.

These verify that ``run_task`` persists the full transcript, drives the
task through its status lifecycle, and emits lifecycle events. They require
PostgreSQL reachable on localhost (``make up``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.fake_llm import FakeLLM

from app.core.container import Container
from app.database.models.enums import MessageRole, TaskStatus
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories.message import MessageRepository
from app.database.repositories.task import TaskRepository
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider, LLMResponse, LLMUsage
from app.orchestrator.broker import EventBroker
from app.orchestrator.cancellation import CancellationRegistry
from app.orchestrator.orchestrator import Orchestrator, OrchestratorEvent
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec

pytestmark = pytest.mark.integration


class _StubRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def specs(self) -> list[ToolSpec]:
        return self._specs


class _StubExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.registry = _StubRegistry(
            [
                ToolSpec(
                    name=ToolName.FILE_READ,
                    description="Read a file",
                    arguments_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                )
            ]
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, output="42")


class _RaisingLLM(FakeLLM):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        raise RuntimeError("boom")


class _FlakyLLM(FakeLLM):
    """Fails the first call, then behaves like a normal scripted LLM."""

    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        super().__init__(responses)
        self._fail_first = True

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("boom")
        return await super().complete(
            messages,
            tools=tools,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )


class _GatedLLM(FakeLLM):
    """Blocks the first call until ``release`` is set, recording ``entered``.

    Lets a test observe the run mid-flight (it has entered and is waiting)
    before releasing it, so cancellation can be requested deterministically.
    """

    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        super().__init__(responses)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        self.entered.set()
        await self.release.wait()
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="Done.", model=self.model)


def _tool_response(
    *,
    content: str = "",
    requests: list[ToolRequest],
    input_tokens: int = 5,
    output_tokens: int = 2,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_requests=requests,
        stop_reason="tool_use",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
    )


def _final_response(content: str, *, input_tokens: int = 5, output_tokens: int = 2) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
    )


async def _seed_task(
    db_session: AsyncSession,
    *,
    workspace_dir: str,
    goal: str = "Fix the bug",
    agent_type: str = "coder",
) -> Task:
    user = User(email="orchestrator@example.com")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(owner_id=user.id, name="repo", repo_path=workspace_dir)
    db_session.add(workspace)
    await db_session.flush()
    agent_session = Session(workspace_id=workspace.id, user_id=user.id)
    db_session.add(agent_session)
    await db_session.flush()
    task = Task(session_id=agent_session.id, agent_type=agent_type, goal=goal)
    db_session.add(task)
    await db_session.commit()
    return task


async def _build_orchestrator(
    container: Container,
    *,
    llm: LLMProvider,
    events: list[OrchestratorEvent],
    cancellations: CancellationRegistry | None = None,
) -> Orchestrator:
    stub = _StubExecutor()

    def _collect(event: OrchestratorEvent) -> None:
        events.append(event)

    return Orchestrator(
        session_factory=container.session_factory,
        llm=llm,
        settings=container.settings,
        on_event=_collect,
        cancellations=cancellations,
        executor_factory=lambda _workspace_dir: cast(ToolExecutor, stub),
    )


async def test_run_task_persists_transcript_and_finalizes(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    fake = FakeLLM(
        [
            _tool_response(requests=[ToolRequest(name="file_read", arguments={"path": "x.py"})]),
            _final_response("Fixed."),
        ]
    )
    events: list[OrchestratorEvent] = []
    orchestrator = await _build_orchestrator(container, llm=fake, events=events)

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
    assert done is not None
    assert done.status == TaskStatus.COMPLETED
    assert done.result == "Fixed."
    assert done.error is None
    assert done.input_tokens == 10
    assert done.output_tokens == 4
    assert done.started_at is not None
    assert done.finished_at is not None
    assert done.started_at <= done.finished_at

    async with container.session_factory() as fresh:
        messages = await MessageRepository(fresh).list_by_session(task.session_id)
    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert [message.ordinal for message in messages] == [0, 1, 2, 3]
    assert messages[0].content == "Fix the bug"
    assistant = messages[1]
    assert assistant.tool_calls is not None
    assert assistant.tool_calls == [
        {"id": assistant.tool_calls[0]["id"], "name": "file_read", "arguments": {"path": "x.py"}}
    ]
    tool_message = messages[2]
    assert tool_message.tool_call_id == assistant.tool_calls[0]["id"]
    assert tool_message.content == "42"
    assert messages[3].content == "Fixed."

    assert [event.kind for event in events] == [
        "started",
        "message",
        "message",
        "message",
        "message",
        "completed",
    ]
    assert events[0].detail == "Fix the bug"
    message_events = [event for event in events if event.kind == "message"]
    assert [event.ordinal for event in message_events] == [0, 1, 2, 3]
    assert message_events[0].message is not None
    assert message_events[0].message.content == "Fix the bug"
    assert events[-1].detail == "Fixed."
    assert events[-1].message is not None
    assert events[-1].message.content == "Fixed."


async def test_run_task_publishes_message_events_to_broker(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    fake = FakeLLM(
        [
            _tool_response(requests=[ToolRequest(name="file_read", arguments={"path": "x.py"})]),
            _final_response("Fixed."),
        ]
    )
    stub = _StubExecutor()
    broker = EventBroker()
    orchestrator = Orchestrator(
        session_factory=container.session_factory,
        llm=fake,
        settings=container.settings,
        event_broker=broker,
        executor_factory=lambda _workspace_dir: cast(ToolExecutor, stub),
    )
    queue = await broker.subscribe(task.id)

    await orchestrator.run_task(task.id)

    received: list[OrchestratorEvent] = []
    while not queue.empty():
        received.append(queue.get_nowait())
    assert [event.kind for event in received] == [
        "started",
        "message",
        "message",
        "message",
        "message",
        "completed",
    ]
    message_events = [event for event in received if event.kind == "message"]
    assert [event.ordinal for event in message_events] == [0, 1, 2, 3]
    assert message_events[0].message is not None
    assert message_events[0].message.role == ChatRole.USER
    assert message_events[1].message is not None
    assert message_events[1].message.role == ChatRole.ASSISTANT


async def test_run_task_marks_failed_but_keeps_partial_transcript(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    events: list[OrchestratorEvent] = []
    orchestrator = await _build_orchestrator(container, llm=_RaisingLLM(), events=events)

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
        persisted = await MessageRepository(fresh).list_by_session(task.session_id)
    assert done is not None
    assert done.status == TaskStatus.FAILED
    assert done.result is None
    assert done.error == "RuntimeError: boom"
    assert done.finished_at is not None
    assert [message.role for message in persisted] == [MessageRole.USER]
    assert persisted[0].content == "Fix the bug"

    assert [event.kind for event in events] == ["started", "message", "failed"]
    assert events[-1].detail == "RuntimeError: boom"


async def test_run_task_unknown_id_raises(container: Container) -> None:
    orchestrator = Orchestrator(
        session_factory=container.session_factory,
        llm=FakeLLM(),
        settings=container.settings,
    )
    with pytest.raises(ValueError, match="no such task"):
        await orchestrator.run_task(uuid.uuid4())


async def test_run_task_increments_attempt_and_records_max_attempts(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    orchestrator = await _build_orchestrator(
        container, llm=FakeLLM([_final_response("Fixed.")]), events=[]
    )

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
    assert done is not None
    assert done.status == TaskStatus.COMPLETED
    assert done.attempt == 1
    assert done.max_attempts == container.settings.task_max_attempts


async def test_run_task_is_noop_for_terminal_task(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    fake = FakeLLM([_final_response("Fixed.")])
    orchestrator = await _build_orchestrator(container, llm=fake, events=[])
    await orchestrator.run_task(task.id)

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
    assert done is not None
    assert done.status == TaskStatus.COMPLETED
    assert done.attempt == 1
    assert len(fake.calls) == 1


async def test_run_task_rejects_running_task(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    task.status = TaskStatus.RUNNING
    await db_session.commit()
    orchestrator = await _build_orchestrator(
        container, llm=FakeLLM([_final_response("Fixed.")]), events=[]
    )

    with pytest.raises(ValueError, match="already running"):
        await orchestrator.run_task(task.id)


async def test_retry_task_resets_and_reruns_failed_task(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    events: list[OrchestratorEvent] = []
    orchestrator = await _build_orchestrator(
        container, llm=_FlakyLLM([_final_response("Fixed.")]), events=events
    )

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        failed = await TaskRepository(fresh).get(task.id)
    assert failed is not None
    assert failed.status == TaskStatus.FAILED
    assert failed.attempt == 1
    assert failed.error == "RuntimeError: boom"

    retried = await orchestrator.retry_task(task.id)
    assert retried.status == TaskStatus.PENDING
    assert retried.error is None
    assert retried.result is None
    assert retried.started_at is None
    assert retried.finished_at is None
    assert retried.attempt == 1

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
        persisted = await MessageRepository(fresh).list_by_task(task.id)
    assert done is not None
    assert done.status == TaskStatus.COMPLETED
    assert done.attempt == 2
    assert done.result == "Fixed."
    assert done.error is None
    assert done.input_tokens == 5
    assert done.output_tokens == 2
    assert [message.role for message in persisted] == [
        MessageRole.USER,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert [event.kind for event in events] == [
        "started",
        "message",
        "failed",
        "started",
        "message",
        "message",
        "completed",
    ]


async def test_retry_task_rejects_running_task(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    task.status = TaskStatus.RUNNING
    await db_session.commit()
    orchestrator = await _build_orchestrator(container, llm=FakeLLM(), events=[])

    with pytest.raises(ValueError, match="not in a retryable state"):
        await orchestrator.retry_task(task.id)


async def test_retry_task_rejects_at_max_attempts(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    task.status = TaskStatus.FAILED
    task.attempt = task.max_attempts
    await db_session.commit()
    orchestrator = await _build_orchestrator(container, llm=FakeLLM(), events=[])

    with pytest.raises(ValueError, match="max attempts"):
        await orchestrator.retry_task(task.id)


async def test_retry_task_unknown_id_raises(container: Container) -> None:
    orchestrator = await _build_orchestrator(container, llm=FakeLLM(), events=[])

    with pytest.raises(ValueError, match="no such task"):
        await orchestrator.retry_task(uuid.uuid4())


async def test_cancel_task_aborts_running_agent(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path))
    request = ToolRequest(name="file_read", arguments={"path": "x.py"})
    gated = _GatedLLM([_tool_response(requests=[request]), _final_response("Fixed.")])
    cancellations = CancellationRegistry()
    events: list[OrchestratorEvent] = []
    orchestrator = await _build_orchestrator(
        container, llm=gated, events=events, cancellations=cancellations
    )

    run = asyncio.create_task(orchestrator.run_task(task.id))
    await gated.entered.wait()
    await cancellations.request_cancel(task.id)
    gated.release.set()
    await run

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
    assert done is not None
    assert done.status == TaskStatus.CANCELLED
    assert done.attempt == 1
    assert done.result is None
    assert done.finished_at is not None
    assert events[-1].kind == "cancelled"
    assert [event.kind for event in events] == [
        "started",
        "message",
        "message",
        "message",
        "cancelled",
    ]


async def test_run_task_runs_multi_agent_pipeline(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path), agent_type="pipeline")
    fake = FakeLLM(
        [
            _final_response("Plan: 1. Inspect. 2. Fix."),
            _final_response("Fixed the bug."),
            _final_response("VERDICT: PASS\nLooks good."),
            _final_response("VERDICT: PASS\nTests green."),
        ]
    )
    events: list[OrchestratorEvent] = []
    orchestrator = await _build_orchestrator(container, llm=fake, events=events)

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
        persisted = await MessageRepository(fresh).list_by_session(task.session_id)
    assert done is not None
    assert done.status == TaskStatus.COMPLETED
    assert done.result == "VERDICT: PASS\nTests green."
    assert done.error is None
    assert done.input_tokens == 20
    assert done.output_tokens == 8
    assert done.attempt == 1
    assert [message.role for message in persisted] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.ASSISTANT,
        MessageRole.ASSISTANT,
        MessageRole.ASSISTANT,
    ]
    assert [message.content for message in persisted] == [
        "Fix the bug",
        "Plan: 1. Inspect. 2. Fix.",
        "Fixed the bug.",
        "VERDICT: PASS\nLooks good.",
        "VERDICT: PASS\nTests green.",
    ]
    assert [event.kind for event in events] == [
        "started",
        "message",
        "message",
        "message",
        "message",
        "message",
        "completed",
    ]
    assert events[-1].detail == "VERDICT: PASS\nTests green."


async def test_run_task_fails_unsupported_agent_type(
    container: Container,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    task = await _seed_task(db_session, workspace_dir=str(tmp_path), agent_type="debugger")
    orchestrator = await _build_orchestrator(
        container, llm=FakeLLM([_final_response("Done.")]), events=[]
    )

    await orchestrator.run_task(task.id)

    async with container.session_factory() as fresh:
        done = await TaskRepository(fresh).get(task.id)
    assert done is not None
    assert done.status == TaskStatus.FAILED
    assert done.error == "ValueError: unsupported agent_type: debugger"
    assert done.attempt == 1
    assert done.finished_at is not None
