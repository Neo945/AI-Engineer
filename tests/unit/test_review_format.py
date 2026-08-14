"""Unit tests for the structured review findings parser."""

from __future__ import annotations

import json

from app.review import (
    FindingSeverity,
    ReviewFinding,
    ReviewReport,
    extract_verdict,
    format_findings,
    parse_findings,
    parse_report,
    sort_findings,
)


def _finding(**overrides) -> dict:
    base = {
        "severity": "high",
        "file": "app/auth.py",
        "line": 12,
        "problem": "expiry never checked",
        "reason": "expired tokens are accepted",
        "fix": "compare now <= exp",
    }
    base.update(overrides)
    return base


def _fenced(*entries: dict) -> str:
    return f"```json\n{json.dumps(list(entries))}\n```"


def test_extract_verdict_uppercase_token() -> None:
    assert extract_verdict("VERDICT: PASS\nok") == "PASS"
    assert extract_verdict("something\nVERDICT: changes_needed\nmore") == "CHANGES_NEEDED"
    assert extract_verdict("no verdict here") is None


def test_parse_findings_fenced_json_block() -> None:
    text = (
        "VERDICT: CHANGES_NEEDED\n"
        "Problems found.\n"
        f"{_fenced(_finding(), _finding(severity='low', file='app/b.py', line=None))}\n"
    )
    findings = parse_findings(text)
    assert len(findings) == 2
    assert findings[0].severity == FindingSeverity.HIGH
    assert findings[0].file == "app/auth.py"
    assert findings[0].line == 12
    assert findings[0].problem == "expiry never checked"
    assert findings[0].fix == "compare now <= exp"
    assert findings[1].severity == FindingSeverity.LOW
    assert findings[1].line is None


def test_parse_findings_severity_and_line_coercion() -> None:
    text = _fenced(_finding(severity="HIGH", line="7"))
    findings = parse_findings(text)
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.HIGH
    assert findings[0].line == 7


def test_parse_findings_bare_array_without_fence() -> None:
    text = f"VERDICT: PASS\n{json.dumps([_finding()])}\nthanks"
    assert len(parse_findings(text)) == 1


def test_parse_findings_empty_array() -> None:
    assert parse_findings("VERDICT: PASS\n```json\n[]\n```") == []


def test_parse_findings_invalid_json_returns_empty() -> None:
    assert parse_findings("VERDICT: PASS\n```json\n{not json}\n```") == []
    assert parse_findings("no json at all") == []


def test_parse_findings_skips_malformed_entries() -> None:
    entries = [
        _finding(),
        {"severity": "high"},
        "nope",
        {"severity": "high", "file": "x.py", "problem": "ok"},
    ]
    text = f"```json\n{json.dumps(entries)}\n```\n"
    findings = parse_findings(text)
    assert len(findings) == 2
    assert [finding.file for finding in findings] == ["app/auth.py", "x.py"]


def test_parse_findings_unknown_severity_skipped() -> None:
    text = _fenced(_finding(severity="urgent"))
    assert parse_findings(text) == []


def test_parse_report_combines_verdict_and_findings() -> None:
    text = f"VERDICT: CHANGES_NEEDED\n{_fenced(_finding())}"
    report = parse_report(text)
    assert report == ReviewReport(
        verdict="CHANGES_NEEDED",
        findings=[ReviewFinding.model_validate(_finding())],
    )


def test_parse_report_no_verdict() -> None:
    report = parse_report("just prose")
    assert report.verdict is None
    assert report.findings == []


def test_sort_findings_orders_by_severity_then_file() -> None:
    findings = [
        ReviewFinding.model_validate(_finding(severity="low", file="a.py")),
        ReviewFinding.model_validate(_finding(severity="critical", file="z.py")),
        ReviewFinding.model_validate(_finding(severity="high", file="b.py")),
    ]
    ordered = sort_findings(findings)
    assert [finding.severity.value for finding in ordered] == [
        "critical",
        "high",
        "low",
    ]


def test_format_findings_plain_text() -> None:
    findings = [
        ReviewFinding.model_validate(
            _finding(
                severity="high",
                file="app/auth.py",
                line=12,
                problem="expiry never checked",
                reason="expired tokens are accepted",
                fix="compare now <= exp",
            )
        )
    ]
    rendered = format_findings(findings)
    assert "HIGH app/auth.py:12 — expiry never checked" in rendered
    assert "why: expired tokens are accepted" in rendered
    assert "fix: compare now <= exp" in rendered


def test_format_findings_empty() -> None:
    assert format_findings([]) == "No findings."
