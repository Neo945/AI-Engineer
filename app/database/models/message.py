"""Message model: a single entry in a conversation transcript."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.enums import MessageRole, native_enum
from app.database.models.mixins import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.session import Session
    from app.database.models.task import Task


class Message(UUIDPrimaryKeyMixin, Base):
    """An immutable entry in the conversation transcript.

    Messages are append-only: they are created once and never updated, so
    they only carry ``created_at`` and omit the timestamp mixin's
    ``updated_at``. ``ordinal`` records the message's position in the
    session transcript; timestamps alone cannot order a task's messages
    because a single task persists all of its messages in one flush.

    Tool interactions are persisted so the transcript can be faithfully
    rebuilt for an LLM context window: assistant messages that requested tools
    carry the requests in ``tool_calls``, and ``TOOL`` role messages reference
    the request they answer via ``tool_call_id``.
    """

    __tablename__ = "messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(native_enum(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, server_default="0", index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    session: Mapped[Session] = relationship("Session", lazy="joined")
    task: Mapped[Task | None] = relationship("Task", lazy="joined")
