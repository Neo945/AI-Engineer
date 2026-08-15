"""Distributed-systems analysis: LLM interpretation of the concern scan.

``engineer analyze`` first runs the deterministic concern scanner
(:mod:`app.analysis.scan`) and then asks the LLM, acting as a staff
distributed-systems engineer, to interpret the evidence into an
:class:`AnalysisReport`: a summary, structured findings (reusing the
:class:`~app.review.findings.ReviewFinding` schema so severity, file, line,
problem, reason, and fix are uniform across modes), and recommendations.
Parsing degrades gracefully, mirroring the review, architecture, and design
modes: a prose reply still yields a summary, and malformed findings are
skipped rather than failing the whole analysis.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from app.review.findings import ReviewFinding, format_findings

__all__ = [
    "ANALYSIS_PROMPT",
    "AnalysisReport",
    "build_analysis_seed",
    "parse_analysis_report",
    "render_analysis",
]

ANALYSIS_PROMPT = (
    "You are a staff distributed-systems engineer reviewing the scan evidence "
    "the user provides. Interpret the evidence in the context of a production "
    "system: identify where reliability, latency, and consistency are at risk, "
    "and be concrete — cite real files and lines from the evidence. Consider "
    "timeout policy, retry/backoff, idempotency, race conditions, locking, "
    "caching, circuit breakers, blocking vs async calls, and message/event "
    "flows. Do not invent problems the evidence does not support, and say so "
    "when a concern is benign or expected in this codebase. "
    "End your reply with a single JSON object inside a fenced code block, like:\n"
    "```json\n"
    '{"summary": "2-4 sentence assessment of distributed-systems posture.", '
    '"findings": [{"severity": "high", "file": "app/foo.py", '
    '"line": 42, "problem": "retry without backoff on a non-idempotent '
    'request", "reason": "double-submits on transient failures", '
    '"fix": "add exponential backoff and an idempotency key"}], '
    '"recommendations": ["add a global timeout policy", "introduce a circuit '
    'breaker on the downstream client"]}\n'
    "```\n"
    "findings is an array of objects with severity (critical|high|medium|low|"
    "nit), file, optional line, problem, optional reason, and optional fix. "
    "recommendations is an array of strings. Empty arrays may be omitted. "
    "Reply with only the JSON object."
)


class AnalysisReport(BaseModel):
    """Parsed outcome of a distributed-systems analysis reply.

    Attributes:
        summary: Assessment of the workspace's distributed-systems posture.
        findings: Structured findings, empty when none were parsed.
        recommendations: Actionable recommendations, empty when none were parsed.
    """

    summary: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


def build_analysis_seed(scan_summary: str) -> str:
    """Return the user message content for a distributed-systems analysis.

    Args:
        scan_summary: The deterministic concern scan summary to interpret.
    """
    return (
        "Analyze the distributed-systems posture of the workspace from the "
        "deterministic scan evidence below, then produce the JSON report "
        "described in your instructions. Treat the evidence as ground truth; "
        "do not guess at lines you were not shown.\n\n"
        f"{scan_summary}"
    )


def parse_analysis_report(text: str) -> AnalysisReport:
    """Parse an analysis reply into an :class:`AnalysisReport`.

    Prefers a fenced or bare JSON object matching the analysis contract; falls
    back to a prose report (first line as the summary, plus any findings
    recovered from a JSON array).
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            report = _report_from_payload(payload)
        except Exception:
            report = None
        if report is not None and (report.summary or report.findings or report.recommendations):
            return report
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return AnalysisReport(
        summary=lines[0] if lines else "No summary provided.",
        findings=_parse_findings_array(text),
    )


def render_analysis(report: AnalysisReport) -> str:
    """Render an analysis report as a Markdown body."""
    sections = [report.summary.strip()] if report.summary.strip() else []
    if report.findings:
        sections.append("## Findings\n" + format_findings(report.findings))
    recommendations = [item.strip() for item in report.recommendations if item.strip()]
    if recommendations:
        sections.append("## Recommendations\n" + "\n".join(f"- {item}" for item in recommendations))
    return "\n\n".join(sections)


def _report_from_payload(payload: dict[str, Any]) -> AnalysisReport:
    """Build a report from a parsed JSON object, skipping malformed entries."""
    findings: list[ReviewFinding] = []
    for item in payload.get("findings") or []:
        if isinstance(item, dict):
            try:
                findings.append(ReviewFinding.model_validate(item))
            except Exception:
                continue
    recommendations = [
        str(item).strip()
        for item in (payload.get("recommendations") or [])
        if isinstance(item, str) and item.strip()
    ]
    return AnalysisReport(
        summary=str(payload.get("summary") or "").strip(),
        findings=findings,
        recommendations=recommendations,
    )


def _parse_findings_array(text: str) -> list[ReviewFinding]:
    """Recover findings from a bare JSON array embedded in prose."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    findings: list[ReviewFinding] = []
    for item in payload:
        if isinstance(item, dict):
            try:
                findings.append(ReviewFinding.model_validate(item))
            except Exception:
                continue
    return findings


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return a JSON object embedded in ``text``, or ``None``."""
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
