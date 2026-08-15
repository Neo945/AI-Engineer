"""LLM architecture analysis seeded from the deterministic dependency graph.

``engineer arch`` asks the LLM to act as a staff software architect over the
workspace's file-level dependency graph (see :mod:`app.architecture.deps`),
and to emit a single JSON object: an overall summary, the components it
identified (name, files, responsibility), the layering, the files that matter
most, and recommendations. A Mermaid diagram can optionally be requested for
the system-design rendering. Parsing degrades gracefully, mirroring
:mod:`app.audit`: a prose reply still yields a summary, and per-entry
validation skips malformed components/key files instead of failing the whole
report.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "ARCHITECTURE_PROMPT",
    "ArchitectureComponent",
    "ArchitectureReport",
    "KeyFile",
    "build_architecture_seed",
    "parse_architecture_report",
    "render_architecture",
]

ARCHITECTURE_PROMPT = (
    "You are a staff software architect analyzing a repository from its "
    "file-level dependency graph. The graph was extracted deterministically "
    "from the source files: nodes are files, edges are internal imports, and "
    "unresolved imports are external dependencies, not edges. Base your "
    "analysis strictly on the graph facts provided, not on assumptions about "
    "the codebase. "
    "End your reply with a single JSON object inside a fenced code block, like:\n"
    "```json\n"
    '{"summary": "2-4 sentence overall assessment of the architecture.", '
    '"components": [{"name": "auth service", '
    '"files": ["app/auth.py", "app/tokens.py"], '
    '"responsibility": "what this component owns"}], '
    '"layers": ["gateway", "application", "domain", "infrastructure"], '
    '"key_files": [{"path": "app/core/config.py", '
    '"role": "why this file matters"}], '
    '"recommendations": ["first concrete, actionable recommendation", '
    '"second recommendation"], '
    '"mermaid": "flowchart TD\\n  A[gateway] --> B[service]\\n"}\n'
    "```\n"
    "components should group the graph's most depended-on files and their "
    "clusters; layers are ordered top-down (most dependent to most depended "
    "on); key_files are the hubs and load-bearing files; recommendations are "
    "specific to the observed structure (cycles, orphans, hub hotspots). "
    "mermaid is optional and should be a valid Mermaid flowchart using the "
    "file or component names. Empty lists may be omitted. Reply with only the "
    "JSON object."
)


class ArchitectureComponent(BaseModel):
    """One component identified in the repository.

    Attributes:
        name: Short component name (e.g. ``auth service``).
        files: Files the component spans.
        responsibility: What the component owns, in one or two clauses.
    """

    name: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    responsibility: str = ""


class KeyFile(BaseModel):
    """A load-bearing file worth knowing about.

    Attributes:
        path: Root-relative path of the file.
        role: Why it matters (hub, bottleneck, cycle participant, ...).
    """

    path: str = Field(min_length=1)
    role: str = ""


class ArchitectureReport(BaseModel):
    """Parsed outcome of an architecture analysis reply.

    Attributes:
        summary: Overall assessment, as plain prose.
        components: Identified components, empty when none were parsed.
        layers: Ordered layering of the system, top-down.
        key_files: Files that matter most, empty when none were parsed.
        recommendations: Actionable, graph-grounded recommendations.
        mermaid: Optional Mermaid flowchart source, empty when absent.
    """

    summary: str = ""
    components: list[ArchitectureComponent] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    key_files: list[KeyFile] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    mermaid: str = ""


def build_architecture_seed(graph_summary_text: str) -> str:
    """Return the user message content for an architecture analysis.

    Args:
        graph_summary_text: The compact graph summary from
            :func:`app.architecture.deps.graph_summary`.
    """
    return (
        "Analyze the architecture of this repository from its dependency "
        "graph, then produce the JSON report described in your instructions.\n\n"
        "Dependency graph summary:\n"
        f"{graph_summary_text}"
    )


def parse_architecture_report(text: str) -> ArchitectureReport:
    """Parse an architecture reply into an :class:`ArchitectureReport`.

    Prefers a fenced or bare JSON object matching the architecture contract;
    falls back to a prose report (first line as the summary, plus any Mermaid
    fenced block found).
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            report = _report_from_payload(payload)
        except Exception:
            report = None
        if report is not None and (report.summary or report.components or report.recommendations):
            return report
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return ArchitectureReport(
        summary=lines[0] if lines else "No summary provided.",
        mermaid=_extract_mermaid_block(text),
    )


def render_architecture(report: ArchitectureReport) -> str:
    """Render an architecture report as a Markdown body."""
    sections = [report.summary.strip()] if report.summary.strip() else []
    if report.components:
        lines = ["## Components"]
        for component in report.components:
            files = ", ".join(component.files)
            lines.append(f"- **{component.name}** — {component.responsibility.strip()}")
            if files:
                lines.append(f"  - files: {files}")
        sections.append("\n".join(lines))
    if report.layers:
        sections.append("## Layers\n" + "\n".join(f"- {layer}" for layer in report.layers))
    if report.key_files:
        lines = ["## Key files"]
        for key_file in report.key_files:
            lines.append(f"- **{key_file.path}** — {key_file.role.strip()}")
        sections.append("\n".join(lines))
    if report.recommendations:
        lines = ["## Recommendations"]
        lines += [
            f"{index}. {item.strip()}" for index, item in enumerate(report.recommendations, 1)
        ]
        sections.append("\n".join(lines))
    if report.mermaid.strip():
        sections.append("## Diagram\n```mermaid\n" + report.mermaid.strip() + "\n```")
    return "\n\n".join(sections)


def _report_from_payload(payload: dict[str, Any]) -> ArchitectureReport:
    """Build a report from a parsed JSON object, skipping malformed entries."""
    components: list[ArchitectureComponent] = []
    for item in payload.get("components") or []:
        if isinstance(item, dict):
            try:
                components.append(ArchitectureComponent.model_validate(item))
            except Exception:
                continue
    key_files: list[KeyFile] = []
    for item in payload.get("key_files") or []:
        if isinstance(item, dict):
            try:
                key_files.append(KeyFile.model_validate(item))
            except Exception:
                continue
    recommendations = [
        str(item).strip()
        for item in (payload.get("recommendations") or [])
        if isinstance(item, str) and item.strip()
    ]
    layers = [
        str(item).strip()
        for item in (payload.get("layers") or [])
        if isinstance(item, str) and item.strip()
    ]
    mermaid = str(payload.get("mermaid") or "").strip()
    if mermaid.startswith("```mermaid"):
        mermaid = _strip_fence(mermaid)
    return ArchitectureReport(
        summary=str(payload.get("summary") or "").strip(),
        components=components,
        layers=layers,
        key_files=key_files,
        recommendations=recommendations,
        mermaid=mermaid,
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


def _extract_mermaid_block(text: str) -> str:
    """Return the first fenced Mermaid diagram in ``text``, if any."""
    import re

    match = re.search(r"```mermaid\s*(.*?)```", text, re.DOTALL)
    return _strip_fence(match.group(1).strip()) if match else ""


def _strip_fence(content: str) -> str:
    content = content.strip()
    return content[7:].strip() if content.startswith("```mermaid") else content
