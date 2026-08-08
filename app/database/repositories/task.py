"""Persistence operations for tasks."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.database.models.session import Session
from app.database.models.task import Task
from app.database.repositories.base import BaseRepository


class TaskRepository(BaseRepository[Task, uuid.UUID]):
    """Task persistence and lookups."""

    model = Task

    async def get_for_run(
        self,
        task_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Task | None:
        """Fetch a task with its session and workspace eagerly loaded.

        Orchestration needs the workspace path to bind an executor, so the
        relationship chain is joined up front instead of lazily loaded (which
        is not safe in an async context). ``for_update`` takes a row lock so
        the status guard (terminal/running) is atomic against concurrent
        runs and cancels.
        """
        stmt = (
            select(Task)
            .where(Task.id == task_id)
            .options(joinedload(Task.session).joinedload(Session.workspace))
        )
        if for_update:
            stmt = stmt.with_for_update(of=Task)
        return (await self._session.scalars(stmt)).unique().one_or_none()

    async def list_by_session(
        self,
        session_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Task]:
        """List tasks for a session, oldest first."""
        stmt = (
            select(Task)
            .where(Task.session_id == session_id)
            .order_by(Task.created_at.asc(), Task.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_children(self, parent_task_id: uuid.UUID) -> Sequence[Task]:
        """List direct subtasks of a parent task, oldest first."""
        stmt = (
            select(Task)
            .where(Task.parent_task_id == parent_task_id)
            .order_by(Task.created_at.asc(), Task.id.asc())
        )
        return (await self._session.scalars(stmt)).all()
