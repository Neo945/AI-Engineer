"""Async session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to ``engine``.

    Args:
        engine: The engine sessions will use.

    Returns:
        A session factory producing ``AsyncSession`` instances.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
