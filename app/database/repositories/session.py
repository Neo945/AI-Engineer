"""Persistence operations for sessions."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from app.database.models.enums import SessionStatus
from app.database.models.session import Session
from app.database.repositories.base import BaseRepository


class SessionRepository(BaseRepository[Session, uuid.UUID]):
    """Session persistence and lookups."""

    model = Session

    async def list_by_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Session]:
        """List sessions for a workspace, newest first."""
        stmt = (
            select(Session)
            .where(Session.workspace_id == workspace_id)
            .order_by(Session.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()

    async def count_by_status(self, workspace_id: uuid.UUID, status: SessionStatus) -> int:
        """Count sessions in a given status for a workspace."""
        stmt = (
            select(func.count())
            .select_from(Session)
            .where(
                Session.workspace_id == workspace_id,
                Session.status == status,
            )
        )
        return (await self._session.scalar(stmt)) or 0
