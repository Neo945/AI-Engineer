"""Structured code review findings.

The reviewer is prompted to emit its findings as a JSON array in a fenced
code block so they can be parsed deterministically into
:class:`ReviewFinding` rows (severity, file, line, problem, reason, fix).
Parsing degrades gracefully: anything unparseable yields no findings, and the
verdict line is still recovered, so a malformed reply never crashes the CLI.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, field_validator

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_SEVERITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "nit": 4,
}


class FindingSeverity(StrEnum):
    """Severity of a single review finding, most to least urgent."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NIT = "nit"


class ReviewFinding(BaseModel):
    """One actionable finding from a code review.

    Attributes:
        severity: Urgency of the finding.
        file: Source file the finding maps to.
        line: Source line, when known.
        problem: What is wrong.
        reason: Why it matters, when known.
        fix: Suggested fix, when known.
    """

    severity: FindingSeverity
    file: str
    line: int | None = None
    problem: str
    reason: str | None = None
    fix: str | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value


class ReviewReport(BaseModel):
    """Parsed outcome of a review reply.

    Attributes:
        verdict: ``PASS`` or ``CHANGES_NEEDED`` recovered from the reply.
        findings: Structured findings, empty when none were emitted or
            nothing parseable was found.
    """

    verdict: str | None
    findings: list[ReviewFinding] = []


def extract_verdict(text: str) -> str | None:
    """Return the token from the first ``VERDICT:`` line, if present."""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            return stripped.split(":", 1)[1].strip().upper()
    return None


def parse_findings(text: str) -> list[ReviewFinding]:
    """Parse a JSON findings array out of ``text``.

    A fenced `` ```json ... ``` `` block is preferred; failing that, the
    first ``[`` ... ``]`` span is tried. Invalid or malformed entries are
    skipped rather than failing the whole parse. Returns ``[]`` when nothing
    parseable is found.
    """
    raw = _extract_json(text)
    if raw is None:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    findings: list[ReviewFinding] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(ReviewFinding.model_validate(item))
        except Exception:  # malformed entries are skipped
            continue
    return findings


def parse_report(text: str) -> ReviewReport:
    """Parse a review reply into a verdict plus its findings."""
    return ReviewReport(verdict=extract_verdict(text), findings=parse_findings(text))


def sort_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Return ``findings`` ordered by severity (most urgent first), then file."""
    return sorted(
        findings,
        key=lambda finding: (_SEVERITY_ORDER.get(finding.severity.value, 9), finding.file),
    )


def format_findings(findings: list[ReviewFinding]) -> str:
    """Render findings as plain text (for transcripts or non-rich output)."""
    if not findings:
        return "No findings."
    lines: list[str] = []
    for finding in sort_findings(findings):
        location = finding.file
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        lines.append(f"{finding.severity.value.upper()} {location} — {finding.problem}")
        if finding.reason:
            lines.append(f"  why: {finding.reason}")
        if finding.fix:
            lines.append(f"  fix: {finding.fix}")
    return "\n".join(lines)


def _extract_json(text: str) -> str | None:
    """Return the JSON payload from ``text``, or ``None`` when not found."""
    fence = _JSON_FENCE.search(text)
    if fence:
        return fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]
