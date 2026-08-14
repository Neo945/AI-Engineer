"""Persistence operations for durable workspace memory."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from app.database.models.enums import MemoryKind
from app.database.models.memory import MemoryEntry
from app.database.repositories.base import BaseRepository


class MemoryRepository(BaseRepository[MemoryEntry, uuid.UUID]):
    """Memory entry persistence, recall, and clearing.

    Recall is offline-first: keyword search over ``content`` works as soon as
    entries exist, and ``semantic_search`` unlocks pgvector ranking once the
    ``embedding`` column is backfilled.
    """

    model = MemoryEntry

    async def list_for_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        kind: MemoryKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[MemoryEntry]:
        """List a workspace's memory entries, newest first."""
        stmt = (
            select(MemoryEntry)
            .where(MemoryEntry.workspace_id == workspace_id)
            .order_by(MemoryEntry.created_at.desc(), MemoryEntry.id.asc())
            .limit(limit)
            .offset(offset)
        )
        if kind is not None:
            stmt = stmt.where(MemoryEntry.kind == kind)
        return (await self._session.scalars(stmt)).all()

    async def count_for_workspace(self, workspace_id: uuid.UUID) -> int:
        """Return the number of memory entries for ``workspace_id``."""
        stmt = (
            select(func.count())
            .select_from(MemoryEntry)
            .where(MemoryEntry.workspace_id == workspace_id)
        )
        return (await self._session.scalar(stmt)) or 0

    async def keyword_search(
        self,
        workspace_id: uuid.UUID,
        query: str,
        *,
        limit: int = 20,
    ) -> Sequence[MemoryEntry]:
        """Search memory content by substring, longest (most specific) first.

        A cheap case-insensitive scan over ``content``; queries are matched as
        literal substrings so no SQL wildcard injection is possible.
        """
        stmt = (
            select(MemoryEntry)
            .where(
                MemoryEntry.workspace_id == workspace_id,
                MemoryEntry.content.ilike(f"%{query}%"),
            )
            .order_by(func.char_length(MemoryEntry.content).asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def semantic_search(
        self,
        workspace_id: uuid.UUID,
        embedding: list[float],
        *,
        limit: int = 20,
    ) -> Sequence[MemoryEntry]:
        """Return entries most similar to ``embedding`` by cosine distance."""
        stmt = (
            select(MemoryEntry)
            .where(
                MemoryEntry.workspace_id == workspace_id,
                MemoryEntry.embedding.is_not(None),
            )
            .order_by(MemoryEntry.embedding.cosine_distance(embedding))
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def delete_for_workspace(self, workspace_id: uuid.UUID) -> int:
        """Delete every memory entry for ``workspace_id``; returns rows deleted."""
        result = await self._session.execute(
            delete(MemoryEntry).where(MemoryEntry.workspace_id == workspace_id)
        )
        return cast(CursorResult[Any], result).rowcount or 0
