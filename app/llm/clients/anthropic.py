"""Anthropic Claude adapter implementing the :class:`LLMProvider` protocol.

Uses the official ``anthropic`` SDK's Messages API. System content is passed
through the top-level ``system`` parameter (Anthropic does not allow a
``system`` role inside ``messages``); any ``SYSTEM`` role entries in the
message list are ignored, so callers should pass the system prompt as the
``system`` argument.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from anthropic import AsyncAnthropic
from anthropic.lib.streaming._types import TextEvent
from anthropic.types import (
    Message,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider, LLMResponse, LLMStreamEvent, LLMUsage
from app.tools.schemas import ToolSpec

_STOP_REASON_MAP = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
}


def _to_anthropic_messages(messages: Sequence[ChatMessage]) -> list[MessageParam]:
    converted: list[MessageParam] = []
    for message in messages:
        if message.role == ChatRole.SYSTEM:
            continue
        if message.role == ChatRole.TOOL:
            assert message.tool_call_id is not None
            converted.append(
                {
                    "role": "user",
                    "content": [
                        ToolResultBlockParam(
                            type="tool_result",
                            tool_use_id=message.tool_call_id,
                            content=message.content,
                        )
                    ],
                }
            )
            continue
        blocks: list[TextBlockParam | ToolUseBlockParam] = []
        if message.content:
            blocks.append(TextBlockParam(type="text", text=message.content))
        for request in message.tool_requests:
            blocks.append(
                ToolUseBlockParam(
                    type="tool_use",
                    id=request.id,
                    name=request.name,
                    input=request.arguments,
                )
            )
        role: Literal["user", "assistant"] = (
            "assistant" if message.role == ChatRole.ASSISTANT else "user"
        )
        converted.append({"role": role, "content": blocks})
    return converted


def _to_anthropic_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(spec.name),
            "description": spec.description,
            "input_schema": spec.arguments_schema,
        }
        for spec in tools
    ]


def _parse_response(message: Message, *, requested_model: str) -> LLMResponse:
    text_parts: list[str] = []
    tool_requests: list[ToolRequest] = []
    for block in message.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_requests.append(
                ToolRequest(id=block.id, name=block.name, arguments=dict(block.input))
            )
    return LLMResponse(
        content="".join(text_parts),
        tool_requests=tool_requests,
        stop_reason=_STOP_REASON_MAP.get(message.stop_reason or "", "end_turn"),
        usage=LLMUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        ),
        model=message.model or requested_model,
    )


class AnthropicClient(LLMProvider):
    """Adapter for Anthropic's Messages API (Claude models).

    Attributes:
        name: Provider identifier (``anthropic``).
        model: Model name sent with every request.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.model = model
        self._client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=2,
        )

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _to_anthropic_messages(messages),
            "tools": _to_anthropic_tools(tools),
        }
        if system is not None:
            kwargs["system"] = system
        response = await self._client.messages.create(**kwargs)
        return _parse_response(response, requested_model=self.model)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[LLMStreamEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _to_anthropic_messages(messages),
            "tools": _to_anthropic_tools(tools),
        }
        if system is not None:
            kwargs["system"] = system
        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if isinstance(event, TextEvent):
                    yield LLMStreamEvent(type="text", text=event.text)
            parsed = _parse_response(await stream.get_final_message(), requested_model=self.model)
        for request in parsed.tool_requests:
            yield LLMStreamEvent(type="tool_request", tool_request=request)
        yield LLMStreamEvent(type="usage", usage=parsed.usage, model=parsed.model)
