"""Persistence operations for users."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.database.models.user import User
from app.database.repositories.base import BaseRepository


class UserRepository(BaseRepository[User, uuid.UUID]):
    """User persistence and lookups."""

    model = User

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email, or ``None``."""
        stmt = select(User).where(User.email == email)
        return await self._session.scalar(stmt)

    async def list_active(self, *, limit: int = 100, offset: int = 0) -> Sequence[User]:
        """List active users, newest first."""
        stmt = (
            select(User)
            .where(User.is_active.is_(True))
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()
