"""Google Gemini adapter implementing the :class:`LLMProvider` protocol.

Uses the official ``google-genai`` SDK's ``generate_content`` API. Gemini keeps
the system prompt in a dedicated ``system_instruction`` field (it does not
accept a ``system`` role inside ``contents``), so any ``SYSTEM`` role entries
in the message list are ignored; callers should pass the system prompt via the
``system`` argument.

Gemini names the assistant turn ``model`` and represents tool calls/results as
``function_call``/``function_response`` parts rather than dedicated roles, so
this adapter translates to and from those shapes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from google import genai
from google.genai import types

from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider, LLMResponse, LLMStreamEvent, LLMUsage
from app.tools.schemas import ToolSpec

_STOP_REASON_MAP = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
}

#: Gemini represents the assistant turn with the literal role ``model``.
_MODEL_ROLE = "model"
_USER_ROLE = "user"


def _tool_names_by_id(messages: Sequence[ChatMessage]) -> dict[str, str]:
    """Map tool-call ids to names so tool results can name their function."""
    names: dict[str, str] = {}
    for message in messages:
        for request in message.tool_requests:
            names[request.id] = request.name
    return names


def _synthesize_call_id(index: int) -> str:
    """Build a stable id for a function call the provider did not id."""
    return f"call_{index}"


def _to_gemini_contents(messages: Sequence[ChatMessage]) -> list[types.Content]:
    """Convert normalized messages into Gemini ``Content`` turns."""
    contents: list[types.Content] = []
    tool_names = _tool_names_by_id(messages)
    for message in messages:
        if message.role == ChatRole.SYSTEM:
            continue
        if message.role == ChatRole.TOOL:
            assert message.tool_call_id is not None
            name = tool_names.get(message.tool_call_id, message.tool_call_id)
            contents.append(
                types.Content(
                    role=_USER_ROLE,
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=name,
                                id=message.tool_call_id,
                                response={"result": message.content},
                            )
                        )
                    ],
                )
            )
            continue
        parts: list[types.Part] = []
        if message.content:
            parts.append(types.Part(text=message.content))
        for request in message.tool_requests:
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=request.id, name=request.name, args=request.arguments
                    )
                )
            )
        if not parts:
            continue
        role = _MODEL_ROLE if message.role == ChatRole.ASSISTANT else _USER_ROLE
        contents.append(types.Content(role=role, parts=parts))
    return contents


def _to_gemini_tools(tools: Sequence[ToolSpec]) -> list[types.Tool]:
    """Convert tool specs into a single Gemini ``Tool`` with declarations."""
    if not tools:
        return []
    declarations = [
        types.FunctionDeclaration(
            name=str(spec.name),
            description=spec.description,
            parameters_json_schema=spec.arguments_schema,
        )
        for spec in tools
    ]
    return [types.Tool(function_declarations=declarations)]


def _finish_reason(value: Any) -> str:
    name = getattr(value, "name", None) or str(value).split(".")[-1]
    return _STOP_REASON_MAP.get(name, "end_turn")


def _requests_from_parts(parts: Sequence[types.Part], start_index: int) -> list[ToolRequest]:
    requests: list[ToolRequest] = []
    index = start_index
    for part in parts:
        call = part.function_call
        if call is None:
            continue
        requests.append(
            ToolRequest(
                id=call.id or _synthesize_call_id(index),
                name=call.name or "",
                arguments=dict(call.args or {}),
            )
        )
        index += 1
    return requests


def _usage_from(usage: types.GenerateContentResponseUsageMetadata | None) -> LLMUsage:
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=usage.prompt_token_count or 0,
        output_tokens=(usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
    )


def _parse_response(
    response: types.GenerateContentResponse, *, requested_model: str
) -> LLMResponse:
    text_parts: list[str] = []
    tool_requests: list[ToolRequest] = []
    finish = "end_turn"
    if response.candidates:
        candidate = response.candidates[0]
        content = candidate.content
        parts = content.parts if content and content.parts else []
        for part in parts:
            if part.text:
                text_parts.append(part.text)
        tool_requests = _requests_from_parts(parts, 0)
        finish = _finish_reason(candidate.finish_reason)
    return LLMResponse(
        content="".join(text_parts),
        tool_requests=tool_requests,
        stop_reason="tool_use" if tool_requests else finish,
        usage=_usage_from(response.usage_metadata),
        model=response.model_version or requested_model,
    )


class GeminiClient(LLMProvider):
    """Adapter for Google's Gemini ``generate_content`` API.

    Attributes:
        name: Provider identifier (``gemini``).
        model: Model name sent with every request.
        base_url: Optional base URL; set this to route to a compatible endpoint.
    """

    name = "gemini"

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
            resolved_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._api_key = resolved_key
        http_options = types.HttpOptions(base_url=base_url, timeout=int(timeout_seconds * 1000))
        self._http_options = http_options
        #: Built lazily: the SDK rejects a missing key at construction, but we
        #: want this adapter to construct in contexts that never issue a call.
        self._client: genai.Client | None = None

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key, http_options=self._http_options)
        return self._client

    def _config(
        self,
        *,
        tools: Sequence[ToolSpec],
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system is not None:
            kwargs["system_instruction"] = system
        gemini_tools = _to_gemini_tools(tools)
        if gemini_tools:
            kwargs["tools"] = gemini_tools
        return types.GenerateContentConfig(**kwargs)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        client = self._ensure_client()
        response = await client.aio.models.generate_content(
            model=self.model,
            contents=_to_gemini_contents(messages),
            config=self._config(
                tools=tools, system=system, max_tokens=max_tokens, temperature=temperature
            ),
        )
        return _parse_response(response, requested_model=self.model)

    async def close(self) -> None:
        """Close the underlying client, releasing its connection pool."""
        if self._client is not None:
            self._client.close()

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[LLMStreamEvent]:
        client = self._ensure_client()
        stream = await client.aio.models.generate_content_stream(
            model=self.model,
            contents=_to_gemini_contents(messages),
            config=self._config(
                tools=tools, system=system, max_tokens=max_tokens, temperature=temperature
            ),
        )
        usage: types.GenerateContentResponseUsageMetadata | None = None
        model_name: str = self.model
        index = 0
        async for chunk in stream:
            if chunk.model_version:
                model_name = chunk.model_version
            if chunk.usage_metadata is not None:
                usage = chunk.usage_metadata
            for candidate in chunk.candidates or []:
                content = candidate.content
                parts = content.parts if content and content.parts else []
                for part in parts:
                    if part.text:
                        yield LLMStreamEvent(type="text", text=part.text)
                for request in _requests_from_parts(parts, index):
                    index += 1
                    yield LLMStreamEvent(type="tool_request", tool_request=request)
        yield LLMStreamEvent(type="usage", usage=_usage_from(usage), model=model_name)
