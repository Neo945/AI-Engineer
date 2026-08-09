"""Unit tests for the test output parser and test_report rendering."""

from __future__ import annotations

from app.executor.test_parser import (
    TestFailure,
    TestReport,
    format_report,
    parse_test_output,
)


def test_parse_pytest_counts_and_failures() -> None:
    output = """....F.....
=========================== FAILURES ===========================
___________________________ test_addition ______________________
tests/test_calc.py:6: in test_addition
    assert add(1, 2) == 4
E   assert 3 == 4
========================= short test summary info =========================
FAILED tests/test_calc.py::test_addition - assert 3 == 4
1 failed, 9 passed in 0.12s
"""
    report = parse_test_output("pytest", "python -m pytest -q", output, exit_code=1)
    assert report.failed == 1
    assert report.passed == 9
    assert not report.ok
    assert report.failures[0].test_id == "tests/test_calc.py::test_addition"
    assert "assert 3 == 4" in report.failures[0].message


def test_parse_pytest_verbose_markers() -> None:
    output = """tests/unit/test_a.py::test_one PASSED
tests/unit/test_b.py::test_two FAILED
2 passed, 1 failed in 0.05s
"""
    report = parse_test_output("pytest", "pytest", output, exit_code=1)
    assert report.passed == 2
    assert report.failed == 1
    assert any("test_b.py::test_two" in f.test_id for f in report.failures)


def test_parse_pytest_passing_run() -> None:
    report = parse_test_output(
        "pytest", "python -m pytest -q", ".... 4 passed in 0.1s", exit_code=0
    )
    assert report.ok
    assert report.passed == 4
    assert report.failed == 0


def test_parse_pytest_skipped_and_errors() -> None:
    output = """3 passed, 2 skipped, 1 error in 0.2s
"""
    report = parse_test_output("pytest", "pytest", output, exit_code=1)
    assert report.passed == 3
    assert report.skipped == 2
    assert report.errors == 1
    assert not report.ok


def test_parse_pytest_unparseable_command_failure() -> None:
    output = "/bin/sh: pytest: not found"
    report = parse_test_output("pytest", "pytest", output, exit_code=127)
    assert not report.ok
    assert report.failed == 1
    assert report.failures[0].test_id == "(command)"


def test_parse_jest_counts() -> None:
    output = """PASS tests/sum.test.js
FAIL tests/sub.test.js
  ● subtracts numbers
    expect(received).toBe(expected)
Tests: 9 passed, 1 failed, 10 total
"""
    report = parse_test_output("jest", "npm test", output, exit_code=1)
    assert report.passed == 9
    assert report.failed == 1
    assert not report.ok
    assert any("sub.test.js" in f.test_id for f in report.failures)


def test_parse_jest_all_pass() -> None:
    output = "Tests: 5 passed, 5 total"
    report = parse_test_output("jest", "npm test", output, exit_code=0)
    assert report.ok
    assert report.passed == 5


def test_parse_generic_failure_lines() -> None:
    output = "some test failed\ntest_suite failed: boom\n"
    report = parse_test_output("generic", "run.sh", output, exit_code=1)
    assert not report.ok
    assert report.failed >= 1


def test_report_roundtrip_dict() -> None:
    report = TestReport(
        framework="pytest",
        command="pytest -q",
        passed=1,
        failed=2,
        skipped=3,
        errors=0,
        failures=[
            TestFailure(test_id="a::b", message="boom", file="a.py", line=4),
        ],
    )
    restored = TestReport.from_dict(report.to_dict())
    assert restored == report


def test_format_report_lists_failures() -> None:
    report = TestReport(
        framework="pytest",
        command="python -m pytest -q",
        passed=1,
        failed=1,
        failures=[TestFailure(test_id="a.py::test_x", message="assert 1 == 2")],
    )
    text = format_report(report)
    assert "Test run: pytest" in text
    assert "FAIL a.py::test_x" in text
    assert "assert 1 == 2" in text
    assert "Fix the failing tests" in text


def test_format_report_passing() -> None:
    report = TestReport(framework="pytest", command="pytest -q", passed=5)
    assert "All tests pass." in format_report(report)
