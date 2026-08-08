"""Integration tests for the orchestrator against real PostgreSQL.

These verify that ``run_task`` persists the full transcript, drives the
task through its status lifecycle, and emits lifecycle events. They require
PostgreSQL reachable on localhost (``make up``).
"""

from __future__ import annotations

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
    task = Task(session_id=agent_session.id, agent_type="coder", goal=goal)
    db_session.add(task)
    await db_session.commit()
    return task


async def _build_orchestrator(
    container: Container,
    *,
    llm: LLMProvider,
    events: list[OrchestratorEvent],
) -> Orchestrator:
    stub = _StubExecutor()

    def _collect(event: OrchestratorEvent) -> None:
        events.append(event)

    return Orchestrator(
        session_factory=container.session_factory,
        llm=llm,
        settings=container.settings,
        on_event=_collect,
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
