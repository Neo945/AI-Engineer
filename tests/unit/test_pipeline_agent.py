"""Unit tests for the multi-agent pipeline (planner → coder → reviewer → tester)."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, cast

import pytest
from tests.unit.fake_llm import FakeLLM

from app.agents.pipeline import PipelineAgent, parse_verdict
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMResponse, LLMUsage
from app.orchestrator.cancellation import TaskCancelled
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec


class _StubRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = specs

    def specs(self) -> list[ToolSpec]:
        return self._specs


class _StubExecutor:
    def __init__(self, test_results: list[bool] | None = None) -> None:
        self.workspace_dir = Path("/workspace")
        self._test_results = deque(test_results or [True])
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
                )
            ]
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        ok = self._test_results.popleft() if self._test_results else True
        return ToolResult(call_id=call.id, tool=call.tool, ok=ok, output="42")


def _final_response(content: str, *, input_tokens: int = 5, output_tokens: int = 2) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="fake-model",
    )


def _pipeline(script: list[LLMResponse], **kwargs: Any) -> PipelineAgent:
    executor = cast(ToolExecutor, _StubExecutor(kwargs.pop("test_results", None)))
    return PipelineAgent(llm=FakeLLM(script), executor=executor, **kwargs)


def test_parse_verdict() -> None:
    assert parse_verdict("VERDICT: PASS\nAll good.") is True
    assert parse_verdict("PASS") is True
    assert parse_verdict("verdict: pass") is True
    assert parse_verdict("VERDICT: CHANGES_NEEDED\nAdd tests.") is False
    assert parse_verdict("VERDICT: FAIL\nBroken.") is False
    assert parse_verdict("") is False
    assert parse_verdict("No verdict here.") is False


def test_parse_verdict_scans_later_lines() -> None:
    assert parse_verdict("## Findings\nEverything looks good.\nVERDICT: PASS") is True
    assert parse_verdict("## Findings\nVERDICT: CHANGES_NEEDED\nAdd tests.") is False


async def test_pipeline_happy_path() -> None:
    script = [
        _final_response("Plan: 1. Inspect. 2. Fix."),
        _final_response("Fixed the bug."),
        _final_response("VERDICT: PASS\nLooks good."),
    ]
    agent = _pipeline(script)

    result = await agent.run("Fix the bug")

    assert result.passes == 0
    assert result.answer.startswith("VERDICT: PASS")
    assert result.input_tokens == 15
    assert result.output_tokens == 6
    assert result.steps == 3
    assert [message.role for message in result.messages] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
    ]
    assert [message.content for message in result.messages[:4]] == [
        "Fix the bug",
        "Plan: 1. Inspect. 2. Fix.",
        "Fixed the bug.",
        "VERDICT: PASS\nLooks good.",
    ]
    assert result.messages[4].content.startswith("VERDICT: PASS\nTest run: pytest")


async def test_pipeline_feeds_accumulated_transcript_to_each_stage() -> None:
    script = [
        _final_response("Plan."),
        _final_response("Fix."),
        _final_response("VERDICT: PASS"),
        _final_response("Fixed it."),
    ]
    executor = cast(ToolExecutor, _StubExecutor(test_results=[False, True]))
    fake = FakeLLM(script)
    agent = PipelineAgent(llm=fake, executor=executor)

    await agent.run("Goal")

    # Planner [goal], coder [goal+plan], reviewer [..+coder]; the tester's
    # first run fails so its repair turn sees the grown transcript plus the
    # failure report appended by the repair loop.
    assert [len(call["messages"]) for call in fake.calls] == [1, 2, 3, 5]
    assert all(call["system"] is not None for call in fake.calls)
    assert fake.calls[3]["messages"][1].content == "Plan."


async def test_pipeline_routes_back_to_coder_on_changes_needed() -> None:
    script = [
        _final_response("Plan."),
        _final_response("Fix v1."),
        _final_response("VERDICT: CHANGES_NEEDED\nAdd tests."),
        _final_response("Fix v2."),
        _final_response("VERDICT: PASS\nNow it is complete."),
    ]
    agent = _pipeline(script)

    result = await agent.run("Goal")

    assert result.passes == 1
    assert result.answer.startswith("VERDICT: PASS")
    assert len(result.messages) == 7  # goal + plan + 2x(coder+reviewer) + tester
    assert result.messages[3].content == "VERDICT: CHANGES_NEEDED\nAdd tests."
    assert result.messages[4].content == "Fix v2."


async def test_pipeline_routes_back_to_coder_on_failed_tests() -> None:
    script = [
        _final_response("Plan."),
        _final_response("Fix."),
        _final_response("VERDICT: PASS"),
        _final_response("Fix again."),
        _final_response("VERDICT: PASS"),
    ]
    agent = _pipeline(script, max_repairs=0, test_results=[False, True])

    result = await agent.run("Goal")

    assert result.passes == 1
    assert result.answer.startswith("VERDICT: PASS")
    # goal + plan + coder + review + tester(fail) + coder + review + tester(pass)
    assert len(result.messages) == 8


async def test_pipeline_stops_after_max_passes() -> None:
    script = [
        _final_response("Plan."),
        _final_response("Fix v1."),
        _final_response("VERDICT: CHANGES_NEEDED\nRound 1."),
        _final_response("Fix v2."),
        _final_response("VERDICT: CHANGES_NEEDED\nRound 2."),
    ]
    agent = _pipeline(script, max_passes=2)

    result = await agent.run("Goal")

    assert result.passes == 2
    assert result.answer == "VERDICT: CHANGES_NEEDED\nRound 2."
    assert len(result.messages) == 6  # goal + plan + coder + review + coder + review

    # The tester is never reached when the reviewer never passes.
    assert "VERDICT: PASS" not in result.answer


async def test_pipeline_zero_passes_allowed() -> None:
    script = [
        _final_response("Plan."),
        _final_response("Fix."),
        _final_response("VERDICT: CHANGES_NEEDED"),
    ]
    agent = _pipeline(script, max_passes=0)

    result = await agent.run("Goal")

    assert result.passes == 1
    assert result.answer == "VERDICT: CHANGES_NEEDED"
    assert len(result.messages) == 4  # goal + plan + coder + reviewer; no rework


async def test_pipeline_streams_messages_in_transcript_order() -> None:
    script = [
        _final_response("Plan."),
        _final_response("Fix."),
        _final_response("VERDICT: PASS"),
    ]
    streamed: list[ChatMessage] = []
    executor = cast(ToolExecutor, _StubExecutor())
    agent = PipelineAgent(
        llm=FakeLLM(script),
        executor=executor,
        on_message=lambda message: streamed.append(message),
    )

    await agent.run("Goal")

    assert [message.role for message in streamed] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
    ]
    assert streamed[0].content == "Goal"


async def test_pipeline_raises_task_cancelled_when_hook_requests_cancel() -> None:
    executor = cast(ToolExecutor, _StubExecutor())
    agent = PipelineAgent(
        llm=FakeLLM([_final_response("Plan.")]),
        executor=executor,
        should_cancel=lambda: True,
    )

    with pytest.raises(TaskCancelled):
        await agent.run("Goal")
