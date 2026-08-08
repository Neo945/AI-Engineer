"""Task run, listing, and detail endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database.models.session import Session
from app.database.models.task import Task
from app.database.repositories.message import MessageRepository
from app.database.repositories.session import SessionRepository
from app.database.repositories.task import TaskRepository
from app.gateway.dependencies import OrchestratorDep, SessionDep
from app.gateway.schemas import (
    MessageResponse,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskResponse,
)

router = APIRouter(tags=["tasks"])


async def get_session_or_404(session_id: uuid.UUID, db: SessionDep) -> Session:
    """Resolve a session by id, or 404.

    Declared before the orchestrator dependency so a missing session wins
    over an unconfigured LLM.
    """
    session = await SessionRepository(db).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


SessionOr404Dep = Annotated[Session, Depends(get_session_or_404)]


@router.post(
    "/sessions/{session_id}/tasks",
    response_model=TaskResponse,
    status_code=201,
)
async def create_and_run_task(
    session_id: uuid.UUID,
    body: TaskCreateRequest,
    db: SessionDep,
    session: SessionOr404Dep,
    orchestrator: OrchestratorDep,
) -> Task:
    """Create a task for a session and run it to completion inline.

    The request blocks until the agent finishes; clients can poll
    ``GET /tasks/{task_id}`` afterwards for the persisted transcript.
    """
    task = await TaskRepository(db).add(
        Task(session_id=session_id, agent_type=body.agent_type, goal=body.goal)
    )
    await db.commit()

    await orchestrator.run_task(task.id)
    await db.refresh(task)
    return task


@router.get("/sessions/{session_id}/tasks", response_model=list[TaskResponse])
async def list_tasks(
    session_id: uuid.UUID,
    db: SessionDep,
    session: SessionOr404Dep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Sequence[Task]:
    """List a session's tasks, oldest first."""
    return await TaskRepository(db).list_by_session(session_id, limit=limit, offset=offset)


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task_detail(
    task_id: uuid.UUID,
    db: SessionDep,
) -> TaskDetailResponse:
    """Return a task with its persisted transcript."""
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    messages = await MessageRepository(db).list_by_task(task_id)
    return TaskDetailResponse(
        **TaskResponse.model_validate(task).model_dump(),
        messages=[MessageResponse.model_validate(message) for message in messages],
    )
