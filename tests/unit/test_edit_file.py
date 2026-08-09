"""Unit tests for the unified-diff ``edit_file`` tool and patch applier."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.executor.executor import ToolExecutor
from app.executor.patch import PatchError, apply_unified_diff
from app.tools.schemas import ToolCall, ToolName, ToolResult


@pytest.fixture
async def executor(tmp_path: Path, settings: Settings) -> AsyncIterator[ToolExecutor]:
    tool_executor = ToolExecutor.build(workspace_dir=tmp_path, settings=settings)
    yield tool_executor
    await tool_executor.sandboxes.close()


async def _call(executor: ToolExecutor, tool: ToolName, **arguments: object) -> ToolResult:
    return await executor.execute(ToolCall(tool=tool, arguments=arguments))


def _content() -> str:
    return "def existing():\n    pass\n\n\ndef triple(x):\n    return x * 3\n"


def test_apply_replace_hunk() -> None:
    diff = """--- a/sample.py
+++ b/sample.py
@@ -1,3 +1,3 @@
 def existing():
-    pass
+    return 1
"""
    new, edit = apply_unified_diff(_content(), diff)
    assert "    return 1" in new
    assert "    pass" not in new
    assert edit.old_lines == 2 and edit.new_lines == 2 and edit.hunks == 1


def test_apply_insertion_hunk() -> None:
    diff = """@@ -2,0 +3,2 @@
+def helper():
+    return None
"""
    new, _ = apply_unified_diff(_content(), diff)
    assert "def helper():\n    return None\n    pass" in new


def test_apply_deletion_hunk() -> None:
    diff = """@@ -1,2 +1,1 @@
 def existing():
-    pass
"""
    new, edit = apply_unified_diff(_content(), diff)
    assert new == "def existing():\n\n\ndef triple(x):\n    return x * 3\n"
    assert edit.old_lines == 2 and edit.new_lines == 1


def test_apply_multiple_hunks_in_order() -> None:
    diff = """@@ -1,2 +1,2 @@
 def existing():
-    pass
+    return 1
@@ -4,2 +4,2 @@
 def triple(x):
-    return x * 3
+    return x * 2
"""
    new, edit = apply_unified_diff(_content(), diff)
    assert "    return 1" in new and "    return x * 2" in new
    assert "    return x * 3" not in new and "    pass" not in new
    assert edit.hunks == 2


def test_apply_tolerates_position_drift() -> None:
    # Hunk claims line 1 but content moved; exact block match elsewhere wins.
    moved = "def before():\n    pass\n\n" + _content()
    diff = """@@ -1,2 +1,2 @@
 def existing():
-    pass
+    return 1
"""
    new, _ = apply_unified_diff(moved, diff)
    assert new.startswith("def before():\n    pass\n\n")
    assert "def existing():\n    return 1" in new
    assert "    return x * 3" in new


def test_apply_missing_context_raises() -> None:
    diff = """@@ -1,1 +1,1 @@
- never seen
+ replaced
"""
    with pytest.raises(PatchError, match="does not match"):
        apply_unified_diff(_content(), diff)


def test_apply_garbage_raises() -> None:
    with pytest.raises(PatchError, match="hunk header"):
        apply_unified_diff(_content(), "this is not a diff")


def test_apply_no_hunks_raises() -> None:
    with pytest.raises(PatchError, match="no hunks"):
        apply_unified_diff(_content(), "--- a/x\n+++ b/x\n")


def test_apply_preserves_trailing_newline() -> None:
    new, _ = apply_unified_diff("a\nb\n", "@@ -1,1 +1,1 @@\n-a\n+b\n")
    assert new == "b\nb\n"


async def test_edit_file_happy_path(tmp_path: Path, executor: ToolExecutor) -> None:
    (tmp_path / "sample.py").write_text(_content())
    diff = """@@ -1,3 +1,3 @@
 def existing():
-    pass
+    return 1
"""
    result = await _call(executor, ToolName.FILE_EDIT, path="sample.py", diff=diff)
    assert result.ok
    assert "1 hunk" in result.output
    assert "    return 1" in (tmp_path / "sample.py").read_text()


async def test_edit_file_missing_file(executor: ToolExecutor) -> None:
    result = await _call(
        executor, ToolName.FILE_EDIT, path="nope.py", diff="@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert result.ok is False
    assert "not a file" in (result.error or "")


async def test_edit_file_stale_diff_fails_loudly(tmp_path: Path, executor: ToolExecutor) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    stale = """@@ -1,1 +1,1 @@
- x = 1
+ x = 2
"""
    result = await _call(executor, ToolName.FILE_EDIT, path="app.py", diff=stale)
    assert result.ok is False
    assert "does not match" in (result.error or "")
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


async def test_edit_file_rejects_path_escape(executor: ToolExecutor) -> None:
    result = await _call(
        executor, ToolName.FILE_EDIT, path="../evil.txt", diff="@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert result.ok is False
    assert "escapes" in (result.error or "")


async def test_edit_file_roundtrips_with_file_read(tmp_path: Path, executor: ToolExecutor) -> None:
    (tmp_path / "data.txt").write_text("line1\nline2\nline3\n")
    diff = """@@ -1,3 +1,3 @@
 line1
 line2
-line3
+line3!
"""
    edited = await _call(executor, ToolName.FILE_EDIT, path="data.txt", diff=diff)
    assert edited.ok
    read = await _call(executor, ToolName.FILE_READ, path="data.txt")
    assert read.ok and read.output == "line1\nline2\nline3!\n"
