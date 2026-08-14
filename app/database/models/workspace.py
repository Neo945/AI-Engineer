"""Workspace model: a user-owned Git repository binding."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.memory import MemoryEntry
    from app.database.models.session import Session
    from app.database.models.user import User


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A repository a user works on.

    Attributes:
        owner_id: Owning user.
        name: Display name for the repository.
        repo_url: Upstream URL, if any.
        repo_path: Checkout path inside the executor sandbox.
        default_branch: Branch used for fresh checkouts.
    """

    __tablename__ = "workspaces"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    repo_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    repo_path: Mapped[str] = mapped_column(String(500), default="")
    default_branch: Mapped[str] = mapped_column(String(255), default="main")

    owner: Mapped[User] = relationship("User", back_populates="workspaces", lazy="joined")
    sessions: Mapped[list[Session]] = relationship(
        "Session",
        back_populates="workspace",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    memory_entries: Mapped[list[MemoryEntry]] = relationship(
        "MemoryEntry",
        back_populates="workspace",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
