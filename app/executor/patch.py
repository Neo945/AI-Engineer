"""Unified diff parsing and application for the ``edit_file`` tool.

Diffs are applied with exact content matching and no fuzz: every context and
removed line must match the file byte-for-byte (line-wise), which makes the
tool deterministic and prevents whitespace-drift surprises. If the diff does
not match at the line numbers it claims, the applier searches the file for an
exact match of the hunk's old block so small position drift is tolerated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_SEARCH_RANGE = 200


class PatchError(ValueError):
    """Raised when a unified diff cannot be parsed or applied cleanly."""


@dataclass(frozen=True)
class _Hunk:
    """One parsed ``@@ -l,c +l,c @@`` hunk.

    Attributes:
        old_start: 1-based line number in the original file (``-`` side).
        old_block: Lines expected in the original file, in diff order
            (context and removed lines interleaved as written).
        new_block: Lines that replace the old block, in diff order
            (context and added lines interleaved as written).
    """

    old_start: int
    old_block: list[str]
    new_block: list[str]

    @property
    def old_count(self) -> int:
        return len(self.old_block)

    @property
    def new_count(self) -> int:
        return len(self.new_block)


@dataclass(frozen=True)
class AppliedEdit:
    """Summary of an applied diff.

    Attributes:
        old_lines: Number of lines matched (removed + context) in total.
        new_lines: Number of lines present after the edit, in total.
        hunks: How many hunks were applied.
    """

    old_lines: int
    new_lines: int
    hunks: int


def _parse_hunks(diff: str) -> list[_Hunk]:
    lines = diff.splitlines()
    hunks: list[_Hunk] = []
    old_block: list[str] = []
    new_block: list[str] = []
    current: _Hunk | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            current.old_block.extend(old_block)
            current.new_block.extend(new_block)
            hunks.append(current)
            current = None
            old_block.clear()
            new_block.clear()

    for line in lines:
        if line.startswith("@@ ") and _HUNK_HEADER.match(line):
            flush()
            match = _HUNK_HEADER.match(line)
            assert match is not None
            current = _Hunk(
                old_start=int(match.group(1)),
                old_block=[],
                new_block=[],
            )
            continue
        if current is None:
            if line.startswith(("--- ", "+++ ")) or line == "":
                continue
            raise PatchError(f"expected a @@ hunk header, got {line!r}")
        if line.startswith("\\"):
            continue
        if line.startswith(" "):
            old_block.append(line[1:])
            new_block.append(line[1:])
        elif line.startswith("-"):
            old_block.append(line[1:])
        elif line.startswith("+"):
            new_block.append(line[1:])
        else:
            raise PatchError(f"malformed diff line: {line!r}")
    flush()
    if not hunks:
        raise PatchError("diff contains no hunks")
    return hunks


def _find_match(lines: list[str], block: list[str], hint: int) -> int | None:
    """Locate an exact match of ``block`` near ``hint`` (else anywhere)."""
    size = len(block)
    if size == 0:
        return max(0, min(hint, len(lines)))
    for start in range(min(hint, len(lines) - size), max(-1, hint - _SEARCH_RANGE - 1), -1):
        if lines[start : start + size] == block:
            return start
    for start in range(hint + 1, min(len(lines) - size, hint + _SEARCH_RANGE) + 1):
        if lines[start : start + size] == block:
            return start
    for start in range(len(lines) - size + 1):
        if lines[start : start + size] == block:
            return start
    return None


def apply_unified_diff(content: str, diff: str) -> tuple[str, AppliedEdit]:
    """Apply a unified diff to ``content``.

    Args:
        content: The current file contents.
        diff: A unified diff (hunks; ``---``/``+++`` headers are optional).

    Returns:
        ``(new_content, AppliedEdit)``.

    Raises:
        PatchError: If the diff is malformed or does not match the file.
    """
    hunks = _parse_hunks(diff)
    lines = content.splitlines()
    ends_with_newline = content.endswith("\n")
    offset = 0
    total_old = 0
    total_new = 0
    for hunk in hunks:
        hint = hunk.old_start - 1 + offset
        start = _find_match(lines, hunk.old_block, hint)
        if start is None:
            expectation = "\n".join(hunk.old_block[:5])
            raise PatchError(
                f"diff does not match the file near line {hunk.old_start}:\n{expectation}"
            )
        lines[start : start + hunk.old_count] = hunk.new_block
        delta = hunk.new_count - hunk.old_count
        offset += delta
        total_old += hunk.old_count
        total_new += hunk.new_count
    new_content = "\n".join(lines)
    if ends_with_newline:
        new_content += "\n"
    return new_content, AppliedEdit(
        old_lines=total_old,
        new_lines=total_new,
        hunks=len(hunks),
    )
