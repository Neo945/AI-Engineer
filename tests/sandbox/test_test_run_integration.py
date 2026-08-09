"""End-to-end tests for the ``test_run`` tool inside the sandbox.

The executor image ships pytest, so these exercise the full path the
repair agent uses: run a real suite in the container, parse the structured
report, and surface failures back to the caller.
"""

from __future__ import annotations

import pytest

from app.executor.executor import ToolExecutor
from app.tools.schemas import ToolCall, ToolName, ToolResult

pytestmark = pytest.mark.integration


async def _call(executor: ToolExecutor, tool: ToolName, **arguments: object) -> ToolResult:
    return await executor.execute(ToolCall(tool=tool, arguments=arguments))


async def test_test_run_passing_suite(executor: ToolExecutor) -> None:
    tests = executor.workspace_dir / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_one():\n    assert 1 == 1\n")

    result = await _call(executor, ToolName.TEST_RUN)

    assert result.ok is True
    assert result.exit_code == 0
    report = result.data["report"]
    assert report["framework"] == "pytest"
    assert report["passed"] == 1
    assert report["failed"] == 0
    assert "All tests pass." in result.output


async def test_test_run_failing_suite_parses_failures(executor: ToolExecutor) -> None:
    tests = executor.workspace_dir / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 == 1\n\ndef test_bad():\n    assert 1 == 2\n"
    )

    result = await _call(executor, ToolName.TEST_RUN)

    assert result.ok is False
    report = result.data["report"]
    assert report["passed"] == 1
    assert report["failed"] == 1
    assert any("test_bad" in failure["test_id"] for failure in report["failures"])
    assert "test(s) failed" in (result.error or "")


async def test_test_run_explicit_command(executor: ToolExecutor) -> None:
    tests = executor.workspace_dir / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_x():\n    assert True\n")

    result = await _call(
        executor,
        ToolName.TEST_RUN,
        command="python -m pytest tests/test_a.py -q",
        framework="pytest",
    )

    assert result.ok is True
    assert result.data["report"]["passed"] == 1


async def test_test_run_workdir(executor: ToolExecutor) -> None:
    sub = executor.workspace_dir / "sub"
    sub.mkdir()
    (sub / "test_sub.py").write_text("def test_s():\n    assert 2 == 2\n")

    result = await _call(
        executor,
        ToolName.TEST_RUN,
        command="python -m pytest -q",
        framework="pytest",
        workdir="sub",
    )

    assert result.ok is True
    assert result.data["report"]["passed"] == 1


async def test_test_run_collection_error_reported(executor: ToolExecutor) -> None:
    tests = executor.workspace_dir / "tests"
    tests.mkdir()
    (tests / "test_broken.py").write_text("def test_nope(:\n    pass\n")

    result = await _call(executor, ToolName.TEST_RUN)

    assert result.ok is False
    assert result.data["report"]["errors"] >= 1
