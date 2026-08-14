"""ORM models.

Importing this module registers every model on ``Base.metadata``, which is
required for Alembic autogenerate and any ``create_all`` usage.
"""

from __future__ import annotations

from app.database.models.code_chunk import CodeChunk
from app.database.models.enums import (
    MemoryKind,
    MessageRole,
    SessionStatus,
    TaskStatus,
    native_enum,
)
from app.database.models.memory import MemoryEntry
from app.database.models.message import Message
from app.database.models.session import Session
from app.database.models.task import Task
from app.database.models.user import User
from app.database.models.workspace import Workspace

__all__ = [
    "CodeChunk",
    "MemoryEntry",
    "MemoryKind",
    "Message",
    "MessageRole",
    "Session",
    "SessionStatus",
    "Task",
    "TaskStatus",
    "User",
    "Workspace",
    "native_enum",
]
