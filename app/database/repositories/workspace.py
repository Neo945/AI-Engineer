"""Persistence operations for workspaces."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.database.models.workspace import Workspace
from app.database.repositories.base import BaseRepository


class WorkspaceRepository(BaseRepository[Workspace, uuid.UUID]):
    """Workspace persistence and lookups."""

    model = Workspace

    async def list_by_owner(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Workspace]:
        """List a user's workspaces, newest first."""
        stmt = (
            select(Workspace)
            .where(Workspace.owner_id == owner_id)
            .order_by(Workspace.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()
