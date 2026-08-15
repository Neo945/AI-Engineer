"""Deterministic file-level dependency graph extraction.

Walks a workspace's source files (reusing the retrieval discovery and
language detection), extracts ``import``/``require``/``using``/``#include``
statements, and resolves each to a file inside the workspace where possible.
The result is a file-level graph plus metrics (hubs, cycles, orphans,
layering) used by ``engineer graph`` and seeded to the LLM for
``engineer arch``.

Resolution is best-effort and membership-checked: an import only becomes an
edge if the target actually maps to a known file, so false edges cannot be
introduced by a sloppy heuristic. Unresolved imports are collected per file
as ``unresolved`` (external dependencies) and simply not edges.
"""

from __future__ import annotations

import ast
import re
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

from app.retrieval.discovery import discover_source_files
from app.retrieval.languages import Language, detect_language

_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

_TS_IMPORT_RE = re.compile(
    r"""(?:from\s*|require\(\s*|import\(\s*|export\s+.*?\s+from\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_TS_SIDE_IMPORT_RE = re.compile(r"^\s*import\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_GO_SPEC_RE = re.compile(r"""(?:[A-Za-z_]\w*\s+)?[`"]([^`"]+)[`"]""")
_RUST_USE_RE = re.compile(r"^\s*use\s+([^;]+);", re.MULTILINE)
_RUST_MOD_RE = re.compile(r"^\s*mod\s+([A-Za-z_]\w*)\s*;", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)
_CSHARP_USING_RE = re.compile(r"^\s*using\s+(?:([\w.]+)\s*=\s*)?([\w.]+)\s*;", re.MULTILINE)
_C_INCLUDE_RE = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE)


@dataclass(frozen=True)
class ImportRef:
    """One import/require/using/include statement inside a file.

    Attributes:
        target: The normalized import specifier (e.g. ``a.b.c``, ``./foo``,
            ``crate::foo``, ``foo.h``).
        name: For ``from <module> import <name>``, the imported name (used to
            resolve submodules).
        relative: Whether the import is relative (Python dots, ``./``/``../``).
        level: Python relative-import depth (0 for absolute imports).
    """

    target: str
    name: str | None = None
    relative: bool = False
    level: int = 0


@dataclass(frozen=True, order=True)
class DependencyEdge:
    """A ``source -> target`` edge between two workspace files."""

    source: str
    target: str


@dataclass(frozen=True)
class FileGraph:
    """The file-level dependency graph of a workspace.

    Attributes:
        nodes: Every supported source file, as a root-relative posix path.
        edges: Deduplicated internal dependency edges.
        unresolved: For each file, the imports that did not resolve to a
            known file (external dependencies), sorted and deduplicated.
    """

    nodes: tuple[str, ...]
    edges: tuple[DependencyEdge, ...]
    unresolved: dict[str, tuple[str, ...]]


def build_file_graph(root: str | Path) -> FileGraph:
    """Build the :class:`FileGraph` for the workspace at ``root``."""
    root = Path(root).resolve()
    files = discover_source_files(root)
    known = {path.relative_to(root).as_posix() for path in files}
    by_basename: dict[str, list[str]] = {}
    for rel in known:
        by_basename.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    edges: set[tuple[str, str]] = set()
    unresolved: dict[str, list[str]] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        language = detect_language(path)
        if language is None:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        targets: set[str] = set()
        missed: list[str] = []
        for import_ref in extract_imports(source, language):
            resolved = _resolve(rel_dir, language.name, import_ref, known, by_basename)
            if resolved:
                targets.update(resolved)
            else:
                missed.append(import_ref.target)
        for target in targets:
            if target != rel:
                edges.add((rel, target))
        if missed:
            unresolved[rel] = sorted(set(missed))
    return FileGraph(
        nodes=tuple(sorted(known)),
        edges=tuple(sorted(DependencyEdge(source, target) for source, target in edges)),
        unresolved={file: tuple(refs) for file, refs in unresolved.items()},
    )


# --- Import extraction ------------------------------------------------------


def extract_imports(source: str, language: Language) -> list[ImportRef]:
    """Return the import statements in ``source`` for ``language``."""
    if language.parser == "ast":
        return _extract_python_imports(source)
    return _extract_clike_imports(source, language.name)


def _extract_python_imports(source: str) -> list[ImportRef]:
    refs: list[ImportRef] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return refs
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.append(ImportRef(target=alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            name = node.names[0].name if node.names else None
            refs.append(
                ImportRef(
                    target=module,
                    name=name,
                    relative=node.level > 0,
                    level=node.level,
                )
            )
    return refs


def _extract_clike_imports(source: str, language_name: str) -> list[ImportRef]:
    if language_name in ("typescript", "javascript"):
        refs = [ImportRef(target=match) for match in _TS_IMPORT_RE.findall(source)]
        refs += [ImportRef(target=match) for match in _TS_SIDE_IMPORT_RE.findall(source)]
        return refs
    if language_name == "go":
        refs = []
        in_block = False
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import"):
                rest = stripped[6:].strip()
                if rest.startswith("("):
                    in_block = True
                    rest = rest[1:].strip()
                match = _GO_SPEC_RE.search(rest)
                if match:
                    refs.append(ImportRef(target=match.group(1)))
                continue
            if in_block:
                if stripped == ")":
                    in_block = False
                    continue
                match = _GO_SPEC_RE.search(stripped)
                if match:
                    refs.append(ImportRef(target=match.group(1)))
        return refs
    if language_name == "rust":
        refs = []
        for match in _RUST_USE_RE.findall(source):
            target = match.strip().split("::", 1)[0].strip()
            if target:
                refs.append(ImportRef(target=match.strip()))
        for name in _RUST_MOD_RE.findall(source):
            refs.append(ImportRef(target=f"mod::{name}"))
        return refs
    if language_name == "java":
        return [ImportRef(target=match) for match in _JAVA_IMPORT_RE.findall(source)]
    if language_name == "csharp":
        refs = []
        for alias, target in _CSHARP_USING_RE.findall(source):
            refs.append(ImportRef(target=alias or target))
        return refs
    if language_name in ("c", "cpp"):
        return [ImportRef(target=match) for match in _C_INCLUDE_RE.findall(source)]
    return []


# --- Resolution (each returns only known files) -----------------------------


def _resolve(
    rel_dir: str,
    language_name: str,
    import_ref: ImportRef,
    known: set[str],
    by_basename: dict[str, list[str]],
) -> list[str]:
    if language_name == "python":
        return _resolve_python(rel_dir, import_ref, known)
    if language_name in ("typescript", "javascript"):
        return _resolve_ts(rel_dir, import_ref.target, known)
    if language_name == "go":
        return _resolve_go(import_ref.target, known)
    if language_name == "rust":
        return _resolve_rust(import_ref.target, known)
    if language_name == "java":
        return _resolve_java(import_ref.target, known)
    if language_name == "csharp":
        return _resolve_dotted(import_ref.target, known, ".cs")
    if language_name in ("c", "cpp"):
        return _resolve_by_basename(import_ref.target, by_basename)
    return []


def _module_files(segments: list[str]) -> list[str]:
    """Candidate files for a dotted module path (module ``a.b.c``)."""
    if not segments:
        return []
    path = "/".join(segments)
    return [path + ".py", path + "/__init__.py"]


def _resolve_python(rel_dir: str, import_ref: ImportRef, known: set[str]) -> list[str]:
    if import_ref.relative:
        parts = [part for part in rel_dir.split("/") if part]
        drop = max(0, import_ref.level - 1)
        pkg = parts[: len(parts) - drop] if len(parts) > drop else []
        if import_ref.target:
            pkg = pkg + import_ref.target.split(".")
    else:
        pkg = import_ref.target.split(".")
    candidates = set(_module_files(pkg))
    if not import_ref.relative:
        candidates |= _module_suffix_candidates(pkg, known)
    if import_ref.name:
        named = [*pkg, import_ref.name]
        candidates.update(_module_files(named))
        if not import_ref.relative:
            candidates |= _module_suffix_candidates(named, known)
    return sorted(candidate for candidate in candidates if candidate in known)


def _module_suffix_candidates(segments: list[str], known: set[str]) -> set[str]:
    """Known files whose path ends with this module path (any package root)."""
    if not segments:
        return set()
    path = "/".join(segments)
    return {
        rel
        for rel in known
        if rel.endswith("/" + path + ".py") or rel.endswith("/" + path + "/__init__.py")
    }


def _resolve_ts(rel_dir: str, target: str, known: set[str]) -> list[str]:
    if target.startswith("."):
        if target.startswith("./"):
            base = (rel_dir + "/" + target[2:]) if rel_dir else target[2:]
        else:
            parts = [part for part in rel_dir.split("/") if part]
            up = 0
            while target.startswith("../"):
                up += 1
                target = target[3:]
            parts = parts[: len(parts) - up] if up <= len(parts) else []
            base = "/".join(parts + ([target] if target else []))
        return sorted(_ts_candidates(base, known))
    candidates = list(_ts_candidates(target, known))
    candidates += list(_ts_candidates("src/" + target, known))
    return sorted(set(candidates))


def _ts_candidates(base: str, known: set[str]) -> list[str]:
    found: list[str] = []
    for extension in _TS_EXTS:
        for candidate in (base + extension, f"{base}/index{extension}"):
            if candidate in known:
                found.append(candidate)
    return found


def _resolve_go(target: str, known: set[str]) -> list[str]:
    parts = [part for part in target.split("/") if part]
    if not parts:
        return []
    base = "/".join(parts[-2:])
    candidates = [base + ".go"]
    candidates += sorted(rel for rel in known if rel.startswith(base + "/") and rel.endswith(".go"))
    return sorted(candidate for candidate in candidates if candidate in known)


def _resolve_rust(target: str, known: set[str]) -> list[str]:
    segments = [
        segment
        for segment in target.replace("mod::", "mod/").split("::")
        if segment and segment not in ("crate", "self", "super", "std", "core", "alloc")
    ]
    if not segments:
        return []
    candidates: list[str] = []
    for depth in (1, 2):
        path = "/".join(segments[:depth])
        candidates += [f"src/{path}.rs", f"src/{path}/mod.rs"]
    return sorted(candidate for candidate in set(candidates) if candidate in known)


def _resolve_java(target: str, known: set[str]) -> list[str]:
    spec = target.rstrip(";").strip()
    if spec.endswith(".*"):
        base = spec[:-2].replace(".", "/")
        return sorted(rel for rel in known if rel.startswith(base + "/") and rel.endswith(".java"))
    return [candidate for candidate in (spec.replace(".", "/") + ".java",) if candidate in known]


def _resolve_dotted(target: str, known: set[str], suffix: str) -> list[str]:
    spec = target.strip().rstrip(";")
    if "=" in spec:
        spec = spec.split("=", 1)[1].strip()
    return [candidate for candidate in (spec.replace(".", "/") + suffix,) if candidate in known]


def _resolve_by_basename(target: str, by_basename: dict[str, list[str]]) -> list[str]:
    return sorted(rel for rel in by_basename.get(target.strip().rstrip(";"), []))


# --- Graph metrics ----------------------------------------------------------


def dependencies(graph: FileGraph, node: str) -> list[str]:
    """Files ``node`` imports (its outgoing edges)."""
    return [edge.target for edge in graph.edges if edge.source == node]


def dependents(graph: FileGraph, node: str) -> list[str]:
    """Files that import ``node`` (its incoming edges)."""
    return [edge.source for edge in graph.edges if edge.target == node]


def hub_files(graph: FileGraph, limit: int = 10) -> list[tuple[str, int]]:
    """The files with the most dependents, most first."""
    counts: Counter[str] = Counter(edge.target for edge in graph.edges)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def orphan_files(graph: FileGraph) -> list[str]:
    """Files with no dependency edges at all (neither importer nor imported)."""
    connected = {edge.source for edge in graph.edges} | {edge.target for edge in graph.edges}
    return [node for node in graph.nodes if node not in connected]


def cycles(graph: FileGraph) -> list[tuple[str, ...]]:
    """Dependency cycles (strongly connected components with > 1 node)."""
    return [
        tuple(sorted(component))
        for component in _strongly_connected_components(graph)
        if len(component) > 1
    ]


def layers(graph: FileGraph) -> dict[str, int]:
    """Longest-path dependency depth for each file (1-based).

    Computed over the condensation of the graph, so files in a cycle share a
    layer. ``layer >= 1`` for every node.
    """
    components = _strongly_connected_components(graph)
    component_of = {node: index for index, component in enumerate(components) for node in component}
    component_edges: set[tuple[int, int]] = set()
    adjacency: dict[int, list[int]] = {index: [] for index in range(len(components))}
    indegree: Counter[int] = Counter()
    for edge in graph.edges:
        source, target = component_of[edge.source], component_of[edge.target]
        if source != target:
            component_edges.add((source, target))
    for source, target in component_edges:
        adjacency[source].append(target)
        indegree[target] += 1
    queue = deque(index for index in range(len(components)) if indegree[index] == 0)
    depth: dict[int, int] = {index: 1 for index in range(len(components))}
    while queue:
        current = queue.popleft()
        for target in adjacency[current]:
            depth[target] = max(depth[target], depth[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return {node: depth[component_of[node]] for node in graph.nodes}


def neighborhood(graph: FileGraph, node: str, depth: int = 1) -> set[str]:
    """The set of files within ``depth`` edges of ``node`` (both directions)."""
    result = {node}
    frontier = {node}
    for _ in range(max(0, depth)):
        next_frontier: set[str] = set()
        for current in frontier:
            next_frontier.update(dependencies(graph, current))
            next_frontier.update(dependents(graph, current))
        result |= next_frontier
        frontier = next_frontier
    return result


def file_language_counts(graph: FileGraph) -> list[tuple[str, int]]:
    """Per-language file counts, most common first."""
    counts: Counter[str] = Counter()
    for node in graph.nodes:
        language = detect_language(Path(node))
        counts[language.name if language else node.rsplit(".", 1)[-1]] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def graph_summary(graph: FileGraph) -> str:
    """A compact text summary of the graph, used to seed the LLM analysis."""
    lines = [
        f"files: {len(graph.nodes)}, internal dependency edges: {len(graph.edges)}",
        "languages: " + ", ".join(f"{name}={count}" for name, count in file_language_counts(graph)),
    ]
    hubs = hub_files(graph, 10)
    lines.append("most depended-on files (top 10):")
    lines += [f"  {file} ({count} dependents)" for file, count in hubs]
    detected = cycles(graph)
    if detected:
        lines.append(f"dependency cycles ({len(detected)}):")
        for cycle in detected:
            lines.append(f"  {' -> '.join(cycle + cycle[:1])}")
    else:
        lines.append("dependency cycles: none")
    orphans = orphan_files(graph)
    lines.append(f"orphan files (no dependency edges): {len(orphans)}")
    lines += [f"  {file}" for file in orphans[:20]]
    if len(orphans) > 20:
        lines.append(f"  ... and {len(orphans) - 20} more")
    unresolved = sum(len(refs) for refs in graph.unresolved.values())
    lines.append(f"unresolved (external) imports: {unresolved}")
    return "\n".join(lines)


def _strongly_connected_components(graph: FileGraph) -> list[list[str]]:
    """Iterative Tarjan; returns every SCC (including singletons)."""
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []
    for start in graph.nodes:
        if start in index:
            continue
        work = [(start, iter(dependencies(graph, start)))]
        index[start] = lowlink[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, successors = work[-1]
            advanced = False
            for successor in successors:
                if successor not in index:
                    index[successor] = lowlink[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, iter(dependencies(graph, successor))))
                    advanced = True
                    break
                if successor in on_stack:
                    lowlink[node] = min(lowlink[node], index[successor])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index[node]:
                component: list[str] = []
                while stack:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    return components
