"""Structured production-readiness audit.

``engineer audit`` asks the LLM to act as a staff engineer auditing the
changes under review, scoring a set of dimensions (0-100) and backing every
claim with cited evidence, then emitting a single JSON object so the result
can be parsed deterministically. Parsing degrades gracefully: a prose reply
still yields a summary and verdict, and per-entry validation skips malformed
scores or findings instead of failing the whole report.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.review import ReviewFinding, extract_verdict, format_findings, parse_findings

__all__ = [
    "AUDIT_PROMPT",
    "AuditReport",
    "AuditScore",
    "parse_audit_report",
    "render_audit",
    "resolve_verdict",
]

PASS_THRESHOLD = 70

AUDIT_PROMPT = (
    "You are a staff engineer performing a production-readiness audit of a "
    "user's repository. Use read-only tools (git status, git diff, git log, "
    "file_read) to inspect the changes under review, not just the request. "
    "Evaluate these dimensions: correctness, security, performance, "
    "maintainability, testability, observability, and production-readiness "
    "(deployment, failure handling, migrations). Every claim must cite "
    "evidence from the code you inspected. "
    "End your reply with a single JSON object inside a fenced code block, like:\n"
    "```json\n"
    '{"summary": "2-4 sentence overall assessment.", '
    '"verdict": "CHANGES_NEEDED", '
    '"scores": [{"dimension": "security", "score": 82, '
    '"rationale": "short reason", "evidence": ["auth.py:12", "token check"]}], '
    '"findings": [{"severity": "high", "file": "app/auth.py", "line": 12, '
    '"problem": "what is wrong", "reason": "why it matters", '
    '"fix": "suggested fix"}]}\n'
    "```\n"
    "scores are integers 0-100 (70+ means the dimension is acceptable); "
    "verdict is 'PASS' when every score is at least 70, else "
    "'CHANGES_NEEDED'. severity is one of critical, high, medium, low, nit; "
    "line may be null; reason and fix are optional. Empty scores or findings "
    "arrays may be omitted. Reply with only the JSON object."
)


class AuditScore(BaseModel):
    """One scored dimension of an audit.

    Attributes:
        dimension: The dimension being scored (e.g. ``security``).
        score: Integer 0-100; 70+ is acceptable.
        rationale: Why this score was given.
        evidence: Cited evidence (files, line numbers, short quotes).
    """

    dimension: str = Field(min_length=1)
    score: int = Field(ge=0, le=100)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _coerce_score(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return round(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value


class AuditReport(BaseModel):
    """Parsed outcome of an audit reply.

    Attributes:
        summary: Overall assessment, as plain prose.
        verdict: ``PASS`` or ``CHANGES_NEEDED``; ``None`` when not stated
            (derive one from the scores via :func:`resolve_verdict`).
        scores: Scored dimensions, empty when none were emitted or parsed.
        findings: Structured findings, empty when none were emitted.
    """

    summary: str = ""
    verdict: str | None = None
    scores: list[AuditScore] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)

    @field_validator("verdict", mode="before")
    @classmethod
    def _coerce_verdict(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if normalized in {"PASS", "CHANGES_NEEDED"}:
                return normalized
        return None


def parse_audit_report(text: str) -> AuditReport:
    """Parse an audit reply into an :class:`AuditReport`.

    Prefers a fenced or bare JSON object matching the audit contract; falls
    back to a prose report (first line as the summary, a ``VERDICT:`` line if
    present, and any fenced findings array).
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            report = _report_from_payload(payload)
        except Exception:
            report = None
        if report is not None and (report.summary or report.scores or report.findings):
            return report
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return AuditReport(
        summary=lines[0] if lines else "No summary provided.",
        verdict=extract_verdict(text),
        findings=parse_findings(text),
    )


def resolve_verdict(report: AuditReport) -> str:
    """Return the audit's verdict, deriving one from the scores when absent."""
    if report.verdict in {"PASS", "CHANGES_NEEDED"}:
        return report.verdict
    if report.scores:
        average = sum(score.score for score in report.scores) / len(report.scores)
        return "PASS" if average >= PASS_THRESHOLD else "CHANGES_NEEDED"
    return "CHANGES_NEEDED"


def render_audit(report: AuditReport) -> str:
    """Render an audit report as a Markdown body."""
    sections = [report.summary.strip()] if report.summary.strip() else []
    if report.scores:
        lines = ["## Scores"]
        for score in report.scores:
            lines.append(f"- **{score.dimension}**: {score.score}/100 — {score.rationale.strip()}")
            for item in score.evidence:
                lines.append(f"  - {item}")
        sections.append("\n".join(lines))
    if report.findings:
        sections.append(format_findings(report.findings))
    return "\n\n".join(sections)


def _report_from_payload(payload: dict[str, Any]) -> AuditReport:
    """Build a report from a parsed JSON object, skipping malformed entries."""
    scores: list[AuditScore] = []
    for item in payload.get("scores") or []:
        if isinstance(item, dict):
            try:
                scores.append(AuditScore.model_validate(item))
            except Exception:
                continue
    findings: list[ReviewFinding] = []
    for item in payload.get("findings") or []:
        if isinstance(item, dict):
            try:
                findings.append(ReviewFinding.model_validate(item))
            except Exception:
                continue
    return AuditReport(
        summary=str(payload.get("summary") or "").strip(),
        verdict=payload.get("verdict"),
        scores=scores,
        findings=findings,
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return a JSON object embedded in ``text``, or ``None``."""
    import json
    import re

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
