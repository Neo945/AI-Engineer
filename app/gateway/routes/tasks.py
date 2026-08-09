"""Task run, listing, detail, and event-stream endpoints."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.database.models.enums import TaskStatus
from app.database.models.message import Message
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.repositories.message import MessageRepository
from app.database.repositories.session import SessionRepository
from app.database.repositories.task import TaskRepository
from app.gateway.dependencies import (
    CancellationRegistryDep,
    ContainerDep,
    EventBrokerDep,
    OrchestratorDep,
    SessionDep,
)
from app.gateway.schemas import (
    MessageResponse,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskResponse,
)

router = APIRouter(tags=["tasks"])

_SSE_KEEPALIVE_SECONDS = 15.0

_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


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
    status_code=202,
)
async def create_and_run_task(
    session_id: uuid.UUID,
    body: TaskCreateRequest,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    session: SessionOr404Dep,
    orchestrator: OrchestratorDep,
    container: ContainerDep,
) -> Task:
    """Create a task for a session and kick off its run in the background.

    The response is returned as soon as the task is created (status
    ``pending``); the agent then executes asynchronously. Clients can watch
    progress on ``GET /sessions/{session_id}/tasks/{task_id}/events`` (SSE)
    or poll ``GET /tasks/{task_id}`` for the persisted transcript. A task is
    retryable up to ``task_max_attempts`` times.
    """
    task = await TaskRepository(db).add(
        Task(
            session_id=session_id,
            agent_type=body.agent_type,
            goal=body.goal,
            max_attempts=container.settings.task_max_attempts,
        )
    )
    await db.commit()
    if task.plan_needs_approval and task.plan_approved is None:
        raise HTTPException(status_code=409, detail="task plan awaits approval")
    if task.plan_approved is False:
        raise HTTPException(status_code=409, detail="task plan was rejected")

    background_tasks.add_task(orchestrator.run_task, task.id)
    return task


@router.post(
    "/tasks/{task_id}/plan",
    response_model=TaskResponse,
    status_code=202,
)
async def plan_task(
    task_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    orchestrator: OrchestratorDep,
) -> Task:
    """Run the planner over a task in the background.

    The planner persists the plan artifact and returns the task to ``pending``;
    a plan that writes files or uses destructive operations then blocks
    execution until it is approved via ``POST /tasks/{task_id}/approve`` (or
    rejected via ``POST /tasks/{task_id}/reject``). Progress streams on the
    task's SSE events endpoint.
    """
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    background_tasks.add_task(orchestrator.plan_task, task_id)
    return task


@router.post("/tasks/{task_id}/approve", response_model=TaskResponse)
async def approve_task(
    task_id: uuid.UUID,
    db: SessionDep,
    orchestrator: OrchestratorDep,
) -> Task:
    """Approve a task's plan so it may run."""
    return await _decide_task(task_id, db, orchestrator, approve=True)


@router.post("/tasks/{task_id}/reject", response_model=TaskResponse)
async def reject_task(
    task_id: uuid.UUID,
    db: SessionDep,
    orchestrator: OrchestratorDep,
) -> Task:
    """Reject a task's plan, blocking execution until it is re-planned."""
    return await _decide_task(task_id, db, orchestrator, approve=False)


async def _decide_task(
    task_id: uuid.UUID,
    db: SessionDep,
    orchestrator: OrchestratorDep,
    *,
    approve: bool,
) -> Task:
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        if approve:
            await orchestrator.approve_task(task_id)
        else:
            await orchestrator.reject_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.post(
    "/tasks/{task_id}/retry",
    response_model=TaskResponse,
    status_code=202,
)
async def retry_task(
    task_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    orchestrator: OrchestratorDep,
) -> Task:
    """Reset a terminal task and schedule it to run again.

    The task must be finished (completed, failed, or cancelled) and must not
    have exhausted its attempt budget; otherwise the request is rejected with
    ``409``. The prior transcript is preserved and the new run appends to it.
    """
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        await orchestrator.retry_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(orchestrator.run_task, task_id)
    await db.refresh(task)
    return task


@router.post(
    "/tasks/{task_id}/run",
    response_model=TaskResponse,
    status_code=202,
)
async def run_task(
    task_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    orchestrator: OrchestratorDep,
) -> Task:
    """Schedule a task to run, usually after its plan is approved.

    A task whose plan awaits approval (or was rejected) is refused with
    ``409``. The run executes asynchronously; watch the task's SSE events
    endpoint or poll ``GET /tasks/{task_id}``.
    """
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.plan_needs_approval and task.plan_approved is None:
        raise HTTPException(status_code=409, detail="task plan awaits approval")
    if task.plan_approved is False:
        raise HTTPException(status_code=409, detail="task plan was rejected")
    background_tasks.add_task(orchestrator.run_task, task_id)
    return task


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: uuid.UUID,
    db: SessionDep,
    cancellations: CancellationRegistryDep,
) -> Task:
    """Cancel a pending or running task.

    The status is persisted as ``cancelled`` immediately (crash-safe), and a
    cooperative cancel is recorded so an in-flight run stops at its next step
    boundary and emits a ``cancelled`` event. Terminal tasks cannot be
    cancelled (``409``).
    """
    task = await TaskRepository(db).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status in _TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="task already finished")
    task.status = TaskStatus.CANCELLED
    task.finished_at = datetime.now(UTC)
    await db.commit()
    await cancellations.request_cancel(task_id)
    await db.refresh(task)
    return task


@router.get("/sessions/{session_id}/tasks/{task_id}/events")
async def stream_task_events(
    session_id: uuid.UUID,
    task_id: uuid.UUID,
    db: SessionDep,
    session: SessionOr404Dep,
    broker: EventBrokerDep,
) -> StreamingResponse:
    """Stream a task's progress as Server-Sent Events.

    The stream opens by replaying the current task snapshot and persisted
    transcript, then emits each new status transition and message live until
    the task reaches a terminal state, at which point the stream closes.
    DB state is the source of truth; broker events only wake the streamer so
    updates arrive promptly rather than on a poll interval.
    """
    task = await TaskRepository(db).get(task_id)
    if task is None or task.session_id != session_id:
        raise HTTPException(status_code=404, detail="task not found")

    queue = await broker.subscribe(task_id)

    async def _stream() -> AsyncIterator[str]:
        last_ordinal = -1
        seen_status: TaskStatus | None = None
        try:
            while True:
                current = await TaskRepository(db).get(task_id)
                messages = await MessageRepository(db).list_by_task(task_id)

                if current is not None and current.status != seen_status:
                    seen_status = current.status
                    yield _sse_frame("snapshot", _task_payload(current))

                for message in messages:
                    if message.ordinal <= last_ordinal:
                        continue
                    yield _sse_frame("message", _message_payload(message))
                    last_ordinal = message.ordinal

                if current is None or current.status in _TERMINAL_STATUSES:
                    if current is not None:
                        yield _sse_frame(current.status.value, _task_payload(current))
                    break

                await db.commit()
                try:
                    await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await broker.unsubscribe(task_id, queue)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _task_payload(task: Task) -> dict[str, Any]:
    return TaskResponse.model_validate(task).model_dump(mode="json")


def _message_payload(message: Message) -> dict[str, Any]:
    return MessageResponse.model_validate(message).model_dump(mode="json")


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
