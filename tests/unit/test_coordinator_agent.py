"""Unit tests for the coordinator agent and its dispatch parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from tests.unit.fake_llm import FakeLLM

from app.agents.base import LoopAgent
from app.agents.coordinator import (
    READ_ONLY_TOOLS,
    SPECIALISTS,
    CoordinatorAgent,
    parse_dispatch,
)
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMResponse, LLMUsage
from app.orchestrator.cancellation import TaskCancelled
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec


class _StubRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def specs(self) -> list[ToolSpec]:
        return self._specs


class _StubExecutor:
    """Executor stub with one read-only and one mutating tool spec."""

    def __init__(self, fail: bool = False) -> None:
        self.workspace_dir = Path("/workspace")
        self._fail = fail
        self.calls: list[ToolCall] = []
        self.registry = _StubRegistry(
            [
                ToolSpec(
                    name=ToolName.FILE_READ,
                    description="Read a file",
                    arguments_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                ),
                ToolSpec(
                    name=ToolName.FILE_WRITE,
                    description="Write a file",
                    arguments_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    },
                ),
            ]
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if self._fail:
            raise RuntimeError("boom")
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, output="42")


def _final_response(content: str, *, input_tokens: int = 5, output_tokens: int = 2) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
    )


def _dispatch_json(decision: dict[str, Any]) -> LLMResponse:
    return _final_response(f"```json\n{json.dumps(decision)}\n```")


def _coordinator(script: list[LLMResponse], **kwargs: Any) -> CoordinatorAgent:
    executor = cast(ToolExecutor, _StubExecutor(kwargs.pop("fail", False)))
    return CoordinatorAgent(llm=FakeLLM(script), executor=executor, **kwargs)


# --- parse_dispatch -------------------------------------------------------


def test_parse_dispatch_fenced_json() -> None:
    decision = parse_dispatch('```json\n{"specialists": ["security"], "needs_changes": true}\n```')
    assert decision.specialists == ["security"]
    assert decision.needs_changes is True


def test_parse_dispatch_bare_json() -> None:
    decision = parse_dispatch('{"specialists": ["performance"], "needs_changes": false}')
    assert decision.specialists == ["performance"]
    assert decision.needs_changes is False


def test_parse_dispatch_drops_unknown_and_dedupes() -> None:
    decision = parse_dispatch(
        '{"specialists": ["security", "mystery", "security"], "needs_changes": true}'
    )
    assert decision.specialists == ["security"]


def test_parse_dispatch_unparseable_falls_back_read_only() -> None:
    decision = parse_dispatch("I think security would be useful.")
    assert set(decision.specialists) == set(SPECIALISTS)
    assert decision.needs_changes is False
    assert "not parseable" in decision.reason


def test_parse_dispatch_non_json_payload_falls_back() -> None:
    decision = parse_dispatch('{"specialists": [1, 2], "needs_changes": "maybe"}')
    assert set(decision.specialists) == set(SPECIALISTS)
    assert decision.needs_changes is False


# --- coordinator happy paths ----------------------------------------------


async def test_coordinator_read_only_happy_path() -> None:
    script = [
        _dispatch_json({"specialists": ["security"], "needs_changes": False}),
        _final_response("Security: no issues found.", input_tokens=7, output_tokens=3),
        _final_response("Final: clean bill of health.", input_tokens=9, output_tokens=4),
    ]
    agent = _coordinator(script)

    result = await agent.run("Review the auth flow")

    assert result.answer == "Final: clean bill of health."
    assert result.specialists_run == ("security",)
    assert result.decision.specialists == ["security"]
    assert result.decision.needs_changes is False
    # goal + specialist + synthesis
    assert [m.content for m in result.messages] == [
        "Review the auth flow",
        "Security: no issues found.",
        "Final: clean bill of health.",
    ]
    roles = [ChatRole.USER, ChatRole.ASSISTANT, ChatRole.ASSISTANT]
    assert [m.role for m in result.messages] == roles
    # dispatch (1) + specialist (1) + synthesis (1); dispatch is also an LLM
    # call with default 5/2 tokens, so totals include it
    assert result.steps == 3
    assert result.input_tokens == 5 + 7 + 9
    assert result.output_tokens == 2 + 3 + 4


async def test_coordinator_parallel_specialists() -> None:
    script = [
        _dispatch_json({"specialists": ["security", "performance"], "needs_changes": False}),
        _final_response("Security: ok."),
        _final_response("Performance: slow query."),
        _final_response("Final: both reports."),
    ]
    agent = _coordinator(script)

    result = await agent.run("Assess the service")

    assert result.specialists_run == ("security", "performance")
    assert result.steps == 4  # dispatch + 2 specialists + synthesis
    assert [m.content for m in result.messages] == [
        "Assess the service",
        "Security: ok.",
        "Performance: slow query.",
        "Final: both reports.",
    ]


async def test_coordinator_runs_coder_when_changes_needed() -> None:
    script = [
        _dispatch_json({"specialists": ["security"], "needs_changes": True}),
        _final_response("Security: path traversal in reader.py."),
        _final_response("Fixed the traversal."),
        _final_response("Final: patched and summarized."),
    ]
    agent = _coordinator(script)

    result = await agent.run("Harden the file reader")

    assert result.decision.needs_changes is True
    assert [m.content for m in result.messages] == [
        "Harden the file reader",
        "Security: path traversal in reader.py.",
        "Fixed the traversal.",
        "Final: patched and summarized.",
    ]
    assert result.steps == 4  # dispatch + specialist + coder + synthesis


async def test_coordinator_specialists_are_read_only() -> None:
    script = [
        _dispatch_json({"specialists": ["security"], "needs_changes": False}),
        _final_response("Security: ok."),
        _final_response("Final: done."),
    ]
    executor = cast(ToolExecutor, _StubExecutor())
    fake = FakeLLM(script)
    agent = CoordinatorAgent(llm=fake, executor=executor)

    await agent.run("Assess")

    # dispatch call has no tools; synthesis has none either
    assert fake.calls[0]["tools"] == []
    assert fake.calls[2]["tools"] == []
    # the specialist call is confined to read-only tools
    offered = {spec.name for spec in fake.calls[1]["tools"]}
    assert offered
    assert offered <= set(READ_ONLY_TOOLS)
    assert ToolName.FILE_WRITE not in offered


async def test_coordinator_max_specialists_caps_dispatch() -> None:
    script = [
        _dispatch_json({"specialists": ["security", "performance"], "needs_changes": False}),
        _final_response("Security: ok."),
        _final_response("Final: done."),
    ]
    agent = _coordinator(script, max_specialists=1)

    result = await agent.run("Assess")

    assert result.specialists_run == ("security",)


async def test_coordinator_specialist_failure_is_captured() -> None:
    request = ToolRequest(name="file_read", arguments={"path": "x.py"})
    script = [
        _dispatch_json({"specialists": ["security"], "needs_changes": False}),
        LLMResponse(
            content="",
            tool_requests=[request],
            stop_reason="tool_use",
            usage=LLMUsage(input_tokens=5, output_tokens=2),
            model="fake-model",
        ),
        _final_response("Final: despite the glitch."),
    ]
    agent = _coordinator(script, fail=True)

    result = await agent.run("Assess")

    assert result.answer == "Final: despite the glitch."
    assert result.specialists_run == ()
    assert "[specialist security failed" in result.messages[1].content
    assert result.messages[2].content == "Final: despite the glitch."


async def test_coordinator_on_message_streams_full_transcript() -> None:
    script = [
        _dispatch_json({"specialists": [], "needs_changes": False}),
        _final_response("Final: nothing to do."),
    ]
    emitted: list[ChatMessage] = []

    async def on_message(message: ChatMessage) -> None:
        emitted.append(message)

    agent = _coordinator(script, on_message=on_message)
    result = await agent.run("Assess")

    assert [m.content for m in emitted] == [m.content for m in result.messages]


async def test_coordinator_cancellation_before_dispatch() -> None:
    agent = _coordinator([], should_cancel=lambda: True)
    with pytest.raises(TaskCancelled):
        await agent.run("Assess")


async def test_coordinator_no_specialists_no_changes() -> None:
    script = [
        _dispatch_json({"specialists": [], "needs_changes": False}),
        _final_response("Final: nothing to do."),
    ]
    agent = _coordinator(script)

    result = await agent.run("Assess")

    assert result.specialists_run == ()
    assert [m.content for m in result.messages] == ["Assess", "Final: nothing to do."]


# --- LoopAgent tool allowlist ---------------------------------------------


async def test_loop_agent_allowlist_filters_offered_tools() -> None:
    script = [_final_response("Done.")]
    fake = FakeLLM(script)
    executor = cast(ToolExecutor, _StubExecutor())
    agent = LoopAgent(
        llm=fake,
        executor=executor,
        system_prompt="You are a test agent.",
        tool_names=[ToolName.FILE_READ],
    )

    await agent.run("Inspect")

    offered = {spec.name for spec in fake.calls[0]["tools"]}
    assert offered == {ToolName.FILE_READ}


async def test_loop_agent_rejects_disallowed_tool() -> None:
    executor = cast(ToolExecutor, _StubExecutor())
    request = ToolRequest(name="file_write", arguments={"path": "x.py", "content": "x"})
    fake = FakeLLM(
        [
            LLMResponse(
                content="",
                tool_requests=[request],
                stop_reason="tool_use",
                usage=LLMUsage(input_tokens=5, output_tokens=2),
                model="fake-model",
            ),
            _final_response("Cannot do that."),
        ]
    )
    agent = LoopAgent(
        llm=fake,
        executor=executor,
        system_prompt="You are a test agent.",
        tool_names=[ToolName.FILE_READ],
    )

    result = await agent.run("Inspect")

    assert result.answer == "Cannot do that."
    tool_messages = [m for m in result.messages if m.role == ChatRole.TOOL]
    assert tool_messages and "not allowed" in tool_messages[0].content
    assert executor.calls == []
