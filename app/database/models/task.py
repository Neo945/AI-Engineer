"""Task model: a single unit of agent work within a session."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.enums import TaskStatus, native_enum
from app.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.session import Session


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A unit of agent work in the orchestrator pipeline.

    Tasks form a tree via ``parent_task_id`` so a planner task can own the
    coder/reviewer/tester subtasks it spawns. Token counts enable per-task
    cost accounting.
    """

    __tablename__ = "tasks"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[TaskStatus] = mapped_column(
        native_enum(TaskStatus, "task_status"),
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )
    goal: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[Session] = relationship("Session", back_populates="tasks")
    parent: Mapped[Task | None] = relationship(
        "Task",
        remote_side="Task.id",
        back_populates="children",
    )
    children: Mapped[list[Task]] = relationship(
        "Task",
        back_populates="parent",
        lazy="selectin",
    )
