"""End-to-end integration tests through the ToolExecutor.

These drive the full path the orchestrator will use: a typed ToolCall is
validated, dispatched, and executed (filesystem on the host, terminal inside
the sandbox), returning a ToolResult.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.executor.executor import ToolExecutor
from app.tools.schemas import ToolCall, ToolName, ToolResult

pytestmark = pytest.mark.integration


async def _call(executor: ToolExecutor, tool: ToolName, **arguments: object) -> ToolResult:
    return await executor.execute(ToolCall(tool=tool, arguments=arguments))


async def test_terminal_echo(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.TERMINAL_RUN, command="echo hello")
    assert result.ok is True
    assert "hello" in result.output
    assert result.exit_code == 0


async def test_terminal_exit_code_and_stderr(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.TERMINAL_RUN, command="echo boom 1>&2; exit 3")
    assert result.ok is False
    assert result.exit_code == 3
    assert "boom" in (result.error or "")


async def test_terminal_timeout_via_call(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.TERMINAL_RUN, command="sleep 30", timeout_ms=1000)
    assert result.ok is False
    assert "timed out" in (result.error or "")
    assert result.truncated is True


async def test_terminal_and_filesystem_share_workspace(executor: ToolExecutor) -> None:
    created = await _call(executor, ToolName.TERMINAL_RUN, command="echo data > shared.txt")
    assert created.ok is True
    read = await _call(executor, ToolName.FILE_READ, path="shared.txt")
    assert read.ok is True
    assert read.output == "data\n"


async def test_terminal_workdir(executor: ToolExecutor) -> None:
    await _call(executor, ToolName.FILE_WRITE, path="sub/keep.txt", content="x")
    result = await _call(executor, ToolName.TERMINAL_RUN, command="pwd", workdir="sub")
    assert result.ok is True
    assert result.output.strip() == "/workspace/sub"


async def test_terminal_rejects_workdir_escape(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.TERMINAL_RUN, command="pwd", workdir="../escape")
    assert result.ok is False


async def test_terminal_denies_destructive_command(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.TERMINAL_RUN, command="rm -rf build")
    assert result.ok is False
    assert "safety policy" in (result.error or "")
    assert "file_delete" in (result.error or "")
    assert not Path("build").exists()


async def test_terminal_requires_confirm_for_destructive_command(
    executor: ToolExecutor,
) -> None:
    result = await _call(executor, ToolName.TERMINAL_RUN, command="git push origin main")
    assert result.ok is False
    assert "confirm=true" in (result.error or "")

    confirmed = await _call(
        executor,
        ToolName.TERMINAL_RUN,
        command="git push origin main",
        confirm=True,
    )
    assert confirmed.ok or "confirm" not in (confirmed.error or "")
