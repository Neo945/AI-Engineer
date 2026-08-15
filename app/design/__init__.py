"""System-design mode: ``engineer design`` generates design documents.

Given a goal, the LLM produces a structured design report (architecture,
components, API contracts, data/event model, caching, failure handling,
scaling, observability, Mermaid diagrams) that renders as Markdown.
"""

from __future__ import annotations

from app.design.report import (
    DESIGN_PROMPT,
    ApiContract,
    DataEntity,
    DesignComponent,
    DesignReport,
    build_design_seed,
    parse_design_report,
    render_design,
)

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
