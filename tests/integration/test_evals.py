"""Integration tests for the eval harness against real PostgreSQL.

These verify that a headless run provisions real workspace/session/task
rows, drives the orchestrator, records the outcome, and persists the result
record. The orchestrator uses a FakeLLM and a stub executor (no sandbox, no
network), and verification is stubbed, so the tests need only PostgreSQL
reachable on localhost (``make up``).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from tests.unit.fake_llm import FakeLLM

from app.core.container import Container
from app.database.models.enums import TaskStatus
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.workspace import Workspace
from app.database.repositories.task import TaskRepository
from app.evals.results import ResultStore
from app.evals.runner import EvalRunner
from app.evals.tasks import EvalTask, task_by_id
from app.executor.executor import ToolExecutor
from app.executor.test_parser import TestReport as _TestReport
from app.llm.messages import ChatRole
from app.llm.protocol import LLMProvider, LLMResponse, LLMUsage
from app.orchestrator.orchestrator import Orchestrator
from app.tools.schemas import ToolCall, ToolResult, ToolSpec

pytestmark = pytest.mark.integration


class _StubRegistry:
    def __init__(self) -> None:
        self._specs = [ToolSpec(name="file_read", description="Read", arguments_schema={})]

    def specs(self) -> list[ToolSpec]:
        return self._specs


class _StubSandboxes:
    async def close(self) -> None:
        return None


class _StubExecutor:
    def __init__(self) -> None:
        self.registry = _StubRegistry()
        self.sandboxes = _StubSandboxes()

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, output="ok")


class _VerifierStub:
    async def verify(self, task: EvalTask, workspace_dir: Path) -> _TestReport:
        return _TestReport(
            framework="generic",
            command=task.test_command,
            passed=3,
            output="Ran 3 tests ... OK",
            exit_code=0,
        )


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=5, output_tokens=2),
        model="fake-model",
    )


def _runner(
    container: Container,
    llm: LLMProvider | None = None,
    executor: _StubExecutor | None = None,
) -> EvalRunner:
    llm = llm or FakeLLM([_final_response("Done.")])
    stub_executor = executor or _StubExecutor()

    def _orchestrator_factory(provision) -> Orchestrator:
        return Orchestrator(
            session_factory=container.session_factory,
            llm=llm,
            settings=container.settings,
            executor_factory=lambda _workspace_dir: cast(ToolExecutor, stub_executor),
        )

    return EvalRunner(
        session_factory=container.session_factory,
        llm=llm,
        settings=container.settings,
        orchestrator_factory=_orchestrator_factory,
        verify_factory=lambda _settings: _VerifierStub(),
    )


async def _workspace_id(container: Container, workspace: Path) -> object:
    async with container.session_factory() as session:
        rows = (
            (await session.execute(select(Workspace).where(Workspace.repo_path == str(workspace))))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    return rows[0].id


async def test_eval_run_provisions_and_records(
    container: Container,
    tmp_path: Path,
) -> None:
    task = task_by_id("fix-failing-test")
    workspace = tmp_path / "bench"
    store = ResultStore(tmp_path / "results.jsonl")

    record = await _runner(container).run(task, workspace, store=store)

    assert record.passed is True
    assert record.task_id == "fix-failing-test"
    assert record.task_status == "completed"
    assert record.tests_passed is True
    assert record.tokens > 0
    assert (workspace / "app_duration.py").is_file()
    assert (workspace / ".git").is_dir()
    assert len(store.load()) == 1

    workspace_id = await _workspace_id(container, workspace)
    async with container.session_factory() as session:
        sessions = (
            (await session.execute(select(Session).where(Session.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
    assert len(sessions) == 1
    session_id = sessions[0].id

    async with container.session_factory() as session:
        tasks = await TaskRepository(session).list_by_session(session_id)
    assert len(tasks) == 1
    assert tasks[0].goal == task.goal
    assert tasks[0].status == TaskStatus.COMPLETED


async def test_eval_run_drives_orchestrator_transcript(
    container: Container,
    tmp_path: Path,
) -> None:
    """The goal is actually sent to the LLM and the reply is persisted."""
    task = task_by_id("add-rest-endpoint")
    fake = FakeLLM([_final_response("Added the endpoint.")])
    runner = _runner(container, llm=fake)

    workspace = tmp_path / "bench"
    record = await runner.run(task, workspace)

    assert record.passed is True
    assert fake.calls, "the orchestrator never invoked the LLM"
    assert fake.calls[0]["messages"][0].role == ChatRole.USER
    assert task.goal in fake.calls[0]["messages"][0].content

    workspace_id = await _workspace_id(container, workspace)
    async with container.session_factory() as session:
        task_rows = (
            (
                await session.execute(
                    select(Task)
                    .join(Session, Session.id == Task.session_id)
                    .where(Session.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(task_rows) == 1
    assert task_rows[0].status == TaskStatus.COMPLETED
