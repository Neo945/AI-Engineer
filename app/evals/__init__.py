"""Evaluation harness (Phase 11).

Headless task runner for SWE-bench-style regression suites that measure
agent correctness across builds, guarding against silent quality regressions.
"""

from __future__ import annotations

from app.evals.results import EvalResultRecord, ModelSummary, ResultStore, summarize
from app.evals.runner import EvalProvision, EvalRunner
from app.evals.tasks import BENCHMARK_TASKS, EvalTask, scaffold, task_by_id, task_ids

__all__ = [
    "BENCHMARK_TASKS",
    "EvalProvision",
    "EvalResultRecord",
    "EvalRunner",
    "EvalTask",
    "ModelSummary",
    "ResultStore",
    "scaffold",
    "summarize",
    "task_by_id",
    "task_ids",
]
