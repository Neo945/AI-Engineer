"""Task-specific context assembly for agent runs.

A :class:`ContextAssembler` retrieves the highest-ranked chunks for a goal
and trims them to a size budget (chunk count and total characters) so the
agent loop receives a focused context window — never the whole repository.
The window is rendered as plain text and injected as an initial user
message by the orchestrator.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.database.repositories.code_chunk import CodeChunkRepository
from app.retrieval.retriever import Retriever, extract_query_terms

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models.code_chunk import CodeChunk

DEFAULT_MAX_CHUNKS = 20
DEFAULT_MAX_CHARS = 12_000


@dataclass(frozen=True)
class ContextItem:
    """One source slice included in a context window."""

    file_path: str
    start_line: int
    end_line: int
    content: str
    score: float
    matches: tuple[str, ...]

    @property
    def chars(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class ContextWindow:
    """A ranked, size-bounded set of chunks retrieved for a goal.

    Attributes:
        items: Highest-ranked context slices, best first.
        total_chars: Sum of ``items`` content lengths.
        terms: Query terms the window was retrieved with.
        truncated: True when higher-ranked chunks were dropped for budget.
    """

    items: list[ContextItem]
    total_chars: int
    terms: tuple[str, ...]
    truncated: bool


class ContextAssembler:
    """Retrieve and bound a context window for a goal.

    Args:
        retriever: Retrieval source over the workspace's ``code_chunks``.
        max_chunks: Upper bound on items in a window.
        max_chars: Upper bound on total content characters.
    """

    def __init__(
        self,
        retriever: Retriever,
        *,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._retriever = retriever
        self._max_chunks = max_chunks
        self._max_chars = max_chars

    @classmethod
    def from_session(
        cls,
        session: AsyncSession,
        *,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> ContextAssembler:
        """Build an assembler bound to a database session."""
        return cls(
            Retriever(CodeChunkRepository(session)),
            max_chunks=max_chunks,
            max_chars=max_chars,
        )

    async def assemble(self, workspace_id: uuid.UUID, goal: str) -> ContextWindow:
        """Return the best context window for ``goal`` within budget.

        The top-ranked chunk is always included (a window that drops its best
        match is empty and useless); later chunks must fit the remaining
        character budget. Retrieval pulls extra candidates so ``truncated``
        reflects a real cutoff rather than a search limit.
        """
        terms = tuple(extract_query_terms(goal))
        candidate_limit = max(self._max_chunks * 4, 1)
        scored = await self._retriever.ranked_search(
            workspace_id,
            goal,
            limit=candidate_limit,
        )
        items: list[ContextItem] = []
        total = 0
        truncated = False
        for hit in scored:
            chunk: CodeChunk = hit.chunk
            item = ContextItem(
                file_path=chunk.file_path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                content=chunk.content,
                score=hit.score,
                matches=hit.matches,
            )
            if len(items) >= self._max_chunks:
                truncated = True
                continue
            if items and total + item.chars > self._max_chars:
                truncated = True
                continue
            items.append(item)
            total += item.chars
        return ContextWindow(
            items=items,
            total_chars=total,
            terms=terms,
            truncated=truncated,
        )


def format_context(window: ContextWindow) -> str:
    """Render a context window as markdown text for the model transcript."""
    if not window.items:
        return ""
    lines = [
        "Relevant source context retrieved for this task (highest ranked first):",
        "",
    ]
    for item in window.items:
        location = f"{item.file_path}:{item.start_line}-{item.end_line}"
        lines.append(f"### {location}")
        lines.append(item.content)
        lines.append("")
    return "\n".join(lines)
