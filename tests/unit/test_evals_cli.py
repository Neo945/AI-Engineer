"""Unit tests for the ``engineer eval`` CLI surface.

These need no infrastructure: parser dispatch, the listing/rendering
commands, and the headless run path are exercised with a fake runner and a
fake LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.cli.commands import cmd_eval_compare, cmd_eval_list, cmd_eval_results, cmd_eval_run
from app.cli.context import CliContext, CliError
from app.cli.main import arun, build_parser
from app.core.config import Settings
from app.evals.results import EvalResultRecord


def _settings() -> Settings:
    return Settings(_env_file=None)


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


def _record(*, passed: bool = True) -> EvalResultRecord:
    return EvalResultRecord(
        task_id="fix-auth-bug",
        task_name="fix auth expiry check",
        category="security",
        model="gpt-4o-mini",
        provider="openai",
        passed=passed,
        task_status="completed",
        tests_passed=passed,
        test_summary="4 passed, 0 failed",
        output_tail="ok",
        attempts=1,
        tokens=42,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
        duration_seconds=5.0,
    )


class _RunnerStub:
    def __init__(self, record: EvalResultRecord) -> None:
        self._record = record
        self.calls: list[dict] = []

    async def run(self, task, workspace_dir, *, store=None) -> EvalResultRecord:
        self.calls.append({"task": task, "workspace_dir": workspace_dir, "store": store})
        Path(workspace_dir).mkdir(parents=True, exist_ok=True)
        store.append(self._record)
        return self._record


def test_parser_dispatches_eval_subcommands() -> None:
    parser = build_parser()
    assert parser.parse_args(["eval", "list"]).eval_handler.__name__ == "_cmd_eval_list"
    run = parser.parse_args(["eval", "run", "fix-auth-bug", "--keep", "--timeout", "60"])
    assert run.eval_handler.__name__ == "_cmd_eval_run"
    assert run.keep is True
    assert run.timeout == 60
    assert parser.parse_args(["eval", "results"]).eval_handler.__name__ == "_cmd_eval_results"
    assert parser.parse_args(["eval", "compare"]).eval_handler.__name__ == "_cmd_eval_compare"


def test_parser_requires_eval_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval"])


@pytest.mark.asyncio
async def test_arun_eval_list_without_repo(tmp_path: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    code = await arun(["eval", "list"], ctx)
    assert code == 0
    assert "benchmark tasks" in buffer.getvalue()
    assert "fix-auth-bug" in buffer.getvalue()


@pytest.mark.asyncio
async def test_cmd_eval_list_renders_tasks() -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    assert await cmd_eval_list(ctx, repo=Path.cwd()) == 0
    text = buffer.getvalue()
    assert "6" in text
    assert "fix-auth-bug" in text


@pytest.mark.asyncio
async def test_cmd_eval_run_passes(tmp_path: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    runner = _RunnerStub(_record(passed=True))
    code = await cmd_eval_run(
        ctx,
        task_id="fix-auth-bug",
        workspace=None,
        keep=False,
        timeout=None,
        results_path=str(tmp_path / "results.jsonl"),
        llm=FakeLLM(),
        runner=runner,  # type: ignore[arg-type]
    )
    assert code == 0
    assert "PASS" in buffer.getvalue()
    assert runner.calls[0]["task"].id == "fix-auth-bug"
    assert (tmp_path / "results.jsonl").is_file()


@pytest.mark.asyncio
async def test_cmd_eval_run_fails_returns_1(tmp_path: Path) -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())
    runner = _RunnerStub(_record(passed=False))
    code = await cmd_eval_run(
        ctx,
        task_id="fix-auth-bug",
        workspace=None,
        keep=False,
        timeout=None,
        results_path=str(tmp_path / "results.jsonl"),
        llm=FakeLLM(),
        runner=runner,  # type: ignore[arg-type]
    )
    assert code == 1


@pytest.mark.asyncio
async def test_cmd_eval_run_unknown_task() -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())
    with pytest.raises(CliError, match="unknown benchmark task"):
        await cmd_eval_run(
            ctx,
            task_id="nope",
            workspace=None,
            keep=False,
            timeout=None,
            results_path=None,
            llm=FakeLLM(),
        )


@pytest.mark.asyncio
async def test_cmd_eval_run_keeps_provided_workspace(tmp_path: Path) -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())
    workspace = tmp_path / "ws"
    runner = _RunnerStub(_record())
    await cmd_eval_run(
        ctx,
        task_id="fix-auth-bug",
        workspace=str(workspace),
        keep=False,
        timeout=None,
        results_path=str(tmp_path / "results.jsonl"),
        llm=FakeLLM(),
        runner=runner,  # type: ignore[arg-type]
    )
    assert workspace.is_dir()


@pytest.mark.asyncio
async def test_cmd_eval_results_renders(tmp_path: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    results_path = str(tmp_path / "results.jsonl")
    await cmd_eval_run(
        ctx,
        task_id="fix-auth-bug",
        workspace=str(tmp_path / "ws"),
        keep=False,
        timeout=None,
        results_path=results_path,
        llm=FakeLLM(),
        runner=_RunnerStub(_record()),
    )
    code = await cmd_eval_results(ctx, model=None, results_path=results_path)
    assert code == 0
    text = buffer.getvalue()
    assert "eval results" in text
    assert "PASS" in text


@pytest.mark.asyncio
async def test_cmd_eval_results_filters_by_model(tmp_path: Path) -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())
    results_path = str(tmp_path / "results.jsonl")
    await cmd_eval_run(
        ctx,
        task_id="fix-auth-bug",
        workspace=str(tmp_path / "ws"),
        keep=False,
        timeout=None,
        results_path=results_path,
        llm=FakeLLM(),
        runner=_RunnerStub(_record()),
    )
    assert await cmd_eval_results(ctx, model="gpt-4o-mini", results_path=results_path) == 0
    assert await cmd_eval_results(ctx, model="other-model", results_path=results_path) == 1


@pytest.mark.asyncio
async def test_cmd_eval_compare_renders(tmp_path: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    results_path = str(tmp_path / "results.jsonl")
    await cmd_eval_run(
        ctx,
        task_id="fix-auth-bug",
        workspace=str(tmp_path / "ws"),
        keep=False,
        timeout=None,
        results_path=results_path,
        llm=FakeLLM(),
        runner=_RunnerStub(_record(passed=True)),
    )
    await cmd_eval_run(
        ctx,
        task_id="optimize-query",
        workspace=str(tmp_path / "ws2"),
        keep=False,
        timeout=None,
        results_path=results_path,
        llm=FakeLLM(),
        runner=_RunnerStub(_record(passed=False)),
    )
    assert await cmd_eval_compare(ctx, results_path=results_path) == 0
    assert "model comparison" in buffer.getvalue()
    assert "50%" in buffer.getvalue()
