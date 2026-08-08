"""User model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.database.models.workspace import Workspace


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated user of the platform."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )

    workspaces: Mapped[list[Workspace]] = relationship(
        "Workspace",
        back_populates="owner",
        lazy="selectin",
    )
