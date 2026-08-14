"""MemoryEntry model: durable session/project/decision memory.

The memory subsystem (Phase 10) persists what the agent has learned across
sessions: durable repository facts, design decisions, user preferences, and
high-signal conversations. Entries are workspace-scoped so each checkout sees
exactly the knowledge its own sessions accumulated. Embeddings are optional
and offline-first — keyword recall works immediately, and pgvector semantic
search can be layered on by backfilling the ``embedding`` column later.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.enums import MemoryKind, native_enum
from app.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.workspace import Workspace


class MemoryEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single piece of durable workspace memory.

    Attributes:
        workspace_id: The workspace the entry belongs to.
        kind: What the entry records (fact, decision, preference, conversation).
        content: The memory text.
        source: Where the memory came from (e.g. ``cli``, ``run``, ``review``).
        embedding: Optional pgvector embedding for semantic recall.
    """

    __tablename__ = "memory_entries"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[MemoryKind] = mapped_column(native_enum(MemoryKind, "memory_kind"))
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="cli")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

    workspace: Mapped[Workspace] = relationship("Workspace", back_populates="memory_entries")
