"""Request-scoped dependency providers for FastAPI routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.container import Container


def get_container(request: Request) -> Container:
    """Return the application container stored on request state."""
    return request.app.state.container


async def get_db_session(
    container: Annotated[Container, Depends(get_container)],
) -> AsyncIterator[AsyncSession]:
    """Yield a database session for the duration of a request."""
    async with container.session_factory() as session:
        yield session


ContainerDep = Annotated[Container, Depends(get_container)]
SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
