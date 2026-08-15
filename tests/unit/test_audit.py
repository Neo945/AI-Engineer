"""Unit tests for the production-readiness audit report module."""

from __future__ import annotations

from app.audit import (
    AUDIT_PROMPT,
    AuditReport,
    parse_audit_report,
    render_audit,
    resolve_verdict,
)

_JSON_REPORT = """\
Here is my audit.

```json
{"summary": "Solid overall, but the auth path needs a fix.",
 "verdict": "CHANGES_NEEDED",
 "scores": [
   {"dimension": "security", "score": 55, "rationale": "expiry never checked",
    "evidence": ["auth.py:12", "tokens accepted past exp"]},
   {"dimension": "maintainability", "score": 88, "rationale": "clear structure",
    "evidence": ["services/"]}
 ],
 "findings": [
   {"severity": "high", "file": "app/auth.py", "line": 12,
    "problem": "expiry never checked", "reason": "expired tokens accepted",
    "fix": "compare now <= exp"}
 ]}
```
"""


def test_parse_audit_report_from_fenced_json() -> None:
    report = parse_audit_report(_JSON_REPORT)
    assert report.summary == "Solid overall, but the auth path needs a fix."
    assert report.verdict == "CHANGES_NEEDED"
    assert len(report.scores) == 2
    assert report.scores[0].dimension == "security"
    assert report.scores[0].score == 55
    assert report.scores[0].evidence == ["auth.py:12", "tokens accepted past exp"]
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.severity.value == "high"
    assert finding.file == "app/auth.py"
    assert finding.line == 12


def test_parse_audit_report_from_bare_json() -> None:
    text = (
        '{"summary": "ok", "verdict": "PASS", '
        '"scores": [{"dimension": "security", "score": 91, "rationale": "fine"}]}'
    )
    report = parse_audit_report(text)
    assert report.summary == "ok"
    assert report.verdict == "PASS"
    assert report.scores[0].score == 91


def test_parse_audit_report_skips_malformed_entries() -> None:
    text = (
        '{"summary": "mixed", "verdict": "PASS", '
        '"scores": [{"dimension": "security", "score": 80, "rationale": "ok"}, '
        '{"dimension": "broken"}, 42], '
        '"findings": [{"severity": "low", "file": "a.py", "problem": "nit"}, '
        '{"file": "missing-field"}]}'
    )
    report = parse_audit_report(text)
    assert report.summary == "mixed"
    assert len(report.scores) == 1
    assert report.scores[0].dimension == "security"
    assert len(report.findings) == 1
    assert report.findings[0].file == "a.py"


def test_parse_audit_report_falls_back_to_prose() -> None:
    text = (
        "The change is broadly fine but the token check is weak.\n"
        "VERDICT: CHANGES_NEEDED\n\n"
        "```json\n"
        '[{"severity": "high", "file": "app/auth.py", "line": 12, '
        '"problem": "expiry never checked", "reason": "expired tokens", '
        '"fix": "compare now <= exp"}]\n'
        "```"
    )
    report = parse_audit_report(text)
    assert report.summary == "The change is broadly fine but the token check is weak."
    assert report.verdict == "CHANGES_NEEDED"
    assert report.scores == []
    assert len(report.findings) == 1


def test_verdict_is_normalized() -> None:
    report = parse_audit_report('{"summary": "s", "verdict": "  pass  ", "scores": []}')
    assert report.verdict == "PASS"
    report = parse_audit_report('{"summary": "s", "verdict": "maybe", "scores": []}')
    assert report.verdict is None


def test_resolve_verdict_uses_explicit_or_derived() -> None:
    assert resolve_verdict(AuditReport(summary="s", verdict="PASS")) == "PASS"
    assert resolve_verdict(AuditReport(summary="s", verdict="CHANGES_NEEDED")) == "CHANGES_NEEDED"

    passing = AuditReport(
        summary="s",
        scores=[
            {"dimension": "security", "score": 80, "rationale": "ok"},
            {"dimension": "tests", "score": 75, "rationale": "ok"},
        ],
    )
    assert resolve_verdict(passing) == "PASS"

    failing = AuditReport(
        summary="s",
        scores=[{"dimension": "security", "score": 40, "rationale": "bad"}],
    )
    assert resolve_verdict(failing) == "CHANGES_NEEDED"

    assert resolve_verdict(AuditReport(summary="s")) == "CHANGES_NEEDED"


def test_render_audit_includes_scores_and_findings() -> None:
    report = parse_audit_report(_JSON_REPORT)
    body = render_audit(report)
    assert "Solid overall" in body
    assert "## Scores" in body
    assert "**security**: 55/100" in body
    assert "auth.py:12" in body
    assert "expiry never checked" in body


def test_audit_prompt_mentions_the_json_contract() -> None:
    assert '"verdict"' in AUDIT_PROMPT
    assert '"scores"' in AUDIT_PROMPT
    assert "0-100" in AUDIT_PROMPT
    assert "CHANGES_NEEDED" in AUDIT_PROMPT
