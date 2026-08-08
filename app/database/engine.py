"""Async SQLAlchemy engine construction and pooling."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as create_engine,
)

from app.core.config import Settings


def build_async_engine(
    settings: Settings,
    *,
    pool_size: int = 10,
    max_overflow: int = 20,
) -> AsyncEngine:
    """Create a configured async SQLAlchemy engine.

    Args:
        settings: Application settings.
        pool_size: Maximum number of persistent connections.
        max_overflow: Extra connections allowed beyond ``pool_size``.

    Returns:
        A ready-to-use async engine.
    """
    return create_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
