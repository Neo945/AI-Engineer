"""Unit tests for the headless eval runner.

The runner is exercised with injected provisioner, orchestrator, and verifier
fakes so no database, sandbox, or real LLM is required. The git scaffold is
real (needs the ``git`` binary) so the fixture workspaces are inspectable.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.unit.fake_llm import FakeLLM

from app.core.config import Settings
from app.database.models.enums import TaskStatus
from app.database.models.task import Task
from app.evals.results import ResultStore
from app.evals.runner import EvalProvision, EvalRunner
from app.evals.tasks import EvalTask, task_by_id
from app.executor.test_parser import TestReport as _TestReport


def _settings() -> Settings:
    return Settings(_env_file=None)


def _task() -> EvalTask:
    return EvalTask(
        id="unit-task",
        name="unit",
        category="bug",
        goal="Fix it",
        files={"app.py": "VALUE = 1\n", "test_app.py": "pass\n"},
        timeout_seconds=30,
    )


def _provision() -> EvalProvision:
    return EvalProvision(
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
    )


def _task_row(
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    attempt: int = 1,
    error: str | None = None,
) -> Task:
    return Task(
        session_id=uuid.uuid4(),
        agent_type="coder",
        goal="Fix it",
        status=status,
        attempt=attempt,
        input_tokens=10,
        output_tokens=5,
        error=error,
    )


def _passing_report() -> _TestReport:
    return _TestReport(
        framework="generic",
        command="python -m unittest",
        passed=3,
        output="Ran 3 tests ... OK",
        exit_code=0,
    )


def _failing_report() -> _TestReport:
    return _TestReport(
        framework="generic",
        command="python -m unittest",
        failed=1,
        failures=[],
        output="FAIL: test_x (test_app)",
        exit_code=1,
    )


class _OrchestratorStub:
    def __init__(self, result: Task | Exception, delay: float = 0) -> None:
        self._result = result
        self._delay = delay
        self.called_with: uuid.UUID | None = None

    async def run_task(self, task_id: uuid.UUID) -> Task:
        self.called_with = task_id
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _VerifierStub:
    def __init__(self, report: _TestReport) -> None:
        self._report = report
        self.called = False
        self.workspace_dir: Path | None = None

    async def verify(self, task: EvalTask, workspace_dir: Path) -> _TestReport:
        self.called = True
        self.workspace_dir = workspace_dir
        return self._report


def _runner(
    *,
    provision: EvalProvision | None = None,
    orchestrator: _OrchestratorStub | None = None,
    verifier: _VerifierStub | None = None,
    timeout: int | None = None,
    settings: Settings | None = None,
) -> EvalRunner:
    settings = settings or _settings()
    return EvalRunner(
        session_factory=async_sessionmaker(),
        llm=FakeLLM(),
        settings=settings,
        timeout_seconds=timeout,
        provisioner=lambda _task, _ws: _make_awaitable(provision or _provision()),
        orchestrator_factory=lambda _provision: orchestrator or _OrchestratorStub(_task_row()),
        verify_factory=lambda _settings: verifier or _VerifierStub(_passing_report()),
    )


def _make_awaitable(value: EvalProvision):
    async def _await() -> EvalProvision:
        return value

    return _await()


@pytest.mark.asyncio
async def test_run_passes_when_tests_pass(tmp_path: Path) -> None:
    verifier = _VerifierStub(_passing_report())
    runner = _runner(verifier=verifier)

    record = await runner.run(_task(), tmp_path)

    assert record.passed is True
    assert record.task_status == "completed"
    assert record.tests_passed is True
    assert record.attempts == 1
    assert record.tokens == 15
    assert record.test_summary == "3 passed, 0 failed"
    assert verifier.called is True
    assert verifier.workspace_dir == tmp_path


@pytest.mark.asyncio
async def test_run_fails_when_tests_fail(tmp_path: Path) -> None:
    runner = _runner(verifier=_VerifierStub(_failing_report()))

    record = await runner.run(_task(), tmp_path)

    assert record.passed is False
    assert record.tests_passed is False
    assert record.task_status == "completed"


@pytest.mark.asyncio
async def test_run_skips_verify_when_task_fails(tmp_path: Path) -> None:
    verifier = _VerifierStub(_passing_report())
    orchestrator = _OrchestratorStub(_task_row(status=TaskStatus.FAILED))
    runner = _runner(orchestrator=orchestrator, verifier=verifier)

    record = await runner.run(_task(), tmp_path)

    assert record.passed is False
    assert record.tests_passed is False
    assert record.task_status == "failed"
    assert verifier.called is False
    assert record.test_summary == "task did not complete"


@pytest.mark.asyncio
async def test_run_surfaces_task_error_in_summary(tmp_path: Path) -> None:
    verifier = _VerifierStub(_passing_report())
    orchestrator = _OrchestratorStub(
        _task_row(status=TaskStatus.FAILED, error="FreeUsageLimitError: rate limited")
    )
    runner = _runner(orchestrator=orchestrator, verifier=verifier)

    record = await runner.run(_task(), tmp_path)

    assert record.passed is False
    assert record.task_status == "failed"
    assert verifier.called is False
    assert record.test_summary == "FreeUsageLimitError: rate limited"


@pytest.mark.asyncio
async def test_run_records_orchestrator_exception(tmp_path: Path) -> None:
    runner = _runner(orchestrator=_OrchestratorStub(RuntimeError("boom")))

    record = await runner.run(_task(), tmp_path)

    assert record.passed is False
    assert record.task_status == "errored"
    assert "boom" in record.test_summary


@pytest.mark.asyncio
async def test_run_times_out(tmp_path: Path) -> None:
    slow = _OrchestratorStub(_task_row(), delay=60)
    runner = _runner(orchestrator=slow, timeout=1)

    record = await runner.run(_task(), tmp_path)

    assert record.passed is False
    assert record.task_status == "errored"
    assert "timed out" in record.test_summary
    assert slow.called_with is not None


@pytest.mark.asyncio
async def test_run_appends_to_store(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    runner = _runner()

    record = await runner.run(_task(), tmp_path / "ws", store=store)

    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].model_dump() == record.model_dump()
    assert record.started_at <= record.finished_at
    assert record.duration_seconds >= 0


@pytest.mark.asyncio
async def test_run_scaffolds_git_repo(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    runner = _runner()

    await runner.run(_task(), workspace)

    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace / ".git").is_dir()


@pytest.mark.asyncio
async def test_run_uses_task_specific_timeout(tmp_path: Path) -> None:
    slow = _OrchestratorStub(_task_row(), delay=60)
    task = _task()
    task.timeout_seconds = 1
    runner = _runner(orchestrator=slow)

    record = await runner.run(task, tmp_path)

    assert record.task_status == "errored"
    assert "timed out" in record.test_summary


@pytest.mark.asyncio
async def test_task_by_id_fixture_runs_in_runner(tmp_path: Path) -> None:
    task = task_by_id("fix-failing-test")
    verifier = _VerifierStub(_passing_report())
    runner = _runner(verifier=verifier)

    record = await runner.run(task, tmp_path)

    assert record.task_id == "fix-failing-test"
    assert record.task_name == "fix duration parser"
    assert record.category == "bug"
