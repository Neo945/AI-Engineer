"""Unit tests for the engineer CLI (parser, state, git-backed commands).

These tests require no infrastructure: state persistence and the pure-git
commands run against a throwaway repository, and ``cmd_run`` is exercised
with a stubbed session factory, broker, and orchestrator.
"""

from __future__ import annotations

import subprocess
import uuid
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from app.cli.commands import (
    _short,
    _snippet,
    cmd_commit,
    cmd_diff,
    cmd_run,
    render_event,
)
from app.cli.context import (
    CliContext,
    CliError,
    WorkspaceState,
    find_repo_root,
    load_state,
    save_state,
    state_file_for,
)
from app.cli.main import arun, build_parser
from app.core.config import Settings
from app.database.models.enums import TaskStatus
from app.database.models.task import Task
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.orchestrator.broker import EventBroker
from app.orchestrator.orchestrator import OrchestratorEvent


def _settings() -> Settings:
    return Settings(_env_file=None)


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _state(repo: Path) -> WorkspaceState:
    return WorkspaceState(
        repo_path=str(repo),
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )


def test_parser_run_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "fix", "the", "test"])
    assert args.command == "run"
    assert args.agent_type == "coder"
    assert args.goal == ["fix", "the", "test"]
    assert args.yes is False

    yes = parser.parse_args(["run", "-y", "go"])
    assert yes.yes is True

    pipeline = parser.parse_args(["run", "--agent-type", "pipeline", "go"])
    assert pipeline.agent_type == "pipeline"

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--agent-type", "debugger", "go"])


def test_parser_tasks_and_commit_defaults() -> None:
    parser = build_parser()
    tasks = parser.parse_args(["tasks"])
    assert tasks.limit == 20
    assert parser.parse_args(["tasks", "--limit", "5"]).limit == 5

    commit = parser.parse_args(["commit", "-m", "msg"])
    assert commit.message == "msg"
    assert commit.yes is False

    with pytest.raises(SystemExit):
        parser.parse_args(["bogus"])


def test_state_roundtrip_and_location(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    state = _state(repo)

    path = save_state(repo, state)

    assert path == state_file_for(repo)
    assert path.is_file()
    assert load_state(repo) == state


def test_load_state_rejects_corrupt_file(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    path = state_file_for(repo)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CliError, match="cannot read"):
        load_state(repo)


def test_short_and_snippet_truncate() -> None:
    assert _short("exact", limit=60) == "exact"
    long_line = "x" * 100
    assert len(_short(long_line, limit=10)) == 10
    assert _short(long_line, limit=10).endswith("...")
    assert _snippet("tiny") == "tiny"
    long_block = "y" * 400
    assert len(_snippet(long_block)) == 300
    assert _snippet(long_block).endswith("...")


def test_render_event_formats_messages() -> None:
    console, buffer = _console()
    task_id = uuid.uuid4()

    render_event(console, OrchestratorEvent(task_id=task_id, kind="started", detail="do it"))
    render_event(
        console,
        OrchestratorEvent(
            task_id=task_id,
            kind="message",
            message=ChatMessage(
                role=ChatRole.ASSISTANT,
                content="Let me look.",
                tool_requests=[ToolRequest(name="file_read", arguments={"path": "x.py"})],
            ),
        ),
    )
    render_event(
        console,
        OrchestratorEvent(
            task_id=task_id,
            kind="message",
            message=ChatMessage(role=ChatRole.TOOL, content="42", tool_call_id="call-1"),
        ),
    )
    render_event(console, OrchestratorEvent(task_id=task_id, kind="completed", detail="ok"))

    out = buffer.getvalue()
    assert "do it" in out
    assert "Let me look." in out
    assert "file_read" in out
    assert "42" in out
    assert "completed" in out


async def test_find_repo_root_from_subdirectory(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)

    root = await find_repo_root(sub)

    assert root == repo.resolve()


async def test_arun_outside_git_repo_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())

    code = await arun(["status"], ctx)

    assert code == 1
    assert "error" in buffer.getvalue()


async def test_arun_status_without_binding_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())

    code = await arun(["status"], ctx)

    assert code == 1
    assert "engineer init" in buffer.getvalue()


async def test_cmd_diff_and_commit_roundtrip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "file.txt").write_text("one\ntwo\n", encoding="utf-8")
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())

    diff_code = await cmd_diff(ctx, repo=repo, state=None)
    assert diff_code == 0
    assert "+two" in buffer.getvalue()

    commit_code = await cmd_commit(ctx, repo=repo, state=None, message="add two", yes=True)
    assert commit_code == 0
    assert _git(repo, "log", "-1", "--format=%s").strip() == "add two"

    console2, buffer2 = _console()
    ctx2 = CliContext(console=console2, settings=_settings())
    assert await cmd_diff(ctx2, repo=repo, state=None, ref="HEAD") == 0
    assert "no changes" in buffer2.getvalue()


async def test_cmd_run_streams_events_with_stubbed_engine(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    holder: list[Task] = []

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Task] = []

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def add(self, entity: Task) -> None:
            self.added.append(entity)
            holder.append(entity)
            if entity.id is None:
                entity.id = uuid.uuid4()

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    class FakeContainer:
        def __init__(self, session: FakeSession, broker: EventBroker) -> None:
            self._session = session
            self.event_broker = broker

        def session_factory(self) -> FakeSession:
            return self._session

    class FakeOrchestrator:
        def __init__(
            self,
            broker: EventBroker,
            task_holder: list[Task],
            plan: dict[str, object] | None,
        ) -> None:
            self._broker = broker
            self._task_holder = task_holder
            self._plan = plan
            self.approvals: list[uuid.UUID] = []

        async def plan_task(self, task_id: uuid.UUID) -> Task:
            task = self._task_holder[0]
            task.plan = self._plan
            await self._broker.publish(
                OrchestratorEvent(task_id=task_id, kind="planned", detail="planner done")
            )
            return task

        async def approve_task(self, task_id: uuid.UUID) -> Task:
            self.approvals.append(task_id)
            task = self._task_holder[0]
            task.plan_approved = True
            return task

        async def run_task(self, task_id: uuid.UUID) -> Task:
            task = self._task_holder[0]
            task.status = TaskStatus.COMPLETED
            await self._broker.publish(
                OrchestratorEvent(task_id=task_id, kind="completed", detail="done")
            )
            return task

    read_only_plan: dict[str, object] = {"objective": "Read the README."}
    broker = EventBroker()
    session = FakeSession()
    container = FakeContainer(session, broker)
    orchestrator = FakeOrchestrator(broker, holder, read_only_plan)
    console, buffer = _console()
    ctx = CliContext(
        console=console,
        settings=_settings(),
        container=container,  # type: ignore[arg-type]
        orchestrator=orchestrator,  # type: ignore[arg-type]
    )
    state = _state(repo)

    code = await cmd_run(ctx, repo=repo, state=state, goal="do the thing", agent_type="coder")

    assert code == 0
    assert len(session.added) == 1
    assert isinstance(session.added[0], Task)
    assert holder[0].status == TaskStatus.COMPLETED
    assert orchestrator.approvals == []
    out = buffer.getvalue()
    assert "do the thing" in out
    assert "planned" in out
    assert "completed" in out
    assert "Read the README." in out


async def test_cmd_run_prompts_and_aborts_when_plan_declined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path)
    holder: list[Task] = []

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Task] = []

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def add(self, entity: Task) -> None:
            self.added.append(entity)
            holder.append(entity)
            if entity.id is None:
                entity.id = uuid.uuid4()

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    class FakeContainer:
        def __init__(self, session: FakeSession, broker: EventBroker) -> None:
            self._session = session
            self.event_broker = broker

        def session_factory(self) -> FakeSession:
            return self._session

    class FakeOrchestrator:
        def __init__(self, broker: EventBroker, task_holder: list[Task]) -> None:
            self._broker = broker
            self._task_holder = task_holder
            self.approvals: list[uuid.UUID] = []

        async def plan_task(self, task_id: uuid.UUID) -> Task:
            task = self._task_holder[0]
            task.plan = {"objective": "Add auth.", "files": ["src/auth.py"]}
            await self._broker.publish(
                OrchestratorEvent(task_id=task_id, kind="planned", detail="planner done")
            )
            return task

        async def approve_task(self, task_id: uuid.UUID) -> Task:
            self.approvals.append(task_id)
            return self._task_holder[0]

        async def run_task(self, task_id: uuid.UUID) -> Task:
            raise AssertionError("run must not start after a declined plan")

    monkeypatch.setattr("app.cli.commands.Confirm.ask", lambda *a, **k: False)
    broker = EventBroker()
    session = FakeSession()
    console, buffer = _console()
    ctx = CliContext(
        console=console,
        settings=_settings(),
        container=FakeContainer(session, broker),  # type: ignore[arg-type]
        orchestrator=FakeOrchestrator(broker, holder),  # type: ignore[arg-type]
    )
    state = _state(repo)

    code = await cmd_run(ctx, repo=repo, state=state, goal="add auth", agent_type="coder")

    assert code == 1
    assert "awaits approval" in buffer.getvalue()


async def test_cmd_run_yes_approves_write_plan(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    holder: list[Task] = []
    approvals: list[uuid.UUID] = []

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Task] = []

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def add(self, entity: Task) -> None:
            self.added.append(entity)
            holder.append(entity)
            if entity.id is None:
                entity.id = uuid.uuid4()

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    class FakeContainer:
        def __init__(self, session: FakeSession, broker: EventBroker) -> None:
            self._session = session
            self.event_broker = broker

        def session_factory(self) -> FakeSession:
            return self._session

    class FakeOrchestrator:
        def __init__(self, broker: EventBroker) -> None:
            self._broker = broker

        async def plan_task(self, task_id: uuid.UUID) -> Task:
            task = holder[0]
            task.plan = {"objective": "Add auth.", "files": ["src/auth.py"]}
            await self._broker.publish(
                OrchestratorEvent(task_id=task_id, kind="planned", detail="planner done")
            )
            return task

        async def approve_task(self, task_id: uuid.UUID) -> Task:
            approvals.append(task_id)
            holder[0].plan_approved = True
            return holder[0]

        async def run_task(self, task_id: uuid.UUID) -> Task:
            holder[0].status = TaskStatus.COMPLETED
            await self._broker.publish(
                OrchestratorEvent(task_id=task_id, kind="completed", detail="done")
            )
            return holder[0]

    broker = EventBroker()
    session = FakeSession()
    console, buffer = _console()
    ctx = CliContext(
        console=console,
        settings=_settings(),
        container=FakeContainer(session, broker),  # type: ignore[arg-type]
        orchestrator=FakeOrchestrator(broker),  # type: ignore[arg-type]
    )
    state = _state(repo)

    code = await cmd_run(ctx, repo=repo, state=state, goal="add auth", agent_type="coder", yes=True)

    assert code == 0
    assert len(approvals) == 1
    assert holder[0].status == TaskStatus.COMPLETED
    out = buffer.getvalue()
    assert "plan approved" in out
    assert "completed" in out
