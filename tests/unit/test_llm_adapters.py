"""Unit tests for the LLM provider adapter mappings.

The pure conversion functions (our normalized messages -> provider wire format
and provider responses -> our normalized responses) are exercised directly;
delegation tests stub out the SDK clients to verify the adapters wire the
conversions together correctly.
"""

from __future__ import annotations

from typing import Any, cast

from anthropic.lib.streaming._types import TextEvent
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from openai.types import CompletionUsage
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
)
from openai.types.chat.chat_completion import Choice as ChatCompletionChoice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)

from app.llm.clients.anthropic import (
    AnthropicClient,
    _to_anthropic_messages,
    _to_anthropic_tools,
)
from app.llm.clients.anthropic import (
    _parse_response as _parse_anthropic_response,
)
from app.llm.clients.openai import (
    OpenAIClient,
    _parse_arguments,
    _to_openai_messages,
    _to_openai_tools,
)
from app.llm.clients.openai import (
    _parse_response as _parse_openai_response,
)
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMStreamEvent
from app.tools.schemas import ToolName, ToolSpec


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage(role=ChatRole.SYSTEM, content="You are a coding agent."),
        ChatMessage(role=ChatRole.USER, content="Read x.py"),
        ChatMessage(
            role=ChatRole.ASSISTANT,
            content="",
            tool_requests=[ToolRequest(id="call_1", name="file_read", arguments={"path": "x.py"})],
        ),
        ChatMessage(role=ChatRole.TOOL, content="ok", tool_call_id="call_1"),
    ]


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name=ToolName.FILE_READ,
            description="Read a file",
            arguments_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        )
    ]


def _anthropic_message() -> Message:
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-haiku-4-5",
        content=[
            TextBlock(type="text", text="Let me check."),
            ToolUseBlock(type="tool_use", id="call_1", name="file_read", input={"path": "x.py"}),
        ],
        stop_reason="tool_use",
        usage=Usage(input_tokens=12, output_tokens=4),
    )


def _openai_completion() -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-1",
        choices=[
            ChatCompletionChoice(
                finish_reason="tool_calls",
                index=0,
                logprobs=None,
                message=ChatCompletionMessage(
                    content=None,
                    role="assistant",
                    tool_calls=[
                        ChatCompletionMessageFunctionToolCall(
                            id="call_1",
                            type="function",
                            function={
                                "name": "file_read",
                                "arguments": '{"path": "x.py"}',
                            },
                        )
                    ],
                ),
            )
        ],
        created=0,
        model="gpt-4o-mini",
        object="chat.completion",
        usage=CompletionUsage(completion_tokens=3, prompt_tokens=5, total_tokens=8),
    )


# --- Anthropic mapping ---


def test_anthropic_message_mapping_skips_system_and_builds_blocks() -> None:
    out = _to_anthropic_messages(_messages())
    assert len(out) == 3
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": "Read x.py"}]}
    assert out[1]["role"] == "assistant"
    content = cast(list[dict[str, Any]], out[1]["content"])
    assert content == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "file_read",
            "input": {"path": "x.py"},
        }
    ]
    assert out[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}],
    }


def test_anthropic_tools_mapping() -> None:
    assert _to_anthropic_tools(_tools()) == [
        {
            "name": "file_read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]


def test_anthropic_response_parsing() -> None:
    parsed = _parse_anthropic_response(_anthropic_message(), requested_model="fallback")
    assert parsed.content == "Let me check."
    assert parsed.stop_reason == "tool_use"
    assert parsed.model == "claude-haiku-4-5"
    assert parsed.usage.input_tokens == 12
    assert parsed.usage.output_tokens == 4
    assert len(parsed.tool_requests) == 1
    request = parsed.tool_requests[0]
    assert request.id == "call_1"
    assert request.name == "file_read"
    assert request.arguments == {"path": "x.py"}


async def test_anthropic_complete_delegates_and_parses() -> None:
    class _FakeMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> Message:
            self.calls.append(kwargs)
            return _anthropic_message()

    class _FakeAnthropic:
        def __init__(self) -> None:
            self.messages = _FakeMessages()

    client = AnthropicClient(model="claude-haiku-4-5", api_key="secret")
    fake = cast(Any, _FakeAnthropic())
    client._client = fake
    parsed = await client.complete(
        _messages(),
        tools=_tools(),
        system="You are a coding agent.",
        max_tokens=100,
        temperature=0.0,
    )
    first = fake.messages.calls[0]
    assert first["model"] == "claude-haiku-4-5"
    assert first["max_tokens"] == 100
    assert first["temperature"] == 0.0
    assert first["system"] == "You are a coding agent."
    assert len(first["messages"]) == 3
    assert parsed.tool_requests[0].name == "file_read"

    await client.complete(_messages(), tools=_tools(), system=None, max_tokens=10, temperature=0.0)
    assert "system" not in fake.messages.calls[1]


async def test_anthropic_stream_emits_text_tool_and_usage() -> None:
    class _FakeStream:
        def __init__(self) -> None:
            self._events = [TextEvent(type="text", text="Hello", snapshot="Hello")]

        async def __aenter__(self) -> _FakeStream:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        def __aiter__(self) -> _FakeStream:
            self._index = 0
            return self

        async def __anext__(self) -> TextEvent:
            if self._index >= len(self._events):
                raise StopAsyncIteration
            event = self._events[self._index]
            self._index += 1
            return event

        async def get_final_message(self) -> Message:
            return _anthropic_message()

    class _FakeMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def stream(self, **kwargs: Any) -> _FakeStream:
            self.calls.append(kwargs)
            return _FakeStream()

    class _FakeAnthropic:
        def __init__(self) -> None:
            self.messages = _FakeMessages()

    client = AnthropicClient(model="claude-haiku-4-5")
    fake = cast(Any, _FakeAnthropic())
    client._client = fake
    events = [
        event
        async for event in client.stream(
            _messages(), tools=_tools(), system="s", max_tokens=10, temperature=0.0
        )
    ]
    assert [event.type for event in events] == ["text", "tool_request", "usage"]
    assert events[0].text == "Hello"
    assert events[1].tool_request is not None
    assert events[1].tool_request.name == "file_read"
    assert events[2].usage is not None
    assert events[2].usage.input_tokens == 12
    assert events[2].model == "claude-haiku-4-5"
    assert fake.messages.calls[0]["system"] == "s"


# --- OpenAI mapping ---


def test_openai_message_mapping_prepends_system() -> None:
    messages = [
        ChatMessage(role=ChatRole.USER, content="Read x.py"),
        ChatMessage(
            role=ChatRole.ASSISTANT,
            content="",
            tool_requests=[ToolRequest(id="call_1", name="file_read", arguments={"path": "x.py"})],
        ),
        ChatMessage(role=ChatRole.TOOL, content="ok", tool_call_id="call_1"),
    ]
    out = _to_openai_messages(messages, system="You are a coding agent.")
    assert out[0] == {"role": "system", "content": "You are a coding agent."}
    assert out[1] == {"role": "user", "content": "Read x.py"}
    assert out[2]["role"] == "assistant"
    assert out[2]["content"] is None
    assert out[2]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_read", "arguments": '{"path": "x.py"}'},
        }
    ]
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}


def test_openai_message_mapping_passes_system_role_through() -> None:
    out = _to_openai_messages(
        [ChatMessage(role=ChatRole.SYSTEM, content="You are a coding agent.")],
        system=None,
    )
    assert out == [{"role": "system", "content": "You are a coding agent."}]


def test_openai_message_mapping_without_system() -> None:
    out = _to_openai_messages([ChatMessage(role=ChatRole.USER, content="hi")], system=None)
    assert out == [{"role": "user", "content": "hi"}]


def test_openai_tools_mapping() -> None:
    assert _to_openai_tools(_tools()) == [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]


def test_openai_argument_parsing() -> None:
    assert _parse_arguments('{"a": 1}') == {"a": 1}
    assert _parse_arguments("not json") == {}
    assert _parse_arguments("[1, 2]") == {}
    assert _parse_arguments("") == {}


def test_openai_response_parsing() -> None:
    parsed = _parse_openai_response(_openai_completion())
    assert parsed.content == ""
    assert parsed.stop_reason == "tool_use"
    assert parsed.model == "gpt-4o-mini"
    assert parsed.usage.input_tokens == 5
    assert parsed.usage.output_tokens == 3
    assert len(parsed.tool_requests) == 1
    request = parsed.tool_requests[0]
    assert request.id == "call_1"
    assert request.name == "file_read"
    assert request.arguments == {"path": "x.py"}


async def test_openai_complete_delegates_and_parses() -> None:
    class _FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> ChatCompletion:
            self.calls.append(kwargs)
            return _openai_completion()

    class _FakeOpenAI:
        def __init__(self) -> None:
            self.chat = type("_FakeChat", (), {"completions": _FakeCompletions()})()

    client = OpenAIClient(model="gpt-4o-mini", api_key="secret")
    fake = cast(Any, _FakeOpenAI())
    client._client = fake
    parsed = await client.complete(
        _messages(), tools=_tools(), system=None, max_tokens=50, temperature=0.0
    )
    first = fake.chat.completions.calls[0]
    assert first["model"] == "gpt-4o-mini"
    assert first["max_tokens"] == 50
    assert first["messages"][0]["role"] == "system"
    assert len(first["tools"]) == 1
    assert parsed.tool_requests[0].name == "file_read"


async def test_openai_stream_accumulates_tool_calls() -> None:
    chunks = [
        ChatCompletionChunk(
            id="c1",
            choices=[
                Choice(
                    finish_reason=None,
                    index=0,
                    logprobs=None,
                    delta=ChoiceDelta(content="Hel", role=None),
                )
            ],
            created=0,
            model="gpt-4o-mini",
            object="chat.completion.chunk",
        ),
        ChatCompletionChunk(
            id="c2",
            choices=[
                Choice(
                    finish_reason=None,
                    index=0,
                    logprobs=None,
                    delta=ChoiceDelta(content="lo", role=None),
                )
            ],
            created=0,
            model="gpt-4o-mini",
            object="chat.completion.chunk",
        ),
        ChatCompletionChunk(
            id="c3",
            choices=[
                Choice(
                    finish_reason=None,
                    index=0,
                    logprobs=None,
                    delta=ChoiceDelta(
                        content=None,
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_1",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    name="file_read", arguments=None
                                ),
                            )
                        ],
                    ),
                )
            ],
            created=0,
            model="gpt-4o-mini",
            object="chat.completion.chunk",
        ),
        ChatCompletionChunk(
            id="c4",
            choices=[
                Choice(
                    finish_reason=None,
                    index=0,
                    logprobs=None,
                    delta=ChoiceDelta(
                        content=None,
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id=None,
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    name=None, arguments='{"path": "x.py"}'
                                ),
                            )
                        ],
                    ),
                )
            ],
            created=0,
            model="gpt-4o-mini",
            object="chat.completion.chunk",
        ),
        ChatCompletionChunk(
            id="c5",
            choices=[
                Choice(
                    finish_reason="tool_calls",
                    index=0,
                    logprobs=None,
                    delta=ChoiceDelta(content=None),
                )
            ],
            created=0,
            model="gpt-4o-mini",
            object="chat.completion.chunk",
            usage=CompletionUsage(completion_tokens=3, prompt_tokens=5, total_tokens=8),
        ),
    ]

    class _FakeStream:
        def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
            self._chunks = chunks

        def __aiter__(self) -> _FakeStream:
            self._index = 0
            return self

        async def __anext__(self) -> ChatCompletionChunk:
            if self._index >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    class _FakeCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> _FakeStream:
            self.calls.append(kwargs)
            return _FakeStream(chunks)

    class _FakeOpenAI:
        def __init__(self) -> None:
            self.chat = type("_FakeChat", (), {"completions": _FakeCompletions()})()

    client = OpenAIClient(model="gpt-4o-mini", base_url="http://localhost:8000/v1")
    fake = cast(Any, _FakeOpenAI())
    client._client = fake
    events: list[LLMStreamEvent] = [
        event
        async for event in client.stream(
            _messages(), tools=_tools(), system=None, max_tokens=50, temperature=0.0
        )
    ]
    assert [event.type for event in events] == ["text", "text", "tool_request", "usage"]
    assert "".join(event.text for event in events if event.type == "text") == "Hello"
    request = next(event.tool_request for event in events if event.type == "tool_request")
    assert request is not None
    assert request.id == "call_1"
    assert request.name == "file_read"
    assert request.arguments == {"path": "x.py"}
    usage = next(event for event in events if event.type == "usage")
    assert usage.usage is not None
    assert usage.usage.input_tokens == 5
    assert usage.usage.output_tokens == 3
    assert usage.model == "gpt-4o-mini"
