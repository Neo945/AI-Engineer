"""Unit tests for the LangGraph coder agent loop."""

from __future__ import annotations

from typing import cast

from tests.unit.fake_llm import FakeLLM

from app.agents.coder import CoderAgent, format_tool_result
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMResponse, LLMUsage
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec


class _StubRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def specs(self) -> list[ToolSpec]:
        return self._specs


class _StubExecutor:
    def __init__(
        self,
        *,
        results: dict[str, ToolResult] | None = None,
        specs: list[ToolSpec] | None = None,
    ) -> None:
        self.calls: list[ToolCall] = []
        self._results = results or {}
        self.registry = _StubRegistry(
            specs
            or [
                ToolSpec(
                    name=ToolName.FILE_READ,
                    description="Read a file",
                    arguments_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                )
            ]
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return self._results.get(
            call.id,
            ToolResult(call_id=call.id, tool=call.tool, ok=True, output="42"),
        )


def _tool_response(
    *,
    content: str = "",
    requests: list[ToolRequest],
    input_tokens: int = 5,
    output_tokens: int = 2,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_requests=requests,
        stop_reason="tool_use",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
    )


def _final_response(content: str, *, input_tokens: int = 5, output_tokens: int = 2) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
    )


async def test_agent_runs_tool_then_answers() -> None:
    stub = _StubExecutor()
    executor = cast(ToolExecutor, stub)
    request = ToolRequest(name="file_read", arguments={"path": "x.py"})
    fake = FakeLLM(
        [
            _tool_response(requests=[request]),
            _final_response("Read x.py: done."),
        ]
    )
    agent = CoderAgent(llm=fake, executor=executor)
    result = await agent.run("Fix the bug")

    assert result.answer == "Read x.py: done."
    assert result.steps == 2
    assert result.input_tokens == 10
    assert result.output_tokens == 4
    assert [call.tool for call in stub.calls] == [ToolName.FILE_READ]
    assert stub.calls[0].arguments == {"path": "x.py"}
    assert stub.calls[0].id == request.id

    assert [message.role for message in result.messages] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.TOOL,
        ChatRole.ASSISTANT,
    ]
    assistant = result.messages[1]
    assert assistant.tool_requests[0].name == "file_read"
    tool_message = result.messages[2]
    assert tool_message.tool_call_id == request.id
    assert tool_message.content == "42"
    assert fake.calls[0]["system"] is not None
    assert fake.calls[0]["tools"][0].name == ToolName.FILE_READ
    # The second LLM call sees the accumulated transcript (goal + tool turn).
    assert len(fake.calls[1]["messages"]) == 3


async def test_agent_answers_without_tools() -> None:
    stub = _StubExecutor()
    executor = cast(ToolExecutor, stub)
    fake = FakeLLM([_final_response("No changes needed.")])
    agent = CoderAgent(llm=fake, executor=executor)
    result = await agent.run("Is the repo clean?")

    assert result.answer == "No changes needed."
    assert result.steps == 1
    assert stub.calls == []
    assert [message.role for message in result.messages] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
    ]


async def test_agent_handles_unknown_tool_gracefully() -> None:
    stub = _StubExecutor()
    executor = cast(ToolExecutor, stub)
    bogus = ToolRequest(name="not_a_real_tool", arguments={})
    fake = FakeLLM(
        [
            _tool_response(requests=[bogus]),
            _final_response("I could not run that."),
        ]
    )
    agent = CoderAgent(llm=fake, executor=executor)
    result = await agent.run("Do something")

    assert stub.calls == []
    tool_message = result.messages[2]
    assert tool_message.role == ChatRole.TOOL
    assert tool_message.tool_call_id == bogus.id
    assert "unknown tool" in tool_message.content


async def test_agent_stops_at_max_steps() -> None:
    stub = _StubExecutor()
    executor = cast(ToolExecutor, stub)
    endless = _tool_response(requests=[ToolRequest(name="file_read", arguments={"path": "x.py"})])
    fake = FakeLLM([endless] * 10)
    agent = CoderAgent(llm=fake, executor=executor, max_steps=3)
    result = await agent.run("Loop forever?")

    assert result.steps == 3
    assert len(fake.calls) == 3
    assert result.answer == ""
    assert len(result.messages) == 7  # goal + 3x (assistant + tool result)


async def test_agent_formats_failed_tool_result() -> None:
    request = ToolRequest(name="file_read", arguments={"path": "x.py"})
    failure = ToolResult(
        call_id=request.id,
        tool=ToolName.FILE_READ,
        ok=False,
        output="partial output",
        error="boom",
    )
    stub = _StubExecutor(results={request.id: failure})
    executor = cast(ToolExecutor, stub)
    fake = FakeLLM(
        [
            _tool_response(requests=[request]),
            _final_response("Retrying."),
        ]
    )
    agent = CoderAgent(llm=fake, executor=executor)
    result = await agent.run("Try it")

    tool_message = result.messages[2]
    assert "partial output" in tool_message.content
    assert "[error] boom" in tool_message.content


def test_format_tool_result_variants() -> None:
    call = ToolCall(tool=ToolName.FILE_READ, arguments={"path": "x"})
    assert (
        format_tool_result(ToolResult(call_id=call.id, tool=call.tool, ok=True, output="content"))
        == "content"
    )
    assert (
        format_tool_result(ToolResult(call_id=call.id, tool=call.tool, ok=False, error="boom"))
        == "[error] boom"
    )
    assert (
        format_tool_result(ToolResult(call_id=call.id, tool=call.tool, ok=True))
        == "(tool file_read returned no output)"
    )
    assert (
        format_tool_result(
            ToolResult(call_id=call.id, tool=call.tool, ok=False, output="out", error="e")
        )
        == "out\n[error] e"
    )


async def test_agent_can_be_seeded_with_existing_transcript() -> None:
    executor = cast(ToolExecutor, _StubExecutor())
    fake = FakeLLM([_final_response("Continuing.")])
    agent = CoderAgent(llm=fake, executor=executor)
    prior = ChatMessage(role=ChatRole.ASSISTANT, content="Earlier answer.")
    result = await agent.run("Pick up where we left off", [prior])

    assert result.answer == "Continuing."
    assert len(fake.calls[0]["messages"]) == 2  # goal + prior message
