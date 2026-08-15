"""Provider protocol shared by every LLM adapter.

Agents and the orchestrator depend only on the :class:`LLMProvider` protocol
(and the value types in this module), never on a specific vendor. Concrete
adapters live in :mod:`app.llm.clients`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.llm.messages import ChatMessage, ToolRequest
from app.tools.schemas import ToolSpec


class LLMUsage(BaseModel):
    """Token accounting for a single request."""

    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    """A completed (non-streamed) model reply.

    Attributes:
        content: Text produced by the model.
        tool_requests: Tools the model wants invoked, if any.
        stop_reason: Why generation stopped. One of ``end_turn``, ``tool_use``,
            ``max_tokens``.
        usage: Token counts for the request.
        model: Model identifier that served the request.
    """

    content: str = ""
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: LLMUsage = Field(default_factory=LLMUsage)
    model: str = ""


class LLMStreamEvent(BaseModel):
    """One event in a streamed reply.

    ``text`` events carry incremental content deltas; a ``tool_request`` event
    is emitted per completed tool request; a final ``usage`` event reports
    token accounting and the serving model.
    """

    type: Literal["text", "tool_request", "usage"]
    text: str = ""
    tool_request: ToolRequest | None = None
    usage: LLMUsage | None = None
    model: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    """Async interface implemented by every provider adapter."""

    name: str
    model: str

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse: ...

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[LLMStreamEvent]: ...

    async def close(self) -> None:
        """Release any transport resources held by the provider.

        Idempotent: safe to call more than once and on providers with no
        external resources (such as test fakes).
        """
        ...
