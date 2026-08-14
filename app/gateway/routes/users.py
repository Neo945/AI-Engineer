"""User-scoped workspace and session CRUD endpoints."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Query

from app.database.models.session import Session
from app.database.models.workspace import Workspace
from app.database.repositories.session import SessionRepository
from app.database.repositories.workspace import WorkspaceRepository
from app.gateway.dependencies import (
    CurrentUserDep,
    OwnedSessionDep,
    OwnedWorkspaceDep,
    SessionDep,
)
from app.gateway.schemas import (
    SessionCreateRequest,
    SessionResponse,
    WorkspaceCreateRequest,
    WorkspaceResponse,
)

router = APIRouter(tags=["workspaces"])


@router.get("/workspaces", response_model=list[WorkspaceResponse])
async def list_workspaces(
    current_user: CurrentUserDep,
    db: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Sequence[Workspace]:
    """List the current user's workspaces, newest first."""
    return await WorkspaceRepository(db).list_by_owner(current_user.id, limit=limit, offset=offset)


@router.post("/workspaces", response_model=WorkspaceResponse, status_code=201)
async def create_workspace(
    body: WorkspaceCreateRequest,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> Workspace:
    """Create a workspace owned by the current user."""
    workspace = Workspace(owner_id=current_user.id, **body.model_dump())
    await WorkspaceRepository(db).add(workspace)
    await db.commit()
    return workspace


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace: OwnedWorkspaceDep,
) -> Workspace:
    """Return one of the current user's workspaces."""
    return workspace


@router.get("/workspaces/{workspace_id}/sessions", response_model=list[SessionResponse])
async def list_sessions(
    workspace: OwnedWorkspaceDep,
    db: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Sequence[Session]:
    """List sessions in one of the current user's workspaces, newest first."""
    return await SessionRepository(db).list_by_workspace(workspace.id, limit=limit, offset=offset)


@router.post("/workspaces/{workspace_id}/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    workspace: OwnedWorkspaceDep,
    body: SessionCreateRequest,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> Session:
    """Create a session inside one of the current user's workspaces."""
    session = Session(
        workspace_id=workspace.id,
        user_id=current_user.id,
        **body.model_dump(),
    )
    await SessionRepository(db).add(session)
    await db.commit()
    return session


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session: OwnedSessionDep,
) -> Session:
    """Return one of the current user's sessions."""
    return session
