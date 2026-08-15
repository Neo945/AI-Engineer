"""Unit tests for the new git tools and the dirty-working-tree protection.

The protection snapshots the set of pre-existing uncommitted paths on first
mutation and refuses to modify or commit any path in that baseline, so the
agent cannot clobber changes that existed before the session started.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.executor.executor import ToolExecutor
from app.executor.git import is_valid_branch
from app.tools.schemas import ToolCall, ToolName, ToolResult


@pytest.fixture
async def executor(tmp_path: Path, settings: Settings) -> AsyncIterator[ToolExecutor]:
    tool_executor = ToolExecutor.build(workspace_dir=tmp_path, settings=settings)
    yield tool_executor
    await tool_executor.sandboxes.close()


async def _call(executor: ToolExecutor, tool: ToolName, **arguments: object) -> ToolResult:
    return await executor.execute(ToolCall(tool=tool, arguments=arguments))


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(workspace: Path) -> None:
    """Create a repo with a clean initial commit containing ``a.txt``."""
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.email", "agent@example.com")
    _git(workspace, "config", "user.name", "Agent")
    (workspace / "a.txt").write_text("alpha\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "initial")


# --- git tools ------------------------------------------------------------


async def test_git_log_lists_commits(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    await _call(executor, ToolName.FILE_WRITE, path="b.txt", content="b")
    await _call(executor, ToolName.GIT_COMMIT, message="add b")

    log = await _call(executor, ToolName.GIT_LOG, limit=1)
    assert log.ok
    assert "add b" in log.output
    assert "initial" not in log.output

    log_all = await _call(executor, ToolName.GIT_LOG)
    assert log_all.ok
    assert "initial" in log_all.output


async def test_git_branch_create_and_list(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    created = await _call(executor, ToolName.GIT_BRANCH, create="feature/otel")
    assert created.ok

    listed = await _call(executor, ToolName.GIT_BRANCH)
    assert listed.ok
    assert "main" in listed.output
    assert "* feature/otel" in listed.output

    back = await _call(executor, ToolName.GIT_CHECKOUT, branch="main")
    assert back.ok
    current = await _call(executor, ToolName.GIT_BRANCH)
    assert "* main" in current.output


async def test_git_branch_rejects_invalid_names(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    for name in ("bad branch", "evil..dot", "..relative", "-leading-dash", "@{ref}"):
        result = await _call(executor, ToolName.GIT_BRANCH, create=name)
        assert result.ok is False
        assert "invalid branch name" in (result.error or "")
        result = await _call(executor, ToolName.GIT_CHECKOUT, branch=name)
        assert result.ok is False


async def test_git_push_pushes_to_remote(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    await _call(executor, ToolName.GIT_BRANCH, create="feature")
    bare = tmp_path / "remote.git"
    _git(bare.parent, "init", "-q", "--bare", bare.name)
    _git(tmp_path, "remote", "add", "origin", str(bare))

    pushed = await _call(executor, ToolName.GIT_PUSH, remote="origin")
    assert pushed.ok, pushed.error
    assert "feature" in pushed.output

    branches = _git(bare, "branch", "--no-color").stdout
    assert "feature" in branches


async def test_git_push_fails_without_remote(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    pushed = await _call(executor, ToolName.GIT_PUSH)
    assert pushed.ok is False
    assert "origin" in (pushed.error or "")


# --- dirty-working-tree protection ----------------------------------------


async def test_write_to_pre_existing_dirty_file_is_blocked(
    executor: ToolExecutor, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed outside the agent\n")

    result = await _call(executor, ToolName.FILE_WRITE, path="a.txt", content="agent write")
    assert result.ok is False
    assert "uncommitted changes from before this session" in (result.error or "")
    assert (tmp_path / "a.txt").read_text() == "changed outside the agent\n"


async def test_edit_delete_move_of_pre_existing_dirty_paths_are_blocked(
    executor: ToolExecutor, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed outside the agent\n")
    (tmp_path / "b.txt").write_text("tracked too\n")
    _git(tmp_path, "add", "b.txt")
    _git(tmp_path, "commit", "-q", "-m", "add b")
    (tmp_path / "b.txt").write_text("also dirty\n")

    edited = await _call(
        executor,
        ToolName.FILE_EDIT,
        path="a.txt",
        diff="@@ -1 +1 @@\n- changed outside\n+ agent\n",
    )
    assert edited.ok is False
    assert "uncommitted changes from before this session" in (edited.error or "")

    deleted = await _call(executor, ToolName.FILE_DELETE, path="a.txt")
    assert deleted.ok is False

    moved = await _call(executor, ToolName.FILE_MOVE, source="a.txt", destination="a2.txt")
    assert moved.ok is False

    moved_into = await _call(executor, ToolName.FILE_MOVE, source="b.txt", destination="a.txt")
    assert moved_into.ok is False


async def test_new_paths_are_not_blocked(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("pre-existing dirty\n")

    result = await _call(executor, ToolName.FILE_WRITE, path="new.txt", content="agent")
    assert result.ok
    result = await _call(executor, ToolName.FILE_READ, path="new.txt")
    assert result.ok and result.output == "agent"


async def test_untracked_pre_existing_file_is_blocked(
    executor: ToolExecutor, tmp_path: Path
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("agent should not touch\n")

    result = await _call(executor, ToolName.FILE_WRITE, path="untracked.txt", content="mine")
    assert result.ok is False
    assert "uncommitted changes from before this session" in (result.error or "")


async def test_commit_refuses_while_baseline_dirty(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("pre-existing dirty\n")

    result = await _call(executor, ToolName.GIT_COMMIT, message="should refuse")
    assert result.ok is False
    assert "refusing to commit" in (result.error or "")
    assert "a.txt" in (result.error or "")


async def test_baseline_refreshed_after_commit(executor: ToolExecutor, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("pre-existing dirty\n")

    await _call(executor, ToolName.FILE_WRITE, path="new.txt", content="agent")
    blocked = await _call(executor, ToolName.FILE_WRITE, path="a.txt", content="no")
    assert blocked.ok is False

    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "external clean-up")

    still_blocked = await _call(executor, ToolName.FILE_WRITE, path="a.txt", content="no")
    assert still_blocked.ok is False

    committed = await _call(executor, ToolName.GIT_COMMIT, message="agent work")
    assert committed.ok

    allowed = await _call(executor, ToolName.FILE_WRITE, path="a.txt", content="mine")
    assert allowed.ok
    assert (tmp_path / "a.txt").read_text() == "mine"


async def test_protection_can_be_disabled(tmp_path: Path, settings: Settings) -> None:
    _init_repo(tmp_path)
    settings = settings.model_copy(update={"git_protect_dirty_tree": False})
    tool_executor = ToolExecutor.build(workspace_dir=tmp_path, settings=settings)
    try:
        result = await _call(tool_executor, ToolName.FILE_WRITE, path="a.txt", content="allowed")
        assert result.ok
        assert (tmp_path / "a.txt").read_text() == "allowed"
    finally:
        await tool_executor.sandboxes.close()


def test_is_valid_branch_rules() -> None:
    assert is_valid_branch("main")
    assert is_valid_branch("feature/otel-1.2")
    assert is_valid_branch("release/v1.2.3")
    assert not is_valid_branch("has space")
    assert not is_valid_branch("..relative")
    assert not is_valid_branch("feature..x")
    assert not is_valid_branch("-leading")
    assert not is_valid_branch("trailing.")
    assert not is_valid_branch("branch.lock")
    assert not is_valid_branch("refs/@{push}")
    assert not is_valid_branch("tab\tinside")
