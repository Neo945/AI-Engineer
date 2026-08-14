"""Memory service: durable session/project/decision memory.

The service is the single entry point for remembering and recalling durable
workspace knowledge. It composes the persistence repository with recall
logic: keyword search works offline-first, and semantic recall is layered on
when entries carry pgvector embeddings.

Remembered entries are rendered as a bounded "project memory" block that the
orchestrator injects into a run's context window, so later sessions start
with what earlier ones learned instead of re-deriving it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.database.models.enums import MemoryKind
from app.database.models.memory import MemoryEntry
from app.database.repositories.memory import MemoryRepository
from app.retrieval.retriever import extract_query_terms

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_MAX_ENTRIES = 20
DEFAULT_MAX_CHARS = 4_000
_SOURCE_STR_LIMIT = 120


class MemoryService:
    """Remember and recall durable workspace memory.

    Args:
        repository: Persistence source for memory entries.
    """

    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    @classmethod
    def from_session(cls, session: AsyncSession) -> MemoryService:
        """Build a service bound to a database session."""
        return cls(MemoryRepository(session))

    async def remember(
        self,
        workspace_id: uuid.UUID,
        *,
        content: str,
        kind: MemoryKind = MemoryKind.FACT,
        source: str = "cli",
    ) -> MemoryEntry:
        """Persist a new memory entry and return it with generated defaults."""
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        if len(content) > 100_000:
            raise ValueError("memory content is too long")
        return await self._repository.add(
            MemoryEntry(
                workspace_id=workspace_id,
                kind=kind,
                content=content,
                source=(source or "cli")[:_SOURCE_STR_LIMIT],
            )
        )

    async def list(
        self,
        workspace_id: uuid.UUID,
        *,
        kind: MemoryKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[MemoryEntry]:
        """List a workspace's memory entries, newest first."""
        return await self._repository.list_for_workspace(
            workspace_id,
            kind=kind,
            limit=limit,
            offset=offset,
        )

    async def count(self, workspace_id: uuid.UUID) -> int:
        """Return how many memory entries a workspace has."""
        return await self._repository.count_for_workspace(workspace_id)

    async def clear(self, workspace_id: uuid.UUID) -> int:
        """Delete every memory entry for a workspace; returns rows deleted."""
        return await self._repository.delete_for_workspace(workspace_id)

    async def recall(
        self,
        workspace_id: uuid.UUID,
        query: str,
        *,
        limit: int = DEFAULT_MAX_ENTRIES,
    ) -> Sequence[MemoryEntry]:
        """Return the entries most relevant to ``query``.

        Query terms are matched as literal substrings over ``content`` — one
        search per term, deduplicated and ranked by a compactness score so the
        tightest, most specific matches surface first.
        """
        terms = [term for term in extract_query_terms(query) if len(term) >= 3]
        if not terms:
            return ()
        hits: dict[uuid.UUID, MemoryEntry] = {}
        for term in terms:
            for entry in await self._repository.keyword_search(workspace_id, term, limit=limit):
                hits.setdefault(entry.id, entry)
        ranked = sorted(
            hits.values(),
            key=lambda entry: sum(
                len(term) if term.lower() in entry.content.lower() else 0 for term in terms
            ),
            reverse=True,
        )
        return tuple(ranked[:limit])


def format_memory_block(entries: Sequence[MemoryEntry]) -> str:
    """Render memory entries as a markdown context block for the agent.

    Returns an empty string when there is nothing to recall, so the context
    assembler can drop the block entirely.
    """
    if not entries:
        return ""
    lines = ["Project memory relevant to this task (highest ranked first):", ""]
    for entry in entries:
        kind = entry.kind.value if hasattr(entry.kind, "value") else str(entry.kind)
        when = entry.created_at.strftime("%Y-%m-%d") if entry.created_at else "?"
        lines.append(f"- [{kind} · {when}] {entry.content}")
    lines.append("")
    return "\n".join(lines)
