"""Pydantic request/response schemas for the gateway."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.database.models.enums import TaskStatus


class TaskCreateRequest(BaseModel):
    """Body for creating and running a task."""

    goal: str = Field(min_length=1, max_length=4000)
    agent_type: str = Field(default="coder", min_length=1, max_length=50)


class TaskResponse(BaseModel):
    """Serialized task state, read from an ORM task."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    parent_task_id: uuid.UUID | None
    agent_type: str
    status: TaskStatus
    goal: str
    result: str | None
    error: str | None
    attempt: int
    max_attempts: int
    input_tokens: int
    output_tokens: int
    plan: dict[str, Any] | None
    plan_needs_approval: bool
    plan_approved: bool | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    """Serialized transcript message, read from an ORM message."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    task_id: uuid.UUID | None
    role: str
    content: str
    ordinal: int
    tool_call_id: str | None
    tool_calls: list[dict[str, Any]] | None
    token_count: int
    created_at: datetime


class TaskDetailResponse(TaskResponse):
    """A task together with its persisted transcript."""

    messages: list[MessageResponse] = Field(default_factory=list)
