"""Unit tests for the benchmark task registry and scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evals.tasks import BENCHMARK_TASKS, EvalTask, scaffold, task_by_id, task_ids


def test_registry_has_six_unique_tasks() -> None:
    assert len(BENCHMARK_TASKS) == 6
    ids = [task.id for task in BENCHMARK_TASKS]
    assert len(ids) == len(set(ids))
    assert task_ids() == ids


@pytest.mark.parametrize("task", BENCHMARK_TASKS, ids=lambda task: task.id)
def test_each_task_is_well_formed(task: EvalTask) -> None:
    assert task.id
    assert task.name
    assert task.category
    assert task.goal
    assert task.test_command
    assert task.files
    assert all(relative and not relative.startswith("/") for relative in task.files)
    assert task.timeout_seconds > 0


def test_each_task_has_a_test_file() -> None:
    for task in BENCHMARK_TASKS:
        assert any(relative.startswith("test_") for relative in task.files)


def test_task_by_id_known_and_unknown() -> None:
    assert task_by_id("fix-auth-bug").name == "fix auth expiry check"
    with pytest.raises(KeyError):
        task_by_id("does-not-exist")


def test_scaffold_writes_all_files(tmp_path: Path) -> None:
    task = BENCHMARK_TASKS[0]
    scaffold(task, tmp_path)
    for relative, content in task.files.items():
        assert (tmp_path / relative).read_text(encoding="utf-8") == content


def test_scaffold_creates_nested_directories(tmp_path: Path) -> None:
    task = EvalTask(
        id="nested",
        name="nested",
        category="bug",
        goal="g",
        files={"pkg/sub/mod.py": "VALUE = 1\n"},
    )
    scaffold(task, tmp_path)
    assert (tmp_path / "pkg" / "sub" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_scaffold_rejects_path_traversal(tmp_path: Path) -> None:
    task = EvalTask(
        id="evil",
        name="evil",
        category="bug",
        goal="g",
        files={"../escape.txt": "pwn"},
    )
    with pytest.raises(ValueError, match="escapes workspace"):
        scaffold(task, tmp_path)
    assert not (tmp_path.parent / "escape.txt").exists()
