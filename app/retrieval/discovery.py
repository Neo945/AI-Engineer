"""Repository file discovery for the indexer.

``discover_source_files`` walks a checkout and returns the source files worth
indexing: files with a supported language extension that are not generated,
vendored, or configuration noise. Exclusions are curated defaults (no extra
dependency): hidden paths, virtual environments, dependency/build caches, and
oversized or binary-looking files are skipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from app.retrieval.languages import supported_extensions

#: Directory names that are never walked, wherever they appear.
_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".env",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".cache",
        ".egg-info",
        "htmlcov",
        ".idea",
        ".vscode",
        "target",
        "vendor",
    }
)

#: Extensions that are source-adjacent but never indexed directly.
_EXCLUDED_FILE_SUFFIXES = frozenset({".map", ".lock", ".pyc", ".pyo", ".so", ".dll", ".dylib"})

#: Files larger than this (bytes) are skipped as non-source blobs.
_MAX_FILE_BYTES = 512 * 1024

#: Suffix pairs with no extension — lockfiles etc.
_EXCLUDED_FILE_NAMES = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock"}
)


def is_excluded(path: Path) -> bool:
    """Return whether a path component matches a curated exclusion."""
    if path.name in _EXCLUDED_DIR_NAMES:
        return True
    if path.name in _EXCLUDED_FILE_NAMES:
        return True
    return path.suffix.lower() in _EXCLUDED_FILE_SUFFIXES


def discover_source_files(root: Path, extra_excluded: Iterable[str] = ()) -> list[Path]:
    """Return all indexable source files under ``root``, sorted for stability.

    Args:
        root: The workspace checkout to walk.
        extra_excluded: Extra directory names to skip (e.g. user overrides).

    Returns:
        Absolute paths of source files, sorted by relative path so repeated
        indexes are deterministic.
    """
    root = Path(root).resolve()
    extra = set(extra_excluded)
    excluded_dirs = _EXCLUDED_DIR_NAMES | extra
    extensions = supported_extensions()
    files: list[Path] = []
    for path in _walk(root):
        rel = path.relative_to(root)
        if any(part in excluded_dirs for part in rel.parts[:-1]):
            continue
        if rel.parts[0].startswith(".") or rel.parts[-1].startswith("."):
            continue
        if path.suffix.lower() not in extensions:
            continue
        if is_excluded(path):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _walk(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path
