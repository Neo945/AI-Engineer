"""OpenAI and OpenAI-compatible local backend adapter.

Implements the :class:`LLMProvider` protocol using the OpenAI Chat Completions
API, which is also the de-facto standard wire format for local backends such
as vLLM and Ollama: point :attr:`OpenAIClient.base_url` at the local server
and everything else works unchanged.

System content is emitted as a leading ``system`` message, matching the
OpenAI convention.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal, cast

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from openai.types.completion_usage import CompletionUsage

from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider, LLMResponse, LLMStreamEvent, LLMUsage
from app.tools.schemas import ToolSpec

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "max_tokens",
}

_ASSISTANT_ROLE: Literal["assistant"] = "assistant"

#: The OpenAI SDK raises at construction when no API key is configured.
#: OpenAI-compatible local backends (vLLM, Ollama) ignore the auth header, so a
#: placeholder is enough when an explicit base URL is set.
_LOCAL_API_KEY = "local"


def _to_openai_messages(
    messages: Sequence[ChatMessage], *, system: str | None
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})
    for message in messages:
        if message.role == ChatRole.SYSTEM:
            converted.append({"role": "system", "content": message.content})
        elif message.role == ChatRole.USER:
            converted.append({"role": "user", "content": message.content})
        elif message.role == ChatRole.TOOL:
            assert message.tool_call_id is not None
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        else:
            tool_calls: list[dict[str, Any]] | None = None
            if message.tool_requests:
                tool_calls = [
                    {
                        "id": request.id,
                        "type": "function",
                        "function": {
                            "name": request.name,
                            "arguments": json.dumps(request.arguments),
                        },
                    }
                    for request in message.tool_requests
                ]
            converted.append(
                {
                    "role": _ASSISTANT_ROLE,
                    "content": message.content or None,
                    "tool_calls": tool_calls,
                }
            )
    return converted


def _to_openai_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(spec.name),
                "description": spec.description,
                "parameters": spec.arguments_schema,
            },
        }
        for spec in tools
    ]


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_response(response: ChatCompletion) -> LLMResponse:
    choice = response.choices[0]
    message = choice.message
    tool_requests: list[ToolRequest] = []
    for call in message.tool_calls or []:
        if call.type != "function":
            continue
        tool_requests.append(
            ToolRequest(
                id=call.id,
                name=call.function.name,
                arguments=_parse_arguments(call.function.arguments or "{}"),
            )
        )
    usage = response.usage
    return LLMResponse(
        content=message.content or "",
        tool_requests=tool_requests,
        stop_reason=_FINISH_REASON_MAP.get(choice.finish_reason or "", "end_turn"),
        usage=LLMUsage(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        ),
        model=response.model,
    )


class OpenAIClient(LLMProvider):
    """Adapter for the OpenAI Chat Completions API.

    Attributes:
        name: Provider identifier (``openai``).
        model: Model name sent with every request.
        base_url: Optional base URL; set this to route to an OpenAI-compatible
            local backend (vLLM, Ollama).
    """

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        resolved_key = api_key
        if resolved_key is None:
            resolved_key = os.environ.get("OPENAI_API_KEY")
        if resolved_key is None and base_url is not None:
            resolved_key = _LOCAL_API_KEY
        self._client = AsyncOpenAI(
            api_key=resolved_key,
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
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=cast(
                list[ChatCompletionMessageParam],
                _to_openai_messages(messages, system=system),
            ),
            tools=cast(list[ChatCompletionToolParam], _to_openai_tools(tools)),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _parse_response(response)

    async def close(self) -> None:
        """Close the underlying HTTP client, releasing its connection pool."""
        await self._client.close()

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[LLMStreamEvent]:
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=cast(
                list[ChatCompletionMessageParam],
                _to_openai_messages(messages, system=system),
            ),
            tools=cast(list[ChatCompletionToolParam], _to_openai_tools(tools)),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        usage: CompletionUsage | None = None
        model_name: str = self.model
        slots: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
            model_name = chunk.model or model_name
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield LLMStreamEvent(type="text", text=delta.content)
            for call in delta.tool_calls or []:
                slot = slots.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                if call.id:
                    slot["id"] = call.id
                if call.function is None:
                    continue
                if call.function.name:
                    slot["name"] = call.function.name
                if call.function.arguments:
                    slot["arguments"] += call.function.arguments
        for slot in sorted(slots.values(), key=lambda item: item["id"]):
            yield LLMStreamEvent(
                type="tool_request",
                tool_request=ToolRequest(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=_parse_arguments(slot["arguments"]),
                ),
            )
        yield LLMStreamEvent(
            type="usage",
            usage=LLMUsage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            ),
            model=model_name,
        )
