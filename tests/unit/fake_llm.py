"""A scriptable :class:`LLMProvider` fake for unit and integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.llm.messages import ChatMessage
from app.llm.protocol import LLMProvider, LLMResponse, LLMStreamEvent
from app.tools.schemas import ToolSpec


class FakeLLM(LLMProvider):
    """Returns scripted responses in order, then a default final answer.

    Attributes:
        name: Provider identifier.
        model: Model identifier reported in responses.
        calls: Every ``complete`` invocation, capturing the received messages,
            tools, system prompt, and limits for assertions.
    """

    name = "fake"
    model = "fake-model"

    def __init__(self, script: Sequence[LLMResponse] = ()) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._script:
            return self._script.pop(0)
        return LLMResponse(content="Done.", model=self.model)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[LLMStreamEvent]:
        response = await self.complete(
            messages, tools=tools, system=system, max_tokens=max_tokens, temperature=temperature
        )
        if response.content:
            yield LLMStreamEvent(type="text", text=response.content)
        for request in response.tool_requests:
            yield LLMStreamEvent(type="tool_request", tool_request=request)
        yield LLMStreamEvent(type="usage", usage=response.usage, model=response.model)
