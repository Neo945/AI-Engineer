"""Typed contracts for tool calls and results.

These Pydantic models travel between the orchestrator (which plans tool
calls) and the executor (which runs them). They are deliberately free of any
I/O so they can be serialized, validated, and unit-tested in isolation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ToolName(StrEnum):
    """Every tool the agent can invoke.

    Member values are the wire/LLM identifiers; member names are Python-side
    and may be renamed freely without breaking persisted tool calls.
    """

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_LIST = "file_list"
    FILE_SEARCH = "file_search"
    FILE_DELETE = "file_delete"
    FILE_MOVE = "file_move"
    TERMINAL_RUN = "terminal_run"
    GIT_STATUS = "git_status"
    GIT_DIFF = "git_diff"
    GIT_COMMIT = "git_commit"


class ToolCall(BaseModel):
    """A single tool invocation planned by an agent.

    Attributes:
        id: Unique identifier echoed back in the matching result.
        tool: Which tool to invoke.
        arguments: Keyword arguments for the tool. Validated by the registry
            against the tool's arguments schema before dispatch.
        timeout_ms: Optional per-call override of the executor's default
            timeout.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    tool: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int | None = Field(default=None, ge=100, le=3_600_000)


class ToolResult(BaseModel):
    """Outcome of a tool invocation.

    Attributes:
        call_id: Matches the originating :class:`ToolCall` id.
        tool: The tool that produced this result.
        ok: Whether the invocation succeeded.
        output: Human-readable result text for the agent transcript.
        error: Machine-readable error detail when ``ok`` is false.
        exit_code: Process exit code for terminal/git tools, if any.
        duration_ms: Wall-clock time spent executing the tool.
        truncated: Whether ``output`` was cut off by a size limit.
    """

    call_id: str
    tool: ToolName
    ok: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    truncated: bool = False


class ToolSpec(BaseModel):
    """LLM-facing description of a tool.

    ``arguments_schema`` is a JSON Schema (draft 2020-12) describing the
    accepted arguments, suitable for provider tool-calling APIs.
    """

    name: ToolName
    description: str
    arguments_schema: dict[str, Any]
