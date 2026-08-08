"""Request-scoped dependency providers for FastAPI routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.container import Container
from app.core.logging import get_logger
from app.orchestrator.broker import EventBroker
from app.orchestrator.cancellation import CancellationRegistry
from app.orchestrator.orchestrator import Orchestrator

logger = get_logger(__name__)


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


def get_event_broker(container: ContainerDep) -> EventBroker:
    """Return the shared event broker backing SSE streams."""
    return container.event_broker


EventBrokerDep = Annotated[EventBroker, Depends(get_event_broker)]


def get_cancellation_registry(container: ContainerDep) -> CancellationRegistry:
    """Return the shared cancellation registry backing cancel requests."""
    return container.cancellations


CancellationRegistryDep = Annotated[
    CancellationRegistry,
    Depends(get_cancellation_registry),
]


def get_orchestrator(container: ContainerDep) -> Orchestrator:
    """Return the shared orchestrator, or 503 when the LLM is unconfigured."""
    try:
        return container.orchestrator()
    except Exception as exc:
        logger.error("llm_configuration_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail=("LLM is not configured; set LLM_PROVIDER, LLM_API_KEY, and/or LLM_BASE_URL"),
        ) from exc


OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
