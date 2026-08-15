"""Architecture analysis: deterministic dependency graph + LLM analysis.

``engineer graph`` builds and renders a file-level dependency graph from the
workspace's source files; ``engineer arch`` seeds the LLM with that graph so
it can describe the system's components, layers, and recommendations.
"""

from __future__ import annotations

from app.architecture.deps import (
    DependencyEdge,
    FileGraph,
    ImportRef,
    build_file_graph,
    cycles,
    dependencies,
    dependents,
    graph_summary,
    hub_files,
    layers,
    neighborhood,
    orphan_files,
)
from app.architecture.render import (
    render_graph_mermaid,
    render_graph_text,
    render_node_mermaid,
    render_node_text,
)
from app.architecture.report import (
    ARCHITECTURE_PROMPT,
    ArchitectureComponent,
    ArchitectureReport,
    KeyFile,
    build_architecture_seed,
    parse_architecture_report,
    render_architecture,
)

__all__ = [
    "ARCHITECTURE_PROMPT",
    "ArchitectureComponent",
    "ArchitectureReport",
    "DependencyEdge",
    "FileGraph",
    "ImportRef",
    "KeyFile",
    "build_architecture_seed",
    "build_file_graph",
    "cycles",
    "dependencies",
    "dependents",
    "graph_summary",
    "hub_files",
    "layers",
    "neighborhood",
    "orphan_files",
    "parse_architecture_report",
    "render_architecture",
    "render_graph_mermaid",
    "render_graph_text",
    "render_node_mermaid",
    "render_node_text",
]
