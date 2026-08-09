"""Parse test runner output into structured failure reports.

The ``test_run`` tool runs a project's suite in the sandbox and turns the raw
output into a :class:`TestReport`: counters plus one :class:`TestFailure` per
failing test (id, message, optional file/line). Parsers cover pytest and
jest-style runners and degrade gracefully: anything unparseable is surfaced
as raw output rather than silently swallowed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_PYTEST_COUNTS = re.compile(r"(\d+)\s+(passed|failed|skipped|error|errors)")
_PYTEST_FAILED_LINE = re.compile(r"^FAILED\s+(\S+?)\s*-\s*(.*)$", re.MULTILINE)
_PYTEST_VERBOSE_FAILED = re.compile(r"^(\S+?)\s+FAILED\s*$", re.MULTILINE)
_PYTEST_BLOCK_HEADER = re.compile(r"^_{3,}\s*(?:\s*\[[^\]]+\]\s*)?(.+?)\s*_{3,}$", re.MULTILINE)
_JEST_COUNTS = re.compile(
    r"Tests:\s+(\d+)\s+passed(?:,\s+(\d+)\s+failed)?(?:,\s+(\d+)\s+skipped)?,\s+\d+\s+total"
)
_JEST_FAIL_HEADER = re.compile(r"^FAIL\s+(.+)$", re.MULTILINE)
_JEST_SYMBOL = re.compile(r"^[\u00d7\u2715]\s+(.+)$", re.MULTILINE)

_MAX_FAILURES = 25
_MAX_OUTPUT = 50_000


@dataclass(frozen=True)
class TestFailure:
    """One failing test and the reason it failed.

    Attributes:
        test_id: Pytest node id or jest test title.
        message: First meaningful error line for the failure.
        file: Source file the failure maps to, when the runner reports it.
        line: Source line the failure maps to, when reported.
    """

    test_id: str
    message: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class TestReport:
    """Structured outcome of a test suite run.

    Attributes:
        framework: Runner family (``pytest``, ``jest``, or ``generic``).
        command: The exact command that was run.
        passed: Number of tests that passed.
        failed: Number of tests that failed.
        skipped: Number of tests that were skipped.
        errors: Number of runner-level errors (collection failures, crashes).
        failures: Details for each failing test.
        output: Raw runner output (truncated).
        timed_out: Whether the command hit its wall-clock timeout.
        exit_code: Process exit code, when captured.
    """

    framework: str
    command: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    failures: list[TestFailure] = field(default_factory=list)
    output: str = ""
    timed_out: bool = False
    exit_code: int | None = None

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped + self.errors

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestReport:
        payload = dict(data)
        payload["failures"] = [TestFailure(**f) for f in payload.get("failures") or []]
        return cls(**payload)


def parse_test_output(
    framework: str,
    command: str,
    output: str,
    *,
    exit_code: int | None = None,
    timed_out: bool = False,
) -> TestReport:
    """Parse raw test runner output into a :class:`TestReport`.

    Unknown frameworks fall back to a generic scan for failure markers so the
    tool still reports something useful instead of failing hard.
    """
    truncated = output[:_MAX_OUTPUT]
    if framework == "pytest":
        return _parse_pytest(command, truncated, exit_code, timed_out)
    if framework == "jest":
        return _parse_jest(command, truncated, exit_code, timed_out)
    return _parse_generic(command, truncated, exit_code, timed_out)


def _parse_pytest(
    command: str,
    output: str,
    exit_code: int | None,
    timed_out: bool,
) -> TestReport:
    counters = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for match in _PYTEST_COUNTS.finditer(output):
        name = match.group(2)
        key = {"error": "errors"}.get(name, name)
        if key in counters:
            counters[key] = max(counters[key], int(match.group(1)))

    failures: list[TestFailure] = []
    seen: set[str] = set()

    for match in _PYTEST_FAILED_LINE.finditer(output):
        test_id = match.group(1).strip()
        message = match.group(2).strip()
        _record_failure(failures, seen, test_id, message)

    for match in _PYTEST_VERBOSE_FAILED.finditer(output):
        _record_failure(
            failures,
            seen,
            match.group(1).strip(),
            _failure_message_after(output, match.end()),
        )

    for match in _PYTEST_BLOCK_HEADER.finditer(output):
        test_id = match.group(1).strip().rstrip("._ ")
        _record_failure(failures, seen, test_id, _failure_message_after(output, match.end()))

    if (
        not counters["passed"]
        and not counters["failed"]
        and not counters["skipped"]
        and not counters["errors"]
        and exit_code not in (0, None)
        and not timed_out
    ):
        return TestReport(
            framework="pytest",
            command=command,
            failed=1,
            errors=0,
            failures=[TestFailure(test_id="(command)", message=output.strip()[:500])],
            output=output,
            timed_out=timed_out,
            exit_code=exit_code,
        )
    return TestReport(
        framework="pytest",
        command=command,
        passed=counters["passed"],
        failed=counters["failed"],
        skipped=counters["skipped"],
        errors=counters["errors"],
        failures=failures[:_MAX_FAILURES],
        output=output,
        timed_out=timed_out,
        exit_code=exit_code,
    )


def _parse_jest(
    command: str,
    output: str,
    exit_code: int | None,
    timed_out: bool,
) -> TestReport:
    passed = failed = skipped = 0
    match = _JEST_COUNTS.search(output)
    if match:
        passed = int(match.group(1))
        failed = int(match.group(2) or 0)
        skipped = int(match.group(3) or 0)

    failures: list[TestFailure] = []
    seen: set[str] = set()
    for match in _JEST_FAIL_HEADER.finditer(output):
        path = match.group(1).strip()
        _record_failure(failures, seen, path, _failure_message_after(output, match.end()))
    for match in _JEST_SYMBOL.finditer(output):
        title = match.group(1).strip()
        _record_failure(failures, seen, title, _failure_message_after(output, match.end()))

    return TestReport(
        framework="jest",
        command=command,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=0,
        failures=failures[:_MAX_FAILURES],
        output=output,
        timed_out=timed_out,
        exit_code=exit_code,
    )


def _parse_generic(
    command: str,
    output: str,
    exit_code: int | None,
    timed_out: bool,
) -> TestReport:
    failures: list[TestFailure] = []
    for line in output.splitlines():
        lowered = line.lower()
        if "fail" in lowered and any(part in lowered for part in ("test", "suite", "failed")):
            _record_failure(failures, set(), line.strip()[:200], "")
    failed = len(failures)
    return TestReport(
        framework="generic",
        command=command,
        failed=failed,
        failures=failures[:_MAX_FAILURES],
        output=output,
        timed_out=timed_out,
        exit_code=exit_code,
    )


def _record_failure(
    failures: list[TestFailure],
    seen: set[str],
    test_id: str,
    message: str,
) -> None:
    if not test_id or test_id in seen:
        return
    seen.add(test_id)
    failures.append(TestFailure(test_id=test_id, message=message.strip()[:1000]))


def _failure_message_after(output: str, position: int) -> str:
    """Collect the first meaningful error line following ``position``."""
    for line in output[position:].splitlines()[:40]:
        stripped = line.strip()
        if stripped.startswith(("E ", "Error", "assert", ">")):
            return stripped[:500]
        if stripped.startswith("Error"):
            return stripped[:500]
    return ""


def format_report(report: TestReport) -> str:
    """Render a test report for the agent transcript."""
    lines = [
        f"Test run: {report.framework} ({report.command})",
        f"  {report.passed} passed, {report.failed} failed, "
        f"{report.skipped} skipped, {report.errors} errors"
        + (f" in {report.exit_code} exit" if report.exit_code is not None else ""),
    ]
    for failure in report.failures:
        location = failure.file or ""
        if failure.line is not None:
            location = f"{location}:{failure.line}"
        suffix = f" ({location})" if location else ""
        lines.append(f"FAIL {failure.test_id}{suffix}")
        if failure.message:
            lines.append(f"     {failure.message}")
    if not report.ok and not report.failures:
        lines.append("(runner produced no parseable failures)")
    if report.ok:
        lines.append("All tests pass.")
    else:
        lines.append("Fix the failing tests; the suite will be re-run.")
    return "\n".join(lines)
