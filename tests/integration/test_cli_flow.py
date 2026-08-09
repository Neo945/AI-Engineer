"""Integration tests for the CLI workspace binding and task run flow.

These drive the shared command layer (``app.cli.commands``) against real
PostgreSQL/Redis with a scripted fake LLM and a stub executor, verifying the
bind → status → run → tasks lifecycle end to end. They require the local
infrastructure (``make up``).
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.cli import commands
from app.cli.context import CliContext, load_state, make_context, state_file_for
from app.core.config import Settings
from app.core.container import Container
from app.database.models.enums import TaskStatus
from app.database.repositories.task import TaskRepository
from app.executor.executor import ToolExecutor
from app.llm.protocol import LLMResponse, LLMUsage
from app.orchestrator.orchestrator import Orchestrator
from app.tools.schemas import ToolCall, ToolResult, ToolSpec

pytestmark = pytest.mark.integration


class _StubRegistry:
    def specs(self) -> list[ToolSpec]:
        return []


class _StubExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.registry = _StubRegistry()

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, output="42")


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=5, output_tokens=2),
        model="fake-model",
    )


def _make_context(settings: Settings, container: Container) -> tuple[CliContext, StringIO]:
    buffer = StringIO()
    console = Console(file=buffer, width=200, highlight=False)
    orchestrator = Orchestrator(
        session_factory=container.session_factory,
        llm=FakeLLM([_final_response("Fixed the bug.")]),
        settings=container.settings,
        event_broker=container.event_broker,
        cancellations=container.cancellations,
        executor_factory=lambda _workspace_dir: cast(ToolExecutor, _StubExecutor()),
    )
    return (
        make_context(
            settings=settings,
            console=console,
            container=container,
            orchestrator=orchestrator,
        ),
        buffer,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cli-repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    return repo


async def test_cli_bind_status_run_tasks_lifecycle(
    container: Container,
    settings: Settings,
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    ctx, buffer = _make_context(settings, container)
    try:
        code = await commands.cmd_init(ctx, repo=repo, name="cli-repo")
        assert code == 0
        assert state_file_for(repo).is_file()

        state = load_state(repo)
        assert state is not None

        status_code = await commands.cmd_status(ctx, repo=repo, state=state)
        assert status_code == 0
        assert "cli-repo" in buffer.getvalue()

        buffer.truncate(0)
        buffer.seek(0)
        run_code = await commands.cmd_run(
            ctx, repo=repo, state=state, goal="fix the bug", agent_type="coder"
        )
        assert run_code == 0

        async with container.session_factory() as session:
            tasks = await TaskRepository(session).list_by_session(state.session_id)
        assert len(tasks) == 1
        done = tasks[0]
        assert done.status == TaskStatus.COMPLETED
        assert done.result == "Fixed the bug."
        assert done.agent_type == "coder"

        buffer.truncate(0)
        buffer.seek(0)
        tasks_code = await commands.cmd_tasks(ctx, repo=repo, state=state, limit=20)
        assert tasks_code == 0
        assert "fix the bug" in buffer.getvalue()
    finally:
        await ctx.aclose()
