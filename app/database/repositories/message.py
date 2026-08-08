"""Persistence operations for messages."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from app.database.models.message import Message
from app.database.repositories.base import BaseRepository


class MessageRepository(BaseRepository[Message, uuid.UUID]):
    """Message persistence and lookups."""

    model = Message

    async def max_ordinal(self, session_id: uuid.UUID) -> int:
        """Return the highest transcript ordinal for a session, or ``-1``.

        Used by the orchestrator to append a task's messages after the
        session's existing transcript.
        """
        stmt = select(func.max(Message.ordinal)).where(Message.session_id == session_id)
        value = await self._session.scalar(stmt)
        return value if value is not None else -1

    async def list_by_session(
        self,
        session_id: uuid.UUID,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[Message]:
        """List a session's transcript in chronological order."""
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.ordinal.asc(), Message.created_at.asc(), Message.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()

    async def add_many(self, messages: Sequence[Message]) -> None:
        """Persist multiple messages in a single flush."""
        self._session.add_all(messages)
        await self._session.flush()
