"""Session model: a persistent conversation/agent context."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.enums import SessionStatus, native_enum
from app.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.task import Task
    from app.database.models.user import User
    from app.database.models.workspace import Workspace


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A durable conversation context bound to a workspace.

    Holds the live status of the agent run and a JSONB ``meta`` bag for
    orchestration state (model config, context pointers) that does not
    warrant its own column.
    """

    __tablename__ = "sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), default="New session")
    status: Mapped[SessionStatus] = mapped_column(
        native_enum(SessionStatus, "session_status"),
        default=SessionStatus.IDLE,
        server_default=SessionStatus.IDLE.value,
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    workspace: Mapped[Workspace] = relationship("Workspace", back_populates="sessions")
    user: Mapped[User] = relationship("User")
    tasks: Mapped[list[Task]] = relationship(
        "Task",
        back_populates="session",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
