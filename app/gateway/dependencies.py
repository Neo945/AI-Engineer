"""Request-scoped dependency providers for FastAPI routes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.container import Container
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.database.repositories.session import SessionRepository
from app.database.repositories.task import TaskRepository
from app.database.repositories.user import UserRepository
from app.database.repositories.workspace import WorkspaceRepository
from app.orchestrator.broker import EventBroker
from app.orchestrator.cancellation import CancellationRegistry
from app.orchestrator.orchestrator import Orchestrator

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


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


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    container: ContainerDep,
    db: SessionDep,
) -> User:
    """Resolve the authenticated user from the ``Authorization`` header.

    Missing, malformed, expired, or revoked tokens are all reported as a
    generic 401 so attackers cannot distinguish the reason from the outside.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_access_token(credentials.credentials, container.settings)
    if claims is None:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await UserRepository(db).get(claims.subject)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_owned_session_or_404(
    session_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> Session:
    """Resolve a session the current user owns, or 404/403."""
    session = await SessionRepository(db).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="session does not belong to you")
    return session


OwnedSessionDep = Annotated[Session, Depends(get_owned_session_or_404)]


async def get_owned_task_or_404(
    task_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> Task:
    """Resolve a task whose session the current user owns, or 404/403."""
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    session = await SessionRepository(db).get(task.session_id)
    if session is None or session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="task does not belong to you")
    return task


OwnedTaskDep = Annotated[Task, Depends(get_owned_task_or_404)]


async def get_owned_workspace_or_404(
    workspace_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> Workspace:
    """Resolve a workspace the current user owns, or 404/403."""
    workspace = await WorkspaceRepository(db).get(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if workspace.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="workspace does not belong to you")
    return workspace


OwnedWorkspaceDep = Annotated[Workspace, Depends(get_owned_workspace_or_404)]
