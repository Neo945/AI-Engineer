"""Unit tests for host-side filesystem and git tools.

These handlers run in-process and are confined to the workspace root, so
they can be tested without a Docker daemon. Terminal execution (which needs
the sandbox) is covered by the integration suite.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.executor.executor import ToolExecutor
from app.tools.schemas import ToolCall, ToolName, ToolResult


@pytest.fixture
async def executor(tmp_path: Path, settings: Settings) -> AsyncIterator[ToolExecutor]:
    tool_executor = ToolExecutor.build(workspace_dir=tmp_path, settings=settings)
    yield tool_executor
    await tool_executor.sandboxes.close()


async def _call(executor: ToolExecutor, tool: ToolName, **arguments: object) -> ToolResult:
    return await executor.execute(ToolCall(tool=tool, arguments=arguments))


async def test_write_read_roundtrip(executor: ToolExecutor) -> None:
    result = await _call(
        executor, ToolName.FILE_WRITE, path="notes/hello.txt", content="hello world"
    )
    assert result.ok
    result = await _call(executor, ToolName.FILE_READ, path="notes/hello.txt")
    assert result.ok
    assert result.output == "hello world"


async def test_read_missing_file(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.FILE_READ, path="missing.txt")
    assert result.ok is False
    assert "not a file" in (result.error or "")


async def test_write_escapes_workspace(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.FILE_WRITE, path="../evil.txt", content="x")
    assert result.ok is False
    assert "escapes" in (result.error or "")


async def test_read_symlink_escape(tmp_path: Path, executor: ToolExecutor) -> None:
    outside = tmp_path.parent / "secrets"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("secret")
    (tmp_path / "link").symlink_to(secret)
    result = await _call(executor, ToolName.FILE_READ, path="link")
    assert result.ok is False


async def test_list_and_search(executor: ToolExecutor) -> None:
    await _call(executor, ToolName.FILE_WRITE, path="a.txt", content="x")
    await _call(executor, ToolName.FILE_WRITE, path="sub/b.txt", content="x")

    listed = await _call(executor, ToolName.FILE_LIST, path=".", recursive=True)
    assert listed.ok
    assert "a.txt" in listed.output
    assert "sub/b.txt" in listed.output

    found = await _call(executor, ToolName.FILE_SEARCH, pattern="**/*.txt")
    assert found.ok
    assert "a.txt" in found.output
    assert "b.txt" in found.output


async def test_search_rejects_traversal_pattern(executor: ToolExecutor) -> None:
    result = await _call(executor, ToolName.FILE_SEARCH, pattern="../../etc/passwd")
    assert result.ok is False


async def test_move_and_delete(executor: ToolExecutor) -> None:
    await _call(executor, ToolName.FILE_WRITE, path="x.txt", content="x")
    moved = await _call(executor, ToolName.FILE_MOVE, source="x.txt", destination="dir/y.txt")
    assert moved.ok
    read = await _call(executor, ToolName.FILE_READ, path="dir/y.txt")
    assert read.ok and read.output == "x"

    refused = await _call(executor, ToolName.FILE_DELETE, path="dir")
    assert refused.ok is False
    deleted = await _call(executor, ToolName.FILE_DELETE, path="dir", recursive=True)
    assert deleted.ok


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )


async def test_git_status_diff_commit_flow(tmp_path: Path, executor: ToolExecutor) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "agent@example.com")
    _git(tmp_path, "config", "user.name", "Agent")

    await _call(executor, ToolName.FILE_WRITE, path="app.py", content="print('hi')\n")
    initial = await _call(executor, ToolName.GIT_COMMIT, message="initial")
    assert initial.ok

    await _call(executor, ToolName.FILE_WRITE, path="app.py", content="print('hello')\n")

    status = await _call(executor, ToolName.GIT_STATUS)
    assert status.ok and "app.py" in status.output

    diff = await _call(executor, ToolName.GIT_DIFF)
    assert diff.ok and "+print" in diff.output

    commit = await _call(executor, ToolName.GIT_COMMIT, message="update app")
    assert commit.ok

    status = await _call(executor, ToolName.GIT_STATUS)
    assert status.ok and "app.py" not in status.output


async def test_git_commit_message_is_not_shell_interpolated(
    tmp_path: Path, executor: ToolExecutor
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "agent@example.com")
    _git(tmp_path, "config", "user.name", "Agent")
    await _call(executor, ToolName.FILE_WRITE, path="a.txt", content="x")

    payload = "clean; rm -rf /tmp/agent-pwned && echo hacked"
    commit = await _call(executor, ToolName.GIT_COMMIT, message=payload)
    assert commit.ok
    assert not Path("/tmp/agent-pwned").exists()
