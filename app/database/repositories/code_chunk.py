"""Persistence operations for indexed code chunks."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from app.database.models.code_chunk import CodeChunk
from app.database.repositories.base import BaseRepository


class CodeChunkRepository(BaseRepository[CodeChunk, uuid.UUID]):
    """Code chunk persistence, re-indexing, and search.

    Indexing is workspace-scoped and idempotent: :meth:`replace_workspace_index`
    drops the workspace's prior index and inserts a fresh one in one round-trip,
    so running ``engineer index`` twice converges to the same rows.
    """

    model = CodeChunk

    async def delete_for_workspace(self, workspace_id: uuid.UUID) -> int:
        """Delete every chunk for ``workspace_id``; returns rows deleted."""
        result = await self._session.execute(
            delete(CodeChunk).where(CodeChunk.workspace_id == workspace_id)
        )
        return cast(CursorResult[Any], result).rowcount or 0

    async def count_for_workspace(self, workspace_id: uuid.UUID) -> int:
        """Return the number of indexed chunks for ``workspace_id``."""
        stmt = (
            select(func.count())
            .select_from(CodeChunk)
            .where(CodeChunk.workspace_id == workspace_id)
        )
        return (await self._session.scalar(stmt)) or 0

    async def list_for_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> Sequence[CodeChunk]:
        """List a workspace's chunks, ordered by file then start line."""
        stmt = (
            select(CodeChunk)
            .where(CodeChunk.workspace_id == workspace_id)
            .order_by(CodeChunk.file_path.asc(), CodeChunk.start_line.asc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()

    async def keyword_search(
        self,
        workspace_id: uuid.UUID,
        query: str,
        *,
        limit: int = 20,
    ) -> Sequence[CodeChunk]:
        """Search chunk content by substring, best (longest) match first.

        A cheap case-insensitive scan over ``content``; symbol and semantic
        search layers sit on top. Queries are matched as literal substrings,
        so no SQL wildcard injection is possible.
        """
        stmt = (
            select(CodeChunk)
            .where(
                CodeChunk.workspace_id == workspace_id,
                CodeChunk.content.ilike(f"%{query}%"),
            )
            .order_by(func.char_length(CodeChunk.content).asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def symbol_search(
        self,
        workspace_id: uuid.UUID,
        name: str,
        *,
        limit: int = 20,
    ) -> Sequence[CodeChunk]:
        """Return chunks containing a symbol (by bare or qualified name).

        Uses JSONB containment on the ``meta.symbols`` array, so the match is
        exact against names recorded by the indexer.
        """
        stmt = (
            select(CodeChunk)
            .where(
                CodeChunk.workspace_id == workspace_id,
                CodeChunk.meta["symbols"].contains([name]),
            )
            .order_by(CodeChunk.file_path.asc(), CodeChunk.start_line.asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_files(self, workspace_id: uuid.UUID, *, limit: int = 1000) -> Sequence[str]:
        """Return the distinct indexed file paths for a workspace."""
        stmt = (
            select(CodeChunk.file_path)
            .where(CodeChunk.workspace_id == workspace_id)
            .order_by(CodeChunk.file_path.asc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def replace_workspace_index(
        self,
        workspace_id: uuid.UUID,
        chunks: Sequence[CodeChunk],
    ) -> int:
        """Replace a workspace's index in one operation; returns rows inserted."""
        await self.delete_for_workspace(workspace_id)
        if chunks:
            self._session.add_all(chunks)
            await self._session.flush()
        return len(chunks)
