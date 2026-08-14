"""Integration tests for the task run/listing/detail endpoints.

These exercise the full path from HTTP request through the orchestrator to
PostgreSQL. They require PostgreSQL and Redis on localhost (``make up``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.fake_llm import FakeLLM

from app.core.config import Settings
from app.core.container import Container
from app.database.models.enums import TaskStatus
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.workspace import Workspace
from app.database.repositories.task import TaskRepository
from app.database.repositories.user import UserRepository
from app.executor.executor import ToolExecutor
from app.gateway.dependencies import get_orchestrator
from app.gateway.main import create_app
from app.llm.messages import ChatMessage, ToolRequest
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
        self.workspace_dir = Path("/workspace")
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


class _FlakyLLM(FakeLLM):
    """Fails the first call, then behaves like a normal scripted LLM."""

    def __init__(self, responses: list[LLMResponse]) -> None:
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


async def _make_user(
    client: AsyncClient,
    email: str,
) -> dict[str, str]:
    """Register a user through the API and return bearer-token headers."""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct horse battery staple",
            "full_name": "API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _seed_session(
    client: AsyncClient,
    db_session: AsyncSession,
    email: str = "tasks-api@example.com",
) -> tuple[uuid.UUID, dict[str, str]]:
    """Seed a user-owned workspace and session; return the session id and headers."""
    headers = await _make_user(client, email)
    user = await UserRepository(db_session).get_by_email(email)
    assert user is not None
    workspace = Workspace(owner_id=user.id, name="repo", repo_path="/tmp/workspace")
    db_session.add(workspace)
    await db_session.flush()
    agent_session = Session(workspace_id=workspace.id, user_id=user.id)
    db_session.add(agent_session)
    await db_session.commit()
    return agent_session.id, headers


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
                cancellations=container.cancellations,
                executor_factory=lambda _workspace_dir: cast(ToolExecutor, _StubExecutor()),
            )

        app.dependency_overrides[get_orchestrator] = _dep

    return _apply


async def test_create_and_run_task_completes_and_persists(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    use_fake_llm(FakeLLM([_final_response("Fixed.")]))

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Fix the bug"},
        headers=headers,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["agent_type"] == "coder"
    assert body["session_id"] == str(session_id)
    task_id = body["id"]

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
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
    session_id, headers = await _seed_session(client, db_session)
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
        headers=headers,
    )
    assert response.status_code == 202
    task_id = response.json()["id"]

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    messages = detail.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assistant = messages[1]
    assert assistant["tool_calls"] == [
        {"id": assistant["tool_calls"][0]["id"], "name": "file_read", "arguments": {"path": "x.py"}}
    ]
    assert messages[2]["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert messages[2]["content"] == "42"
    assert messages[3]["content"] == "Done."


async def test_create_and_run_pipeline_task(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    use_fake_llm(
        FakeLLM(
            [
                _final_response("Plan: 1. Inspect. 2. Fix."),
                _final_response("Fixed the bug."),
                _final_response("VERDICT: PASS\nLooks good."),
            ]
        )
    )

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Fix the bug", "agent_type": "pipeline"},
        headers=headers,
    )
    assert response.status_code == 202
    assert response.json()["agent_type"] == "pipeline"
    task_id = response.json()["id"]

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "completed"
    assert body["result"].startswith("VERDICT: PASS\nTest run: pytest")
    assert body["input_tokens"] == 15
    assert body["output_tokens"] == 6
    assert [m["role"] for m in body["messages"]] == [
        "user",
        "assistant",
        "assistant",
        "assistant",
        "assistant",
    ]
    assert [m["content"] for m in body["messages"][:4]] == [
        "Fix the bug",
        "Plan: 1. Inspect. 2. Fix.",
        "Fixed the bug.",
        "VERDICT: PASS\nLooks good.",
    ]
    assert body["messages"][4]["content"].startswith("VERDICT: PASS\nTest run: pytest")
    assert [m["ordinal"] for m in body["messages"]] == [0, 1, 2, 3, 4]


async def test_create_task_returns_503_when_llm_unconfigured(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id, headers = await _seed_session(client, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Run me"},
        headers=headers,
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]

    listing = await client.get(f"/api/v1/sessions/{session_id}/tasks", headers=headers)
    assert listing.json() == []


async def test_create_task_requires_authentication(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id, _ = await _seed_session(client, db_session)

    response = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Denied"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "not authenticated"


async def test_create_task_session_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = await _make_user(client, "not-found@example.com")
    response = await client.post(
        f"/api/v1/sessions/{uuid.uuid4()}/tasks",
        json={"goal": "Nowhere"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"


async def test_list_tasks_oldest_first(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    first = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="first")
    )
    await db_session.commit()
    await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="second")
    )
    await db_session.commit()

    response = await client.get(f"/api/v1/sessions/{session_id}/tasks", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["id"] == str(first.id)
    assert [item["goal"] for item in body] == ["first", "second"]


async def test_get_task_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = await _make_user(client, "task-not-found@example.com")
    response = await client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


async def _collect_sse(
    client: AsyncClient,
    url: str,
    headers: dict[str, str],
) -> list[tuple[str, dict[str, object]]]:
    """Stream ``url`` and parse every non-comment SSE frame."""
    async with client.stream("GET", url, headers=headers) as response:
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
    session_id, headers = await _seed_session(client, db_session)
    use_fake_llm(FakeLLM([_final_response("Fixed.")]))

    created = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Fix the bug"},
        headers=headers,
    )
    assert created.status_code == 202
    task_id = created.json()["id"]

    events = await _collect_sse(
        client, f"/api/v1/sessions/{session_id}/tasks/{task_id}/events", headers
    )

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
    session_id, headers = await _seed_session(client, db_session)
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
        headers=headers,
    )
    task_id = created.json()["id"]

    events = await _collect_sse(
        client, f"/api/v1/sessions/{session_id}/tasks/{task_id}/events", headers
    )

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
    session_id, headers = await _seed_session(client, db_session)
    response = await client.get(
        f"/api/v1/sessions/{session_id}/tasks/{uuid.uuid4()}/events", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


async def test_task_events_for_missing_session_returns_404(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = await _make_user(client, "events-not-found@example.com")
    response = await client.get(
        f"/api/v1/sessions/{uuid.uuid4()}/tasks/{uuid.uuid4()}/events", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"


async def test_retry_task_reruns_failed_task(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    use_fake_llm(_FlakyLLM([_final_response("Fixed.")]))

    created = await client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"goal": "Fix the bug"},
        headers=headers,
    )
    assert created.status_code == 202
    task_id = created.json()["id"]
    assert created.json()["status"] == "pending"
    assert created.json()["attempt"] == 0
    assert created.json()["max_attempts"] == 3

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "failed"
    assert detail.json()["attempt"] == 1
    assert detail.json()["error"] == "RuntimeError: boom"

    retried = await client.post(f"/api/v1/tasks/{task_id}/retry", headers=headers)
    assert retried.status_code == 202
    assert retried.json()["status"] == "pending"
    assert retried.json()["attempt"] == 1

    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    body = detail.json()
    for _ in range(50):
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
        body = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    assert body["status"] == "completed"
    assert body["attempt"] == 2
    assert body["result"] == "Fixed."
    assert body["error"] is None
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "Fix the bug"),
        ("user", "Fix the bug"),
        ("assistant", "Fixed."),
    ]


async def test_retry_task_rejects_running_task(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    use_fake_llm(FakeLLM())
    session_id, headers = await _seed_session(client, db_session)
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="Running")
    )
    task.status = TaskStatus.RUNNING
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers)
    assert response.status_code == 409
    assert "not in a retryable state" in response.json()["detail"]


async def test_retry_task_rejects_at_max_attempts(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    use_fake_llm(FakeLLM())
    session_id, headers = await _seed_session(client, db_session)
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="Exhausted")
    )
    task.status = TaskStatus.FAILED
    task.attempt = task.max_attempts
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/retry", headers=headers)
    assert response.status_code == 409
    assert "max attempts" in response.json()["detail"]


async def test_retry_task_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    use_fake_llm(FakeLLM())
    headers = await _make_user(client, "retry-not-found@example.com")
    response = await client.post(f"/api/v1/tasks/{uuid.uuid4()}/retry", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


_PLAN_TEXT = """Objective: Add a reset password flow.
## Files
- src/auth/reset.py
## Steps
1. Add the reset token model.
2. Wire the reset route.
"""


async def test_plan_approve_run_endpoint_completes(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    use_fake_llm(FakeLLM([_final_response(_PLAN_TEXT), _final_response("Fixed.")]))
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="Add reset flow")
    )
    await db_session.commit()
    task_id = task.id

    planned = await client.post(f"/api/v1/tasks/{task_id}/plan", headers=headers)
    assert planned.status_code == 202
    body = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    for _ in range(50):
        if body.get("plan") is not None:
            break
        await asyncio.sleep(0.05)
        body = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    assert body["plan"] is not None
    assert body["plan"]["files"] == ["src/auth/reset.py"]
    assert body["plan_needs_approval"] is True
    assert body["plan_approved"] is None

    run_early = await client.post(f"/api/v1/tasks/{task_id}/run", headers=headers)
    assert run_early.status_code == 409
    assert "awaits approval" in run_early.json()["detail"]

    approved = await client.post(f"/api/v1/tasks/{task_id}/approve", headers=headers)
    assert approved.status_code == 200
    assert approved.json()["plan_approved"] is True

    run = await client.post(f"/api/v1/tasks/{task_id}/run", headers=headers)
    assert run.status_code == 202

    body = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    for _ in range(50):
        if body["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
        body = (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).json()
    assert body["status"] == "completed"
    assert body["result"] == "Fixed."
    assert body["plan_approved"] is True


async def test_reject_endpoint_blocks_run(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    use_fake_llm(FakeLLM([_final_response(_PLAN_TEXT)]))
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="Add reset flow")
    )
    await db_session.commit()
    task_id = task.id

    await client.post(f"/api/v1/tasks/{task_id}/plan", headers=headers)
    rejected = await client.post(f"/api/v1/tasks/{task_id}/reject", headers=headers)
    assert rejected.status_code == 200
    assert rejected.json()["plan_approved"] is False

    run = await client.post(f"/api/v1/tasks/{task_id}/run", headers=headers)
    assert run.status_code == 409
    assert "rejected" in run.json()["detail"]


async def test_approve_endpoint_requires_pending_plan(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    use_fake_llm(FakeLLM())
    session_id, headers = await _seed_session(client, db_session)
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="No plan yet")
    )
    await db_session.commit()

    approved = await client.post(f"/api/v1/tasks/{task.id}/approve", headers=headers)
    assert approved.status_code == 409
    assert "no plan awaiting approval" in approved.json()["detail"]

    rejected = await client.post(f"/api/v1/tasks/{task.id}/reject", headers=headers)
    assert rejected.status_code == 409
    assert "no plan awaiting approval" in rejected.json()["detail"]


async def test_plan_approve_run_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
    use_fake_llm: Callable[[LLMProvider], None],
) -> None:
    use_fake_llm(FakeLLM())
    headers = await _make_user(client, "plan-not-found@example.com")
    for path in ("plan", "approve", "reject", "run"):
        response = await client.post(f"/api/v1/tasks/{uuid.uuid4()}/{path}", headers=headers)
        assert response.status_code == 404, path
        assert response.json()["detail"] == "task not found"


async def test_cancel_task_pending(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="Never run")
    )
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["finished_at"] is not None


async def test_cancel_task_running(
    app: FastAPI,
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="In flight")
    )
    task.status = TaskStatus.RUNNING
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["finished_at"] is not None
    assert app.state.container.cancellations.is_requested(task.id) is True


async def test_cancel_task_already_finished_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    session_id, headers = await _seed_session(client, db_session)
    task = await TaskRepository(db_session).add(
        Task(session_id=session_id, agent_type="coder", goal="Done")
    )
    task.status = TaskStatus.COMPLETED
    await db_session.commit()

    response = await client.post(f"/api/v1/tasks/{task.id}/cancel", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "task already finished"


async def test_cancel_task_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    headers = await _make_user(client, "cancel-not-found@example.com")
    response = await client.post(f"/api/v1/tasks/{uuid.uuid4()}/cancel", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"
