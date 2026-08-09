"""Symbol extraction from source files.

Python is parsed with the standard :mod:`ast` module (accurate, including
qualified names for nested classes/methods). C-like languages
(TypeScript, JavaScript, Java, Go, Rust, C, C++, C#) use a documented
brace-matching heuristic over declarations, which is good enough to locate
chunk boundaries and feed keyword/symbol search without extra dependencies.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from app.retrieval.languages import Language


@dataclass(frozen=True)
class Symbol:
    """A named declaration inside a source file.

    Attributes:
        kind: ``class``, ``function``, ``method``, or ``import``.
        name: The declaration's bare name.
        start_line: 1-indexed first line (inclusive).
        end_line: 1-indexed last line (inclusive).
        qualified_name: Dotted path for nested declarations, e.g.
            ``utils.parser.parse``. Falls back to ``name``.
    """

    kind: str
    name: str
    start_line: int
    end_line: int
    qualified_name: str | None = None


_PY_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# --- C-like heuristic patterns ------------------------------------------------

_CLASS_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|export\s+|default\s+|"
    r"abstract\s+|final\s+|static\s+|sealed\s+|partial\s+)*"
    r"(?:class|interface|struct|enum|trait|type)\s+([A-Za-z_$][\w$]*)"
)
_FUNC_RE = re.compile(
    r"^\s*(?:export\s+|async\s+|pub\s+)?"
    r"(?:function\s*([A-Za-z_$][\w$]*))"
)
_CLIKE_METHOD_RE = re.compile(
    r"^\s{0,8}(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|"
    r"async\s+|final\s+|override\s+|fn\s+|func\s+|def\s+|pub\s+)*"
    r"(?:[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*"
)
_TS_METHOD_RE = re.compile(r"^\s{0,4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")
_TS_FUNC_EXPR_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)


def extract_symbols(source: str, language: Language) -> list[Symbol]:
    """Return the symbols declared in ``source`` for ``language``."""
    if language.parser == "ast":
        return _extract_python(source)
    return _extract_clike(source)


# --- Python (AST) ------------------------------------------------------------


def _extract_python(source: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    _visit_py_definitions(tree, "", symbols)
    _visit_py_imports(tree, symbols)
    return _sorted_unique(symbols)


def _visit_py_definitions(node: ast.AST, qualifier: str, symbols: list[Symbol]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            qualified = _qualify(qualifier, child.name)
            symbols.append(
                Symbol(
                    kind="class",
                    name=child.name,
                    start_line=child.lineno,
                    end_line=_end_line(child),
                    qualified_name=qualified,
                )
            )
            _visit_py_definitions(child, qualified, symbols)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                Symbol(
                    kind="method" if qualifier else "function",
                    name=child.name,
                    start_line=child.lineno,
                    end_line=_end_line(child),
                    qualified_name=_qualify(qualifier, child.name),
                )
            )
            _visit_py_definitions(child, _qualify(qualifier, child.name), symbols)
        else:
            _visit_py_definitions(child, qualifier, symbols)


def _visit_py_imports(tree: ast.AST, symbols: list[Symbol]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbols.append(
                    Symbol(
                        kind="import",
                        name=alias.asname or alias.name,
                        start_line=node.lineno,
                        end_line=node.lineno,
                        qualified_name=alias.name,
                    )
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                symbols.append(
                    Symbol(
                        kind="import",
                        name=alias.asname or alias.name,
                        start_line=node.lineno,
                        end_line=node.lineno,
                        qualified_name=f"{node.module}.{alias.name}",
                    )
                )


def _end_line(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None)
    if isinstance(end, int):
        return end
    lineno = getattr(node, "lineno", None)
    return lineno if isinstance(lineno, int) else 0


def _qualify(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


# --- C-like (heuristic) ------------------------------------------------------


def _extract_clike(source: str) -> list[Symbol]:
    lines = source.splitlines()
    symbols: list[Symbol] = []
    for index, line in enumerate(lines):
        start = index + 1
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#", "//")):
            continue
        class_match = _CLASS_RE.match(line)
        if class_match:
            end = _brace_end(lines, index)
            symbols.append(
                Symbol(
                    kind="class",
                    name=class_match.group(1),
                    start_line=start,
                    end_line=end,
                )
            )
            continue
        func_match = _FUNC_RE.match(line)
        if func_match:
            symbols.append(
                Symbol(
                    kind="function",
                    name=func_match.group(1),
                    start_line=start,
                    end_line=_brace_end(lines, index),
                )
            )
            continue
        expr_match = _TS_FUNC_EXPR_RE.match(line)
        if expr_match:
            symbols.append(
                Symbol(
                    kind="function",
                    name=expr_match.group(1),
                    start_line=start,
                    end_line=_brace_end(lines, index),
                )
            )
            continue
        if _TS_METHOD_RE.match(line):
            symbols.append(
                Symbol(
                    kind="method",
                    name=_TS_METHOD_RE.match(line).group(1),  # type: ignore[union-attr]
                    start_line=start,
                    end_line=_brace_end(lines, index),
                )
            )
    return _sorted_unique(symbols)


def _brace_end(lines: list[str], start_index: int) -> int:
    """Return the 1-indexed line where the block opened at ``start_index`` closes.

    Falls back to the last line when braces never balance (single-line
    declarations, closing braces on a later scanned construct).
    """
    depth = 0
    for index in range(start_index, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if index > start_index and depth <= 0:
            return index + 1
    return len(lines)


def _sorted_unique(symbols: list[Symbol]) -> list[Symbol]:
    return sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.end_line))


# --- Convenience -------------------------------------------------------------


def extract_symbols_from_path(path: Path, language: Language | None) -> list[Symbol]:
    """Read ``path`` and return its symbols; empty on decode or parse errors."""
    if language is None:
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return extract_symbols(source, language)
