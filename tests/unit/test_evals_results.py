"""Unit tests for the eval result store and model summaries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.evals.results import EvalResultRecord, ModelSummary, ResultStore, summarize, truncate_tail


def _record(
    *,
    task_id: str = "fix-auth-bug",
    model: str = "gpt-4o-mini",
    passed: bool = True,
    tests_passed: bool | None = None,
) -> EvalResultRecord:
    return EvalResultRecord(
        task_id=task_id,
        task_name="t",
        category="security",
        model=model,
        provider="openai",
        passed=passed,
        task_status="completed",
        tests_passed=passed if tests_passed is None else tests_passed,
        test_summary="3 passed, 0 failed",
        output_tail="ok",
        attempts=1,
        tokens=10,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
        duration_seconds=5.0,
    )


def test_store_appends_and_loads_round_trip(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "results.jsonl")
    store.append(_record())
    store.append(_record(task_id="optimize-query", passed=False))

    loaded = store.load()
    assert len(loaded) == 2
    assert loaded[0].task_id == "fix-auth-bug"
    assert loaded[0].passed is True
    assert loaded[1].task_id == "optimize-query"
    assert loaded[1].passed is False
    assert loaded[1].finished_at == datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)


def test_store_load_empty_missing_file(tmp_path: Path) -> None:
    assert ResultStore(tmp_path / "missing.jsonl").load() == []


def test_store_creates_parent_directories(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "deep" / "nested" / "results.jsonl")
    store.append(_record())
    assert store.load()[0].task_id == "fix-auth-bug"


def test_store_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text("\n" + _record().model_dump_json() + "\n\n", encoding="utf-8")
    assert len(ResultStore(path).load()) == 1


def test_summarize_groups_and_orders_models() -> None:
    records = [
        _record(model="model-a", passed=True),
        _record(model="model-a", passed=True),
        _record(model="model-b", passed=False),
        _record(model="model-b", passed=True),
        _record(model="model-c", passed=False),
    ]
    summaries = summarize(records)
    assert summaries == [
        ModelSummary(model="model-a", runs=2, passed=2),
        ModelSummary(model="model-b", runs=2, passed=1),
        ModelSummary(model="model-c", runs=1, passed=0),
    ]
    assert [round(summary.pass_rate, 2) for summary in summaries] == [1.0, 0.5, 0.0]


def test_summarize_empty() -> None:
    assert summarize([]) == []


def test_summarize_counts_tests_passed_not_passed() -> None:
    record = _record(passed=False, tests_passed=True)
    assert summarize([record])[0].passed == 1


def test_truncate_tail() -> None:
    assert truncate_tail("short") == "short"
    long = "x" * 5000
    assert truncate_tail(long, limit=100) == "x" * 100
    assert truncate_tail(long) == long[-2000:]
