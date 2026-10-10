"""Legacy-modernization mode: LLM-generated migration plans.

``engineer modernize`` seeds the LLM with the workspace's deterministic
dependency-graph summary and asks it to act as a staff engineer planning a
modernization of the legacy codebase: it emits a single JSON object covering
the current/target state, an ordered migration plan, a risk assessment, a
rollback strategy, and validation/follow-up steps. Parsing degrades
gracefully, mirroring the design/audit/architecture modes: a prose reply still
yields a summary, and per-entry validation skips malformed steps/risks instead
of failing the whole plan.

The command is plan-only: it produces the migration artifact and never
executes changes, so a human can review and approve before any work begins.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.review.findings import ReviewFinding, sort_findings

__all__ = [
    "MODERNIZATION_PROMPT",
    "MigrationStep",
    "ModernizationReport",
    "build_modernization_seed",
    "parse_modernization_report",
    "render_modernization",
]

MODERNIZATION_PROMPT = (
    "You are a staff software engineer planning the modernization of a legacy "
    "system. The user provides a deterministic dependency-graph summary of the "
    "codebase and an optional target stack. Produce a concrete, staged plan to "
    "move the code toward the target without a big-bang rewrite: name the real "
    "files or modules to change, sequence the work so the system stays "
    "deployable at every step, and be explicit about what could break. "
    "End your reply with a single JSON object inside a fenced code block, like:\n"
    "```json\n"
    '{"summary": "2-4 sentence overview of the modernization and its strategy.", '
    '"current_state": "prose describing the legacy system as it is today", '
    '"target_state": "prose describing the modernized target", '
    '"assumptions": ["constraint or unknown the plan rests on"], '
    '"migration_steps": [{"title": "strangler-facade the HTTP layer", '
    '"description": "what changes and which modules are touched", '
    '"risk": "what could break and why", '
    '"effort": "rough size, e.g. 2-3 days", '
    '"validation": "how to confirm this step is safe"}], '
    '"risks": [{"severity": "high", "file": "app/retrieval/indexer.py", '
    '"problem": "tight coupling to the ORM session", '
    '"reason": "blocks introducing a repository port", '
    '"fix": "extract a storage port behind an interface first"}], '
    '"rollback_strategy": ["feature-flag the new path and keep the old one live"], '
    '"validation": ["run the full test suite against the modernized path"], '
    '"follow_ups": ["adopt structured concurrency in the worker pool"]}\n'
    "```\n"
    "Every field is a JSON string, except assumptions, migration_steps, risks, "
    "rollback_strategy, validation, and follow_ups which are arrays. Severity "
    "is one of critical, high, medium, low, or nit. Order migration_steps so "
    "the system is shippable after each one. Empty arrays may be omitted. "
    "Reply with only the JSON object."
)


class MigrationStep(BaseModel):
    """One ordered step of a modernization plan.

    Attributes:
        title: Short imperative title for the step.
        description: What changes and which modules are touched.
        risk: What could break and why.
        effort: Rough size estimate (e.g. ``2-3 days``).
        validation: How to confirm the step is safe.
    """

    title: str = Field(min_length=1)
    description: str = ""
    risk: str = ""
    effort: str = ""
    validation: str = ""


class ModernizationReport(BaseModel):
    """Parsed outcome of a legacy-modernization reply.

    Attributes:
        summary: Overview of the modernization and its strategy.
        current_state: Prose describing the legacy system as-is.
        target_state: Prose describing the modernized target.
        assumptions: Explicit assumptions the plan rests on.
        migration_steps: Ordered steps, empty when none were parsed.
        risks: Risk findings, empty when none were parsed.
        rollback_strategy: How to back out of the change safely.
        validation: How to verify the modernization succeeded.
        follow_ups: Improvements to schedule after the migration.
    """

    summary: str = ""
    current_state: str = ""
    target_state: str = ""
    assumptions: list[str] = Field(default_factory=list)
    migration_steps: list[MigrationStep] = Field(default_factory=list)
    risks: list[ReviewFinding] = Field(default_factory=list)
    rollback_strategy: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)


def build_modernization_seed(graph_summary: str, *, target: str | None = None) -> str:
    """Return the user message content for a modernization analysis.

    Args:
        graph_summary: Deterministic dependency-graph summary for the repo.
        target: Optional description of the stack to modernize toward.
    """
    target_line = f"\nTarget stack: {target.strip()}" if target and target.strip() else ""
    return (
        "Plan the modernization of the codebase described by this dependency "
        "graph. Sequence the work into small, independently shippable steps and "
        "call out the concrete risks at each one.\n\n"
        f"Dependency graph:\n{graph_summary.strip()}{target_line}"
    )


def parse_modernization_report(text: str) -> ModernizationReport:
    """Parse a modernization reply into a :class:`ModernizationReport`.

    Prefers a fenced or bare JSON object matching the plan contract; falls back
    to a prose report (first non-empty line as the summary).
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            report = _report_from_payload(payload)
        except Exception:
            report = None
        if report is not None and (report.summary or report.migration_steps or report.target_state):
            return report
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return ModernizationReport(summary=lines[0] if lines else "No summary provided.")


def render_modernization(report: ModernizationReport) -> str:
    """Render a modernization report as a Markdown body."""
    sections = [report.summary.strip()] if report.summary.strip() else []

    if report.current_state.strip():
        sections.append("## Current state\n" + report.current_state.strip())
    if report.target_state.strip():
        sections.append("## Target state\n" + report.target_state.strip())

    if report.migration_steps:
        lines = ["## Migration plan"]
        for index, step in enumerate(report.migration_steps, start=1):
            lines.append(f"{index}. **{step.title}**")
            if step.description.strip():
                lines.append(f"   - {step.description.strip()}")
            if step.effort.strip():
                lines.append(f"   - effort: {step.effort.strip()}")
            if step.risk.strip():
                lines.append(f"   - risk: {step.risk.strip()}")
            if step.validation.strip():
                lines.append(f"   - validation: {step.validation.strip()}")
        sections.append("\n".join(lines))

    if report.risks:
        lines = ["## Risk assessment"]
        for risk in sort_findings(report.risks):
            location = f" ({risk.file})" if risk.file.strip() else ""
            lines.append(f"- **{risk.severity.value.upper()}**{location} {risk.problem.strip()}")
            if risk.reason and risk.reason.strip():
                lines.append(f"  - why: {risk.reason.strip()}")
            if risk.fix and risk.fix.strip():
                lines.append(f"  - mitigation: {risk.fix.strip()}")
        sections.append("\n".join(lines))

    _bullet_sections = (
        ("## Rollback strategy", report.rollback_strategy),
        ("## Validation", report.validation),
        ("## Assumptions", report.assumptions),
        ("## Follow-ups", report.follow_ups),
    )
    for heading, items in _bullet_sections:
        cleaned = [item.strip() for item in items if item.strip()]
        if cleaned:
            sections.append(heading + "\n" + "\n".join(f"- {item}" for item in cleaned))

    return "\n\n".join(sections)


def _report_from_payload(payload: dict[str, Any]) -> ModernizationReport:
    """Build a report from a parsed JSON object, skipping malformed entries."""
    steps: list[MigrationStep] = []
    for item in payload.get("migration_steps") or []:
        if isinstance(item, dict):
            try:
                steps.append(MigrationStep.model_validate(item))
            except Exception:
                continue
    risks: list[ReviewFinding] = []
    for item in payload.get("risks") or []:
        if isinstance(item, dict):
            try:
                risks.append(ReviewFinding.model_validate(item))
            except Exception:
                continue

    def _strings(key: str) -> list[str]:
        return [
            str(item).strip()
            for item in (payload.get(key) or [])
            if isinstance(item, str) and item.strip()
        ]

    return ModernizationReport(
        summary=str(payload.get("summary") or "").strip(),
        current_state=str(payload.get("current_state") or "").strip(),
        target_state=str(payload.get("target_state") or "").strip(),
        assumptions=_strings("assumptions"),
        migration_steps=steps,
        risks=risks,
        rollback_strategy=_strings("rollback_strategy"),
        validation=_strings("validation"),
        follow_ups=_strings("follow_ups"),
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
