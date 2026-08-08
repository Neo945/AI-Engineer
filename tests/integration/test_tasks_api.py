"""Integration tests for the task run/listing/detail endpoints.

These exercise the full path from HTTP request through the orchestrator to
PostgreSQL. They require PostgreSQL and Redis on localhost (``make up``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.fake_llm import FakeLLM

from app.core.config import Settings
from app.core.container import Container
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories.task import TaskRepository
from app.executor.executor import ToolExecutor
from app.gateway.dependencies import get_orchestrator
from app.gateway.main import create_app
from app.llm.messages import ToolRequest
from app.llm.protocol import LLMProvider, LLMResponse, LLMUsage
from app.orchestrator.orchestrator import Orchestrator
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


async def _seed_session(db_session: AsyncSession) -> uuid.UUID:
    user = User(email="tasks-api@example.com")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(owner_id=user.id, name="repo", repo_path="/tmp/workspace")
    db_session.add(workspace)
    await db_session.flush()
    agent_session = Session(workspace_id=workspace.id, user_id=user.id)
    db_session.add(agent_session)
    await db_session.commit()
    return agent_session.id


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


@pytest.fixture
def use_fake_llm(app: FastAPI, container: Container) -> Callable[[LLMProvider], None]:
    """Override the orchestrator dependency with a fake-LLM orchestrator."""

    def _apply(llm: LLMProvider) -> None:
        def _dep() -> Orchestrator:
            return Orchestrator(
                session_factory=container.session_factory,
                llm=llm,
                settings=container.settings,
                event_broker=container.event_broker,
                executor_factory=lambda _workspace_dir: cast(ToolExecutor, _StubExecutor()),
            )

        app.dependency_overrides[get_orchestrator] = _dep

    return _apply


async def test_create_and_run_task_completes_and_persists(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id = await _seed_session(db_session)
    use_fake_llm(FakeLLM([_final_response("Fixed.")]))

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Fix the bug"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["agent_type"] == "coder"
    assert body["session_id"] == str(session_id)
    task_id = body["id"]

    detail = await client.get(f"/api/v1/tasks/{task_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["status"] == "completed"
    assert detail_body["result"] == "Fixed."
    assert detail_body["input_tokens"] == 5
    assert detail_body["output_tokens"] == 2
    assert [(m["role"], m["content"]) for m in detail_body["messages"]] == [
        ("user", "Fix the bug"),
        ("assistant", "Fixed."),
    ]
    assert [m["ordinal"] for m in detail_body["messages"]] == [0, 1]


async def test_create_and_run_task_persists_tool_roundtrip(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id = await _seed_session(db_session)
    request = ToolRequest(name="file_read", arguments={"path": "x.py"})
    use_fake_llm(
        FakeLLM(
            [
                _tool_response(requests=[request]),
                _final_response("Done."),
            ]
        )
    )

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Inspect the repo"},
    )
    assert response.status_code == 202
    task_id = response.json()["id"]

    detail = await client.get(f"/api/v1/tasks/{task_id}")
    messages = detail.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assistant = messages[1]
    assert assistant["tool_calls"] == [
        {"id": assistant["tool_calls"][0]["id"], "name": "file_read", "arguments": {"path": "x.py"}}
    ]
    assert messages[2]["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert messages[2]["content"] == "42"
    assert messages[3]["content"] == "Done."


async def test_create_task_returns_503_when_llm_unconfigured(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id = await _seed_session(db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Run me"},
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]

    listing = await client.get(f"/api/v1/sessions/{session_id}/tasks")
    assert listing.json() == []


async def test_create_task_session_not_found(client: AsyncClient) -> None:
    response = await client.post(
        f"/api/v1/sessions/{uuid.uuid4()}/tasks",
        json={"goal": "Nowhere"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"


async def test_list_tasks_oldest_first(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id = await _seed_session(db_session)
    first = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="first")
    )
    await db_session.commit()
    await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="second")
    )
    await db_session.commit()

    response = await client.get(f"/api/v1/sessions/{session_id}/tasks")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["id"] == str(first.id)
    assert [item["goal"] for item in body] == ["first", "second"]


async def test_get_task_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/tasks/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


async def _collect_sse(client: AsyncClient, url: str) -> list[tuple[str, dict[str, object]]]:
    """Stream ``url`` and parse every non-comment SSE frame."""
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join([chunk async for chunk in response.aiter_text()])
    events: list[tuple[str, dict[str, object]]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        events.append((event, json.loads("".join(data_lines))))
    return events


async def test_task_events_stream_replays_and_closes(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id = await _seed_session(db_session)
    use_fake_llm(FakeLLM([_final_response("Fixed.")]))

    created = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Fix the bug"},
    )
    assert created.status_code == 202
    task_id = created.json()["id"]

    events = await _collect_sse(client, f"/api/v1/sessions/{session_id}/tasks/{task_id}/events")

    assert [event for event, _ in events] == [
        "snapshot",
        "message",
        "message",
        "completed",
    ]
    assert events[0][1]["status"] == "completed"
    assert [(data["role"], data["content"]) for _, data in events[1:3]] == [
        ("user", "Fix the bug"),
        ("assistant", "Fixed."),
    ]
    assert [data["ordinal"] for _, data in events[1:3]] == [0, 1]
    assert events[3][1]["status"] == "completed"
    assert events[3][1]["result"] == "Fixed."


async def test_task_events_streams_live_transcript(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id = await _seed_session(db_session)
    request = ToolRequest(name="file_read", arguments={"path": "x.py"})
    use_fake_llm(
        FakeLLM(
            [
                _tool_response(requests=[request]),
                _final_response("Done."),
            ]
        )
    )

    created = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Inspect the repo"},
    )
    task_id = created.json()["id"]

    events = await _collect_sse(client, f"/api/v1/sessions/{session_id}/tasks/{task_id}/events")

    assert [event for event, _ in events] == [
        "snapshot",
        "message",
        "message",
        "message",
        "message",
        "completed",
    ]
    assert [data["role"] for _, data in events[1:5]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert events[3][1]["content"] == "42"
    assert events[3][1]["tool_call_id"] is not None


async def test_task_events_for_missing_task_returns_404(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id = await _seed_session(db_session)
    response = await client.get(f"/api/v1/sessions/{session_id}/tasks/{uuid.uuid4()}/events")
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


async def test_task_events_for_missing_session_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/sessions/{uuid.uuid4()}/tasks/{uuid.uuid4()}/events")
    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"
