"""System-design mode: LLM-generated design documents.

``engineer design "goal"`` asks the LLM to act as a staff software architect
designing a system from scratch for the given goal, and to emit a single JSON
object covering architecture, components, API contracts, data/event model,
caching, failure handling, scaling, observability, and Mermaid diagrams.
Parsing degrades gracefully, mirroring the audit and architecture modes: a
prose reply still yields a summary, Mermaid fenced blocks are recovered, and
per-entry validation skips malformed components/APIs/entities instead of
failing the whole design.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "DESIGN_PROMPT",
    "ApiContract",
    "DataEntity",
    "DesignComponent",
    "DesignReport",
    "build_design_seed",
    "parse_design_report",
    "render_design",
]

DESIGN_PROMPT = (
    "You are a staff software architect designing a system for the goal the "
    "user provides. Produce a concrete, implementable design, not a generic "
    "essay: name real components, real endpoints, real entities and events, "
    "and state your key assumptions explicitly. "
    "End your reply with a single JSON object inside a fenced code block, like:\n"
    "```json\n"
    '{"summary": "2-4 sentence overview of the system and its shape.", '
    '"assumptions": ["load estimate", "tech constraints"], '
    '"architecture": "prose describing the overall topology and how '
    'components communicate", '
    '"components": [{"name": "api gateway", "responsibility": "what it owns", '
    '"details": "routing, rate limits"}], '
    '"api": [{"method": "POST", "path": "/shorten", '
    '"request": "{\\"url\\": \\"...\\"}", "response": "201 {\\"slug\\": \\"...\\"}", '
    '"notes": "idempotent by url"}], '
    '"data_model": [{"entity": "links", "fields": "slug PK, url, created_at", '
    '"notes": "unique on url"}], '
    '"events": ["link.created -> analytics consumer"], '
    '"caching": ["hot slugs in Redis, TTL 24h, invalidated on write"], '
    '"failure_handling": ["idempotent writes", "retry with backoff", '
    '"circuit breaker on downstream"], '
    '"scaling": ["stateless API tier behind a load balancer", '
    '"read replicas for the read path"], '
    '"observability": ["request logs with trace ids", "RED metrics per endpoint"], '
    '"mermaid": ["flowchart TD\\n  A[client] --> B[api]\\n", '
    '"sequenceDiagram\\n  client->>api: POST /shorten\\n"], '
    '"risks": ["hot key skew on popular links"]}\n'
    "```\n"
    "Every field is a JSON string, except assumptions, events, caching, "
    "failure_handling, scaling, observability, and risks which are string "
    "arrays, and mermaid which is an array of Mermaid diagram sources. Include "
    "at least one Mermaid flowchart and one sequence diagram when practical. "
    "Empty arrays may be omitted. Reply with only the JSON object."
)


class DesignComponent(BaseModel):
    """One component of the designed system.

    Attributes:
        name: Component name (e.g. ``api gateway``).
        responsibility: What the component owns.
        details: Concrete detail (libraries, protocol, guarantees).
    """

    name: str = Field(min_length=1)
    responsibility: str = ""
    details: str = ""


class ApiContract(BaseModel):
    """One API endpoint contract.

    Attributes:
        method: HTTP method (e.g. ``POST``).
        path: URL path (e.g. ``/shorten``).
        request: Request shape (payload, params).
        response: Response shape (status codes, payload).
        notes: Semantics (idempotency, auth, rate limits).
    """

    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    request: str = ""
    response: str = ""
    notes: str = ""


class DataEntity(BaseModel):
    """One persisted entity in the data model.

    Attributes:
        entity: Entity name (e.g. ``links``).
        fields: Field list / keys.
        notes: Constraints, indexes, retention.
    """

    entity: str = Field(min_length=1)
    fields: str = ""
    notes: str = ""


class DesignReport(BaseModel):
    """Parsed outcome of a system-design reply.

    Attributes:
        summary: Overview of the system and its shape.
        assumptions: Explicit assumptions the design rests on.
        architecture: Prose describing topology and communication.
        components: Named components, empty when none were parsed.
        api: Endpoint contracts, empty when none were parsed.
        data_model: Persisted entities, empty when none were parsed.
        events: Event/async flows.
        caching: Caching strategy.
        failure_handling: Failure and retry strategy.
        scaling: How the system scales.
        observability: Logs, metrics, tracing.
        mermaid: Mermaid diagram sources.
        risks: Risks and mitigations to note.
    """

    summary: str = ""
    assumptions: list[str] = Field(default_factory=list)
    architecture: str = ""
    components: list[DesignComponent] = Field(default_factory=list)
    api: list[ApiContract] = Field(default_factory=list)
    data_model: list[DataEntity] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    caching: list[str] = Field(default_factory=list)
    failure_handling: list[str] = Field(default_factory=list)
    scaling: list[str] = Field(default_factory=list)
    observability: list[str] = Field(default_factory=list)
    mermaid: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def build_design_seed(goal: str) -> str:
    """Return the user message content for a system-design analysis.

    Args:
        goal: The user's description of the system to design.
    """
    return (
        "Design a system for the following goal, then produce the JSON design "
        "report described in your instructions. Ask yourself what the system "
        "must guarantee and be explicit about trade-offs.\n\n"
        f"Goal: {goal.strip()}"
    )


def parse_design_report(text: str) -> DesignReport:
    """Parse a system-design reply into a :class:`DesignReport`.

    Prefers a fenced or bare JSON object matching the design contract; falls
    back to a prose report (first line as the summary, plus any Mermaid fenced
    blocks found).
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            report = _report_from_payload(payload)
        except Exception:
            report = None
        if report is not None and (report.summary or report.components or report.architecture):
            return report
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return DesignReport(
        summary=lines[0] if lines else "No summary provided.",
        mermaid=_extract_mermaid_blocks(text),
    )


def render_design(report: DesignReport) -> str:
    """Render a system design report as a Markdown body."""
    sections = [report.summary.strip()] if report.summary.strip() else []

    if report.architecture.strip():
        sections.append("## Architecture\n" + report.architecture.strip())

    if report.components:
        lines = ["## Components"]
        for component in report.components:
            lines.append(f"- **{component.name}** — {component.responsibility.strip()}")
            if component.details.strip():
                lines.append(f"  - {component.details.strip()}")
        sections.append("\n".join(lines))

    if report.api:
        lines = ["## API"]
        for endpoint in report.api:
            label = f"{endpoint.method} {endpoint.path}" if endpoint.method else endpoint.path
            lines.append(f"- **{label}**")
            if endpoint.request:
                lines.append(f"  - request: `{endpoint.request}`")
            if endpoint.response:
                lines.append(f"  - response: `{endpoint.response}`")
            if endpoint.notes:
                lines.append(f"  - {endpoint.notes}")
        sections.append("\n".join(lines))

    if report.data_model:
        lines = ["## Data model"]
        for entity in report.data_model:
            lines.append(f"- **{entity.entity}** — {entity.fields.strip()}")
            if entity.notes:
                lines.append(f"  - {entity.notes}")
        sections.append("\n".join(lines))

    _bullet_sections = (
        ("## Events", report.events),
        ("## Caching", report.caching),
        ("## Failure handling", report.failure_handling),
        ("## Scaling", report.scaling),
        ("## Observability", report.observability),
        ("## Assumptions", report.assumptions),
        ("## Risks", report.risks),
    )
    for heading, items in _bullet_sections:
        cleaned = [item.strip() for item in items if item.strip()]
        if cleaned:
            sections.append(heading + "\n" + "\n".join(f"- {item}" for item in cleaned))

    diagrams = [diagram.strip() for diagram in report.mermaid if diagram.strip()]
    if diagrams:
        sections.append("## Diagrams\n" + "\n\n".join(f"```mermaid\n{d}\n```" for d in diagrams))

    return "\n\n".join(sections)


def _report_from_payload(payload: dict[str, Any]) -> DesignReport:
    """Build a report from a parsed JSON object, skipping malformed entries."""
    components: list[DesignComponent] = []
    for item in payload.get("components") or []:
        if isinstance(item, dict):
            try:
                components.append(DesignComponent.model_validate(item))
            except Exception:
                continue
    api: list[ApiContract] = []
    for item in payload.get("api") or []:
        if isinstance(item, dict):
            try:
                api.append(ApiContract.model_validate(item))
            except Exception:
                continue
    data_model: list[DataEntity] = []
    for item in payload.get("data_model") or []:
        if isinstance(item, dict):
            try:
                data_model.append(DataEntity.model_validate(item))
            except Exception:
                continue

    def _strings(key: str) -> list[str]:
        return [
            str(item).strip()
            for item in (payload.get(key) or [])
            if isinstance(item, str) and item.strip()
        ]

    mermaid = [_strip_fence(diagram) for diagram in _strings("mermaid")]
    return DesignReport(
        summary=str(payload.get("summary") or "").strip(),
        assumptions=_strings("assumptions"),
        architecture=str(payload.get("architecture") or "").strip(),
        components=components,
        api=api,
        data_model=data_model,
        events=_strings("events"),
        caching=_strings("caching"),
        failure_handling=_strings("failure_handling"),
        scaling=_strings("scaling"),
        observability=_strings("observability"),
        mermaid=mermaid,
        risks=_strings("risks"),
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


def _extract_mermaid_blocks(text: str) -> list[str]:
    """Return every fenced Mermaid diagram in ``text``, in order."""
    import re

    return [
        _strip_fence(match.group(1).strip())
        for match in re.finditer(r"```mermaid\s*(.*?)```", text, re.DOTALL)
    ]


def _strip_fence(content: str) -> str:
    content = content.strip()
    return content[7:].strip() if content.startswith("```mermaid") else content
