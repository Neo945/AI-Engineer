"""Deterministic rendering of the file-level dependency graph.

``engineer graph`` prints either a plain-text summary or a Mermaid flowchart
derived from the graph's nodes and edges, so the output is reproducible and
usable in docs, PRs, or Mermaid-aware viewers. Mermaid node identifiers are
generated positionally and labels sanitized, so odd file names cannot break
the diagram.
"""

from __future__ import annotations

from collections.abc import Callable

from app.architecture.deps import (
    FileGraph,
    dependencies,
    dependents,
    file_language_counts,
    hub_files,
    neighborhood,
)

__all__ = [
    "render_graph_mermaid",
    "render_graph_text",
    "render_node_mermaid",
    "render_node_text",
]

#: Names that are invalid bare Mermaid flowchart identifiers.
_MERMAID_RESERVED = frozenset({"end", "subgraph", "direction", "click"})

_DEFAULT_MAX_NODES = 200


def render_graph_text(graph: FileGraph, *, max_nodes: int = _DEFAULT_MAX_NODES) -> str:
    """Render the graph as a plain-text report.

    Args:
        graph: The file graph to render.
        max_nodes: Cap on nodes listed in the body (nodes are still counted).
    """
    sections: list[str] = []
    languages = ", ".join(f"{name}={count}" for name, count in file_language_counts(graph))
    sections.append(f"files: {len(graph.nodes)}, internal dependency edges: {len(graph.edges)}")
    sections.append(f"languages: {languages}")
    if graph.nodes:
        shown = graph.nodes[:max_nodes] if len(graph.nodes) > max_nodes else graph.nodes
        body = [f"  {node}" for node in shown]
        if len(graph.nodes) > max_nodes:
            body.append(f"  ... and {len(graph.nodes) - max_nodes} more files")
        sections.append("files:\n" + "\n".join(body))
    hubs = hub_files(graph, 10)
    if hubs:
        sections.append(
            "most depended-on files:\n"
            + "\n".join(f"  {file} ({count} dependents)" for file, count in hubs)
        )
    connected = {edge.source for edge in graph.edges} | {edge.target for edge in graph.edges}
    orphans = [node for node in graph.nodes if node not in connected]
    sections.append(f"orphan files (no dependency edges): {len(orphans)}")
    sections.append(
        f"unresolved (external) imports: {sum(len(v) for v in graph.unresolved.values())}"
    )
    return "\n".join(sections)


def render_node_text(graph: FileGraph, node: str, *, depth: int = 1) -> str:
    """Render one file's immediate neighborhood as text.

    Args:
        graph: The file graph.
        node: The file (root-relative path) to focus on.
        depth: How many edge-hops to include in both directions.
    """
    if node not in graph.nodes:
        raise ValueError(f"unknown file in graph: {node}")
    lines = [node, "imports:"]
    lines += [f"  {target}" for target in dependencies(graph, node)] or ["  (none)"]
    lines.append("imported by:")
    lines += [f"  {source}" for source in dependents(graph, node)] or ["  (none)"]
    if depth > 1:
        nearby = sorted(neighborhood(graph, node, depth) - {node})
        lines.append(f"within {depth} hops ({len(nearby)} files):")
        lines += [f"  {other}" for other in nearby] or ["  (none)"]
    return "\n".join(lines)


def render_graph_mermaid(
    graph: FileGraph,
    *,
    max_nodes: int = _DEFAULT_MAX_NODES,
    include_unresolved: bool = False,
    label: Callable[[str], str] = lambda node: node,
) -> str:
    """Render the graph as a Mermaid ``flowchart``.

    Only edges whose endpoints are both rendered are included; edges touching
    a dropped node are omitted so the diagram stays valid and bounded. With
    ``include_unresolved``, external dependencies become dashed stub nodes.

    Args:
        graph: The file graph to render.
        max_nodes: Upper bound on the number of file nodes emitted.
        include_unresolved: Whether to add dashed external-dependency stubs.
        label: Optional transform applied to node labels (e.g. to strip a
            ``src/`` prefix).
    """
    included = list(graph.nodes[:max_nodes]) if len(graph.nodes) > max_nodes else list(graph.nodes)
    if not included:
        return "flowchart TD"
    node_ids = {node: f"n{index}" for index, node in enumerate(included)}
    lines = ["flowchart TD"]
    for node in included:
        lines.append(_mermaid_node(node_ids[node], label(node)))
    for edge in graph.edges:
        if edge.source in node_ids and edge.target in node_ids:
            lines.append(f"    {node_ids[edge.source]} --> {node_ids[edge.target]}")
    if include_unresolved:
        for node, refs in graph.unresolved.items():
            if node not in node_ids:
                continue
            for ref in list(refs)[:5]:
                lines.append(f"    {node_ids[node]} -.-> {_mermaid_node_id(ref)}")
    return "\n".join(lines)


def render_node_mermaid(graph: FileGraph, node: str, *, depth: int = 1) -> str:
    """Render a single file's neighborhood as a Mermaid ``flowchart``.

    Args:
        graph: The file graph.
        node: The file to focus on.
        depth: How many edge-hops to include in both directions.
    """
    if node not in graph.nodes:
        raise ValueError(f"unknown file in graph: {node}")
    focus = sorted(neighborhood(graph, node, depth))
    node_ids = {other: _mermaid_node_id(other) for other in focus}
    lines = ["flowchart TD"]
    lines.append(f"    {node_ids[node]}[{_mermaid_label(node)}]")
    for edge in graph.edges:
        if edge.source in node_ids and edge.target in node_ids:
            lines.append(f"    {node_ids[edge.source]} --> {node_ids[edge.target]}")
    return "\n".join(lines)


def _mermaid_node(node_id: str, label_text: str) -> str:
    return f"    {node_id}[{_mermaid_label(label_text)}]"


def _mermaid_node_id(node: str) -> str:
    clean = node.replace("/", "_").replace("\\", "_").replace(".", "_").replace("-", "_")
    clean = "".join(char for char in clean if char.isalnum() or char == "_")
    clean = clean or "node"
    if clean[0].isdigit():
        clean = "n_" + clean
    if clean in _MERMAID_RESERVED:
        clean = "n_" + clean
    return clean


def _mermaid_label(text: str) -> str:
    return text.replace('"', "#quot;").replace("\n", " ").replace("\r", "")
