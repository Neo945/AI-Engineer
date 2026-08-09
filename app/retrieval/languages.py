"""Language detection for source files in a workspace.

A small, dependency-free registry maps file extensions to languages. The
indexer uses :func:`detect_language` to decide whether a discovered file is
source it should index and how to extract symbols from it. Symbol extraction
has two strategies: a real AST walk for Python and a brace-matching heuristic
for C-like languages (TypeScript, JavaScript, Java, Go, Rust, C, C++, C#).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Language:
    """A supported source language.

    Attributes:
        name: Canonical name stored in ``code_chunks.language``.
        extensions: File extensions (with leading dot) that map to it.
        parser: ``ast`` for real AST extraction, ``clike`` for the heuristic.
    """

    name: str
    extensions: frozenset[str]
    parser: str


_LANGUAGES: tuple[Language, ...] = (
    Language("python", frozenset({".py", ".pyi"}), "ast"),
    Language("typescript", frozenset({".ts", ".tsx", ".mts", ".cts"}), "clike"),
    Language("javascript", frozenset({".js", ".jsx", ".mjs", ".cjs"}), "clike"),
    Language("java", frozenset({".java"}), "clike"),
    Language("go", frozenset({".go"}), "clike"),
    Language("rust", frozenset({".rs"}), "clike"),
    Language("c", frozenset({".c", ".h"}), "clike"),
    Language("cpp", frozenset({".cc", ".cpp", ".hpp", ".hxx"}), "clike"),
    Language("csharp", frozenset({".cs"}), "clike"),
)

_BY_EXTENSION: dict[str, Language] = {
    extension: language for language in _LANGUAGES for extension in language.extensions
}


def detect_language(path: Path) -> Language | None:
    """Return the :class:`Language` for ``path``, or ``None`` if unsupported.

    Matching is case-insensitive so ``FOO.PY`` behaves like ``foo.py``.
    """
    return _BY_EXTENSION.get(path.suffix.lower())


def supported_extensions() -> frozenset[str]:
    """Return every extension the indexer will consider source."""
    return frozenset().union(*(language.extensions for language in _LANGUAGES))
