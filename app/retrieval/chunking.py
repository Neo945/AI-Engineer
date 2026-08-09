"""Chunking source files into indexable slices.

Chunks are cut at symbol boundaries (so a class or function stays intact) and
capped at ``max_lines`` with a small overlap so long declarations are split
without losing their head/tail. Every chunk records the symbols it contains,
which the search layer uses for symbol lookup.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.symbols import Symbol

MAX_LINES = 60
OVERLAP = 3
MIN_LINES = 5


@dataclass(frozen=True)
class Chunk:
    """A slice of source lines.

    Attributes:
        start_line: 1-indexed first line (inclusive).
        end_line: 1-indexed last line (inclusive).
        content: The raw lines joined by newlines.
        symbols: Symbols whose declaration starts inside this chunk.
    """

    start_line: int
    end_line: int
    content: str
    symbols: tuple[Symbol, ...] = ()


def chunk_source(
    source: str,
    symbols: list[Symbol],
    *,
    max_lines: int = MAX_LINES,
    overlap: int = OVERLAP,
    min_lines: int = MIN_LINES,
) -> list[Chunk]:
    """Split ``source`` into :class:`Chunk` slices, longest-first-kept."""
    lines = source.splitlines()
    total = len(lines)
    if total == 0:
        return []
    # Imports are recorded in chunks but never act as chunk boundaries, so a
    # file header stays one chunk instead of one per import.
    boundaries = sorted(
        {
            max(1, min(symbol.start_line, total))
            for symbol in symbols
            if symbol.kind != "import"
        }
    )
    cut_points = [1, *boundaries, total + 1]
    spans: list[tuple[int, int]] = []
    for index in range(len(cut_points) - 1):
        start, end = cut_points[index], cut_points[index + 1] - 1
        if end < start:
            continue
        spans.extend(_window(start, end, max_lines=max_lines, overlap=overlap))

    chunks: list[Chunk] = []
    for start, end in spans:
        contained = tuple(
            symbol for symbol in symbols if start <= symbol.start_line <= end
        )
        # Drop short interstitial gaps (blank lines between declarations) but
        # never a file's only span: a symbol-less file must still be indexed.
        if end - start + 1 < min_lines and not contained and len(spans) > 1:
            continue
        content = "\n".join(lines[start - 1 : end])
        chunks.append(
            Chunk(start_line=start, end_line=end, content=content, symbols=contained)
        )
    return chunks


def _window(start: int, end: int, *, max_lines: int, overlap: int) -> list[tuple[int, int]]:
    """Split ``start..end`` into windows of at most ``max_lines`` with overlap."""
    size = end - start + 1
    if size <= max_lines:
        return [(start, end)]
    step = max(max_lines - overlap, 1)
    windows: list[tuple[int, int]] = []
    position = start
    while position <= end:
        window_end = min(position + max_lines - 1, end)
        windows.append((position, window_end))
        if window_end == end:
            break
        position += step
    return windows
