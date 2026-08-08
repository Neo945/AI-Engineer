"""Repository implementations.

Repositories encapsulate persistence for each aggregate. Services depend on
these concrete classes (or interfaces in later phases) instead of SQL.
"""

from __future__ import annotations

from app.database.repositories.base import BaseRepository
from app.database.repositories.message import MessageRepository
from app.database.repositories.session import SessionRepository
from app.database.repositories.task import TaskRepository
from app.database.repositories.user import UserRepository
from app.database.repositories.workspace import WorkspaceRepository

__all__ = [
    "BaseRepository",
    "MessageRepository",
    "SessionRepository",
    "TaskRepository",
    "UserRepository",
    "WorkspaceRepository",
]
