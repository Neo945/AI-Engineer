"""CodeChunk model: retrieval-scaffolded vector storage.

This table backs the retrieval module (Phase 9). Each row is a slice of a
source file with an optional pgvector embedding. The schema is deliberately
minimal; chunking/embedding policy lands with the indexer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.mixins import UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.workspace import Workspace


class CodeChunk(UUIDPrimaryKeyMixin, Base):
    """A chunk of indexed source code with an optional embedding."""

    __tablename__ = "code_chunks"
    __table_args__ = (Index("ix_code_chunks_workspace_file", "workspace_id", "file_path"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
    )
    file_path: Mapped[str] = mapped_column(String(2000))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    workspace: Mapped[Workspace] = relationship("Workspace", lazy="joined")
