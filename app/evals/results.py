"""Result records and the JSONL result store for the eval harness.

Each headless run appends one :class:`EvalResultRecord` to a JSONL file so
runs are durable, orderable, and comparable across models without a database.
The store is append-only and safe to read while a run is in progress.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_OUTPUT_TAIL_CHARS = 2_000


class EvalResultRecord(BaseModel):
    """The durable outcome of one benchmark run.

    Attributes:
        task_id: Benchmark identifier (see :mod:`app.evals.tasks`).
        task_name: Human-readable task name at the time of the run.
        category: Task category (security, api, data, bug, performance).
        model: LLM model identifier.
        provider: LLM provider name.
        passed: Whether the run is a success (task completed and tests pass).
        task_status: Terminal task status (``completed``, ``failed``, ...).
        tests_passed: Whether the verification suite passed.
        test_summary: One-line verification summary (or an error message).
        output_tail: Tail of the verification output, for debugging.
        attempts: How many attempts the agent consumed.
        tokens: Total tokens used (input + output).
        started_at: When the run started (UTC).
        finished_at: When the run finished (UTC).
        duration_seconds: Wall-clock duration of the run.
    """

    task_id: str
    task_name: str
    category: str
    model: str
    provider: str
    passed: bool
    task_status: str
    tests_passed: bool
    test_summary: str
    output_tail: str
    attempts: int
    tokens: int
    started_at: datetime
    finished_at: datetime
    duration_seconds: float


class ResultStore:
    """Append-only JSONL persistence for :class:`EvalResultRecord` rows.

    Attributes:
        path: The JSONL file results are appended to and loaded from.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: EvalResultRecord) -> None:
        """Append ``record`` to the store, creating the file and parent dir."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def load(self) -> list[EvalResultRecord]:
        """Return every recorded result, oldest first."""
        if not self.path.is_file():
            return []
        records: list[EvalResultRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(EvalResultRecord.model_validate_json(line))
        return records


@dataclass(frozen=True)
class ModelSummary:
    """Aggregated pass-rate statistics for one model.

    Attributes:
        model: LLM model identifier.
        runs: Number of recorded runs for the model.
        passed: Number of runs whose tests passed.
        pass_rate: Fraction of runs that passed (``0.0`` when no runs).
    """

    model: str
    runs: int
    passed: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0


def summarize(records: list[EvalResultRecord]) -> list[ModelSummary]:
    """Group ``records`` by model into pass-rate summaries.

    A run counts as passed only when its verification suite passed. Models
    are returned in descending pass-rate order (ties by total runs, then
    name) so ``engineer eval compare`` shows the best performer first.
    """
    counters: dict[str, Counter[str]] = {}
    for record in records:
        bucket = counters.setdefault(record.model, Counter({"runs": 0, "passed": 0}))
        bucket["runs"] += 1
        if record.tests_passed:
            bucket["passed"] += 1
    summaries = [
        ModelSummary(model=model, runs=counts["runs"], passed=counts["passed"])
        for model, counts in counters.items()
    ]
    summaries.sort(key=lambda summary: (-summary.pass_rate, -summary.runs, summary.model))
    return summaries


def truncate_tail(output: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    """Return the tail of ``output`` trimmed to ``limit`` characters."""
    if len(output) <= limit:
        return output
    return output[-limit:]


def utcnow() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(UTC)


def record_to_dict(record: EvalResultRecord) -> dict[str, Any]:
    """Serialize ``record`` for storage or display."""
    return record.model_dump(mode="json")
