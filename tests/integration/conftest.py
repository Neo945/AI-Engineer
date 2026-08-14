"""Fixtures for integration tests.

All fixtures in this directory require PostgreSQL and Redis reachable on
localhost (``make up``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.container import Container


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[Container]:
    """Yield a fully wired container and release its resources on teardown."""
    container = Container.build(settings)
    yield container
    await container.aclose()


@pytest.fixture
async def db_session(container: Container) -> AsyncIterator[AsyncSession]:
    """Yield a fresh database session per test."""
    async with container.session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
async def _clean_tables(container: Container) -> AsyncIterator[None]:
    """Truncate all domain tables after each integration test."""
    yield
    async with container.engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE messages, tasks, code_chunks, memory_entries, "
                "sessions, workspaces, users RESTART IDENTITY CASCADE"
            )
        )
