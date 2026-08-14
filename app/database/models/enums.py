"""Domain enums for the persistence layer."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum


def _member_values(members: type[StrEnum]) -> list[str]:
    """Return the *values* of an enum class.

    SQLAlchemy stores enum member names by default; persisting values keeps
    database rows stable even if member identifiers are renamed later.
    """
    return [member.value for member in members]


def native_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Build a native PostgreSQL enum type storing member values.

    Args:
        enum_cls: The Python ``StrEnum`` to persist.
        name: The PostgreSQL type name.

    Returns:
        A SQLAlchemy ``Enum`` type instance.
    """
    return Enum(enum_cls, name=name, native_enum=True, values_callable=_member_values)


class SessionStatus(StrEnum):
    """Lifecycle of an agent session."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """Lifecycle of a single task within a session."""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    REVIEWING = "reviewing"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageRole(StrEnum):
    """Role of a message in a conversation transcript."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MemoryKind(StrEnum):
    """Scope of a durable memory entry.

    ``FACT`` captures durable repository facts; ``DECISION`` records why a
    design or tooling choice was made; ``PREFERENCE`` stores user working
    preferences; ``CONVERSATION`` keeps high-signal exchanges worth replaying
    to later sessions.
    """

    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    CONVERSATION = "conversation"
