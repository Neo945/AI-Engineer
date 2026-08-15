"""Unit tests for the engineer CLI (parser, state, git-backed commands).

These tests require no infrastructure: state persistence and the pure-git
commands run against a throwaway repository, and ``cmd_run`` is exercised
with a stubbed session factory, broker, and orchestrator.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.cli.commands import (
    _short,
    _snippet,
    cmd_audit,
    cmd_commit,
    cmd_diff,
    cmd_pr,
    cmd_review,
    cmd_run,
    cmd_test,
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
from app.executor.test_parser import TestReport
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMResponse, LLMUsage
from app.orchestrator.broker import EventBroker
from app.orchestrator.orchestrator import OrchestratorEvent
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec


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


class _StubSandboxes:
    async def close(self) -> None:
        return None


class _StubRegistry:
    def specs(self) -> list[ToolSpec]:
        return []


class _StubExecutor:
    def __init__(self, workspace_dir: Path, reports: list[TestReport] | None = None) -> None:
        self.workspace_dir = workspace_dir
        self.registry = _StubRegistry()
        self.sandboxes = _StubSandboxes()
        self._reports = list(reports or [])
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        report = (
            self._reports.pop(0)
            if self._reports
            else TestReport(framework="pytest", command="pytest -q")
        )
        return ToolResult(
            call_id=call.id,
            tool=call.tool,
            ok=report.ok,
            output="report",
            data={"report": report.to_dict()},
        )


def _report(*, ok: bool) -> TestReport:
    return TestReport(
        framework="pytest",
        command="pytest -q",
        passed=1 if ok else 0,
        failed=0 if ok else 1,
    )


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=2, output_tokens=1),
        model="fake-model",
    )


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
        raise RuntimeError("provider exploded")


def test_final_verdict_detection() -> None:
    from app.cli.commands import _final_verdict, _verdict_passed

    assert _final_verdict("VERDICT: PASS\nNo issues.") == "PASS"
    assert _final_verdict("## Findings\n...\nVERDICT: CHANGES_NEEDED") == "CHANGES_NEEDED"
    assert _final_verdict("VERDICT: PASS\nVERDICT: CHANGES_NEEDED") == "PASS"
    assert _final_verdict("no verdict anywhere") is None
    assert _verdict_passed("## Findings\n...\nVERDICT: PASS") is True
    assert _verdict_passed("VERDICT: FAIL") is False


def test_parser_test_and_review_defaults() -> None:
    parser = build_parser()

    test = parser.parse_args(["test"])
    assert test.command == "test"
    assert test.test_command is None
    assert test.framework is None
    assert test.fix is False
    assert test.repairs is None

    fixed = parser.parse_args(["test", "--fix", "--command", "make test", "--repairs", "3"])
    assert fixed.fix is True
    assert fixed.test_command == "make test"
    assert fixed.repairs == 3

    jest = parser.parse_args(["test", "--framework", "jest"])
    assert jest.framework == "jest"

    review = parser.parse_args(["review"])
    assert review.ref is None
    assert review.max_steps == 8

    review_ref = parser.parse_args(["review", "--ref", "HEAD~1", "--max-steps", "3"])
    assert review_ref.ref == "HEAD~1"
    assert review_ref.max_steps == 3

    audit = parser.parse_args(["audit"])
    assert audit.command == "audit"
    assert audit.ref is None
    assert audit.max_steps == 8

    audit_ref = parser.parse_args(["audit", "--ref", "main", "--max-steps", "4"])
    assert audit_ref.ref == "main"
    assert audit_ref.max_steps == 4


async def test_cmd_test_runs_suite_and_reports_failure(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    stub = _StubExecutor(repo, [_report(ok=False)])

    code = await cmd_test(ctx, repo=repo, state=None, executor=stub)

    assert code == 1
    assert stub.calls[0].tool == ToolName.TEST_RUN
    assert stub.calls[0].arguments["command"] == "python -m pytest -q"
    assert "1 failed" in buffer.getvalue()


async def test_cmd_test_passing_suite_exits_zero(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    stub = _StubExecutor(repo, [_report(ok=True)])

    code = await cmd_test(ctx, repo=repo, state=None, executor=stub)

    assert code == 0
    assert "All tests pass." in buffer.getvalue()


async def test_cmd_test_repair_loop_fixes_failures(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("Fixed it.\nVERDICT: PASS")])
    stub = _StubExecutor(repo, [_report(ok=False), _report(ok=True)])

    code = await cmd_test(ctx, repo=repo, state=None, fix=True, llm=llm, executor=stub)

    assert code == 0
    assert "VERDICT: PASS" in buffer.getvalue()


async def test_cmd_test_repair_exhausts_attempts_exits_one(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("try1\nVERDICT: FAIL"), _response("try2\nVERDICT: FAIL")])
    stub = _StubExecutor(repo, [_report(ok=False), _report(ok=False), _report(ok=False)])

    code = await cmd_test(ctx, repo=repo, state=None, fix=True, repairs=2, llm=llm, executor=stub)

    assert code == 1
    assert "VERDICT: FAIL" in buffer.getvalue()


async def test_cmd_review_pass_exits_zero(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("VERDICT: PASS\nNo issues.")])

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 0
    assert "VERDICT: PASS" in buffer.getvalue()


async def test_cmd_review_changes_needed_exits_one(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("VERDICT: CHANGES_NEEDED\nAdd tests.")])

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    assert "CHANGES_NEEDED" in buffer.getvalue()


async def test_cmd_review_reasks_missing_verdict(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM(
        [
            _response("Looks fine to me."),
            _response("VERDICT: PASS\nNo issues found."),
        ]
    )

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 0
    assert len(llm.calls) == 2
    assert "VERDICT: PASS" in buffer.getvalue()


async def test_cmd_review_returns_one_when_verdict_never_appears(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM(
        [
            _response("Could be better."),
            _response("Still no verdict."),
        ]
    )

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    assert len(llm.calls) == 2
    assert "no verdict produced" in buffer.getvalue()


async def test_cmd_review_renders_structured_findings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    answer = """\
VERDICT: CHANGES_NEEDED
The auth logic needs a fix.
```json
[
  {
    "severity": "high",
    "file": "app/auth.py",
    "line": 12,
    "problem": "expiry never checked",
    "reason": "expired tokens are accepted",
    "fix": "compare now <= exp"
  }
]
```
"""
    llm = FakeLLM([_response(answer)])

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    text = buffer.getvalue()
    assert "VERDICT: CHANGES_NEEDED" in text
    assert "review findings (1)" in text
    assert "HIGH" in text
    assert "app/auth.py:12" in text
    assert "expiry never checked" in text
    assert "compare now <= exp" in text


async def test_cmd_review_degrades_without_findings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("VERDICT: PASS\nAll good.")])

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 0
    assert "no structured findings parsed" in buffer.getvalue()


async def test_cmd_review_llm_failure_is_friendly(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_review(
            ctx, repo=repo, state=None, llm=_RaisingLLM(), executor=_StubExecutor(repo)
        )


async def test_cmd_test_fix_llm_failure_is_friendly(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())
    stub = _StubExecutor(repo, [_report(ok=False), _report(ok=True)])

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_test(ctx, repo=repo, state=None, fix=True, llm=_RaisingLLM(), executor=stub)


async def test_cmd_review_streams_tokens_live(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("VERDICT: PASS\nNo issues.")])

    code = await cmd_review(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    out = buffer.getvalue()
    assert code == 0
    assert "◆ VERDICT: PASS\nNo issues." in out
    assert out.count("VERDICT: PASS") == 1


def test_render_event_does_not_repeat_streamed_content() -> None:
    from app.cli.commands import _TokenSink

    console, buffer = _console()
    sink = _TokenSink(console)
    sink.feed("streamed verdict")
    event = OrchestratorEvent(
        task_id=uuid.uuid4(),
        kind="message",
        message=ChatMessage(role=ChatRole.ASSISTANT, content="streamed verdict"),
    )
    render_event(console, event, sink)

    out = buffer.getvalue()
    assert out.count("streamed verdict") == 1
    assert out.endswith("\n")


async def test_cmd_test_fix_unconfigured_llm_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)

    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_test(ctx, repo=repo, state=None, fix=True, executor=_StubExecutor(repo))


async def test_cmd_review_unconfigured_llm_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)

    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_review(ctx, repo=repo, state=None, executor=_StubExecutor(repo))


class _TypedConsole(Console):
    """A console whose ``input`` returns a fixed value (no stdin needed)."""

    def __init__(self, value: str, buffer: StringIO | None = None) -> None:
        super().__init__(file=buffer or StringIO(), width=200, highlight=False)
        self._value = value

    def input(self, prompt: str = "", *, password: bool = False, stream=None) -> str:
        return self._value


def test_parser_pr_and_commit_generate_defaults() -> None:
    parser = build_parser()

    pr = parser.parse_args(["pr"])
    assert pr.command == "pr"
    assert pr.base is None
    assert pr.branch is None
    assert pr.remote == "origin"
    assert pr.draft is False
    assert pr.title is None
    assert pr.yes is False

    filled = parser.parse_args(
        [
            "pr",
            "--base",
            "develop",
            "--branch",
            "feat/x",
            "--remote",
            "upstream",
            "--draft",
            "--title",
            "wip",
            "-y",
        ]
    )
    assert filled.base == "develop"
    assert filled.branch == "feat/x"
    assert filled.remote == "upstream"
    assert filled.draft is True
    assert filled.title == "wip"
    assert filled.yes is True

    commit = parser.parse_args(["commit", "--generate"])
    assert commit.generate is True
    assert parser.parse_args(["commit", "-m", "m"]).generate is False


async def test_cmd_commit_generate_uses_llm_drafted_message(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "file.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("feat: add a third line")])

    code = await cmd_commit(
        ctx, repo=repo, state=None, message=None, yes=True, generate=True, llm=llm
    )

    assert code == 0
    assert len(llm.calls) == 1
    assert _git(repo, "log", "-1", "--format=%s").strip() == "feat: add a third line"


async def test_cmd_commit_generate_falls_back_to_prompt_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    (repo / "file.txt").write_text("one\nchanged\n", encoding="utf-8")

    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    buffer = StringIO()
    ctx = CliContext(console=_TypedConsole("chore: manual edit", buffer), settings=_settings())

    code = await cmd_commit(ctx, repo=repo, state=None, message=None, yes=True, generate=True)

    assert code == 0
    assert _git(repo, "log", "-1", "--format=%s").strip() == "chore: manual edit"


async def _no_gh(*_args: object, **_kwargs: object) -> bool:
    return False


async def test_cmd_pr_generates_llm_description_and_saves_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: add two")

    monkeypatch.setattr("app.cli.commands.Confirm.ask", lambda *a, **k: True)
    monkeypatch.setattr("app.cli.commands._gh_pr_create", _no_gh)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM(
        [
            _response(
                '{"title": "feat: add two", "summary": "Adds a second line.", '
                '"tests": "manual", "risks": [], "migration": null}'
            )
        ]
    )

    code = await cmd_pr(
        ctx,
        repo=repo,
        state=None,
        base="main",
        branch="feature",
        remote="origin",
        yes=True,
        llm=llm,
    )

    assert code == 0
    assert len(llm.calls) == 1
    out = buffer.getvalue()
    assert "feat: add two" in out
    assert "Adds a second line." in out
    body = (repo / ".engineer" / "pr-feature.md").read_text()
    assert "feat: add two" in body
    assert "Adds a second line." in body


async def test_cmd_pr_falls_back_without_remote_or_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: add two")

    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    monkeypatch.setattr("app.cli.commands.Confirm.ask", lambda *a, **k: True)
    monkeypatch.setattr("app.cli.commands._gh_pr_create", _no_gh)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())

    code = await cmd_pr(
        ctx, repo=repo, state=None, base="main", branch="feature", remote="origin", yes=True
    )

    assert code == 0
    out = buffer.getvalue()
    assert "no git remote configured" in out
    assert "PR body saved to" in out


async def test_cmd_pr_requires_a_feature_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="create a feature branch"):
        await cmd_pr(ctx, repo=repo, state=None, base="main", yes=True)

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "file.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: add two")

    with pytest.raises(CliError, match="not checked out"):
        await cmd_pr(ctx, repo=repo, state=None, base="main", branch="other", yes=True)


async def test_cmd_pr_rejects_no_commits(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="no commits"):
        await cmd_pr(ctx, repo=repo, state=None, base="main", branch="feature", yes=True)


def _audit_response(summary: str, verdict: str, *scores: tuple[str, int]) -> LLMResponse:
    payload = {
        "summary": summary,
        "verdict": verdict,
        "scores": [
            {"dimension": name, "score": value, "rationale": "r", "evidence": ["a.py:1"]}
            for name, value in scores
        ],
        "findings": [],
    }
    return _response(json.dumps(payload))


async def test_cmd_audit_pass_exits_zero(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_audit_response("Looks ready.", "PASS", ("security", 92), ("tests", 85))])

    code = await cmd_audit(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 0
    out = buffer.getvalue()
    assert "audit verdict: PASS" in out
    assert "audit scores (2)" in out
    assert "security" in out
    assert "92/100" in out


async def test_cmd_audit_changes_needed_exits_one(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_audit_response("Needs work.", "CHANGES_NEEDED", ("security", 45))])

    code = await cmd_audit(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    assert "audit verdict: CHANGES_NEEDED" in buffer.getvalue()


async def test_cmd_audit_derives_verdict_from_scores(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    answer = (
        '{"summary": "mixed", "scores": [{"dimension": "tests", "score": 40, "rationale": "r"}]}'
    )
    llm = FakeLLM([_response(answer)])

    code = await cmd_audit(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    out = buffer.getvalue()
    assert "no verdict stated; derived CHANGES_NEEDED" in out
    assert "audit verdict: CHANGES_NEEDED" in out


async def test_cmd_audit_renders_findings_table(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    answer = (
        '{"summary": "audit", "verdict": "CHANGES_NEEDED", "scores": [], '
        '"findings": [{"severity": "high", "file": "app/auth.py", "line": 12, '
        '"problem": "expiry never checked", "fix": "compare now <= exp"}]}'
    )
    llm = FakeLLM([_response(answer)])

    code = await cmd_audit(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    out = buffer.getvalue()
    assert "review findings (1)" in out
    assert "HIGH" in out
    assert "app/auth.py:12" in out
    assert "expiry never checked" in out


async def test_cmd_audit_prose_reply_degrades_gracefully(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_response("Looks solid. No changes needed.")])

    code = await cmd_audit(ctx, repo=repo, state=None, llm=llm, executor=_StubExecutor(repo))

    assert code == 1
    assert "audit verdict: CHANGES_NEEDED" in buffer.getvalue()


async def test_cmd_audit_llm_failure_is_friendly(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_audit(ctx, repo=repo, state=None, llm=_RaisingLLM(), executor=_StubExecutor(repo))


async def test_cmd_audit_unconfigured_llm_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)

    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_audit(ctx, repo=repo, state=None, executor=_StubExecutor(repo))
