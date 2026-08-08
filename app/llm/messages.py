"""Provider-agnostic chat message types.

These normalized types decouple the orchestrator and agents from any specific
LLM vendor's wire format. Provider adapters translate them to and from the
Anthropic, OpenAI, and OpenAI-compatible local backend message schemas.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ChatRole(StrEnum):
    """Canonical message roles shared by every provider.

    ``TOOL`` messages carry the result of a previously requested tool call and
    reference that call via :attr:`ChatMessage.tool_call_id`.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolRequest(BaseModel):
    """A tool invocation requested by the model.

    Attributes:
        id: Identifier referenced by the matching tool result. Providers
            assign their own ids; the orchestration layer echoes them back.
        name: Tool identifier as chosen by the model. Kept as ``str`` so a
            hallucinated name degrades gracefully instead of failing
            validation here; it is resolved to a known :class:`ToolName` when
            the tool call is dispatched.
        arguments: Parsed keyword arguments for the tool.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """A single normalized message in an LLM conversation.

    Attributes:
        role: Canonical role.
        content: Text payload. Empty for assistant messages that only carry
            tool requests and for user messages built purely from tool
            results (a convention per provider).
        tool_call_id: Identifier of the tool request this message answers;
            required when ``role`` is ``TOOL``.
        tool_requests: Tool requests issued by the model; only populated for
            ``ASSISTANT`` messages.
    """

    role: ChatRole
    content: str = ""
    tool_call_id: str | None = None
    tool_requests: list[ToolRequest] = Field(default_factory=list)
