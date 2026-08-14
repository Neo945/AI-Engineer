"""Pydantic request/response schemas for the gateway."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models.enums import SessionStatus, TaskStatus

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserResponse(BaseModel):
    """Serialized user, read from an ORM user."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime


class RegisterRequest(BaseModel):
    """Body for creating an account."""

    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=120)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _EMAIL_PATTERN.fullmatch(normalized):
            raise ValueError("email must be a valid address")
        return normalized


class LoginRequest(BaseModel):
    """Body for exchanging credentials for an access token."""

    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    """Body for changing the authenticated user's password."""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    """A signed access token plus the user it belongs to."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class WorkspaceCreateRequest(BaseModel):
    """Body for creating a workspace."""

    name: str = Field(min_length=1, max_length=120)
    repo_url: str | None = Field(default=None, max_length=2000)
    repo_path: str = Field(default="", max_length=500)
    default_branch: str = Field(default="main", max_length=255)


class WorkspaceResponse(BaseModel):
    """Serialized workspace, read from an ORM workspace."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    repo_url: str | None
    repo_path: str
    default_branch: str
    created_at: datetime
    updated_at: datetime


class SessionCreateRequest(BaseModel):
    """Body for creating a session inside a workspace."""

    title: str = Field(default="New session", max_length=255)
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    """Serialized session, read from an ORM session."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    title: str
    status: SessionStatus
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


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
