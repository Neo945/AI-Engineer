"""Unit tests for the structured test-and-repair loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from tests.unit.fake_llm import FakeLLM

from app.agents.repair import RepairAgent
from app.executor.executor import ToolExecutor
from app.executor.test_parser import TestReport
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMResponse, LLMUsage
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec


class _StubRegistry:
    def specs(self) -> list[ToolSpec]:
        return []


class _StubExecutor:
    def __init__(self, workspace_dir: Path, reports: list[TestReport]) -> None:
        self.workspace_dir = workspace_dir
        self.registry = _StubRegistry()
        self._reports = list(reports)
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        report = (
            self._reports.pop(0)
            if self._reports
            else TestReport(framework="pytest", command="pytest -q")
        )
        return ToolResult(
            call_id=call.id,
            tool=call.tool,
            ok=report.ok,
            output="report",
            data={"report": report.to_dict()},
        )


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=3, output_tokens=1),
        model="fake-model",
    )


def _report(*, ok: bool) -> TestReport:
    return TestReport(
        framework="pytest",
        command="pytest -q",
        passed=1 if ok else 0,
        failed=0 if ok else 1,
    )


def _agent(
    workspace_dir: Path,
    reports: list[TestReport],
    llm: FakeLLM,
    **kwargs: Any,
) -> RepairAgent:
    return RepairAgent(
        llm=llm,
        executor=cast(ToolExecutor, _StubExecutor(workspace_dir, reports)),
        **kwargs,
    )


async def test_passes_without_repair(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [_report(ok=True)], FakeLLM())

    result = await agent.run("Fix the bug")

    assert result.answer.startswith("VERDICT: PASS")
    assert result.repairs == 0
    assert result.steps == 0


async def test_repairs_until_pass(tmp_path: Path) -> None:
    llm = FakeLLM([_response("Fixed it.")])
    agent = _agent(tmp_path, [_report(ok=False), _report(ok=True)], llm, max_repairs=2)

    result = await agent.run("Fix the bug")

    assert result.answer.startswith("VERDICT: PASS")
    assert result.repairs == 1
    assert result.steps == 1
    assert result.messages[1].content.startswith("The test suite still has failures")
    assert result.messages[2].content == "Fixed it."
    assert result.messages[3].content.startswith("VERDICT: PASS")


async def test_exhausts_max_repairs(tmp_path: Path) -> None:
    llm = FakeLLM([_response("try1"), _response("try2")])
    agent = _agent(
        tmp_path,
        [_report(ok=False), _report(ok=False), _report(ok=False)],
        llm,
        max_repairs=2,
    )

    result = await agent.run("Fix the bug")

    assert result.answer.startswith("VERDICT: FAIL")
    assert result.repairs == 2
    assert result.steps == 2


async def test_uses_explicit_test_command(tmp_path: Path) -> None:
    executor = _StubExecutor(tmp_path, [_report(ok=True)])
    agent = RepairAgent(
        llm=FakeLLM(),
        executor=cast(ToolExecutor, executor),
        test_command="make test",
    )

    await agent.run("Fix the bug")

    call = executor.calls[0]
    assert call.tool == ToolName.TEST_RUN
    assert call.arguments["command"] == "make test"


async def test_seeds_goal_and_initial_messages(tmp_path: Path) -> None:
    agent = _agent(tmp_path, [_report(ok=True)], FakeLLM())

    result = await agent.run(
        "Fix the bug",
        initial_messages=[ChatMessage(role=ChatRole.ASSISTANT, content="plan")],
    )

    assert result.messages[0].content == "Fix the bug"
    assert result.messages[1].content == "plan"


async def test_messages_streamed_via_on_message(tmp_path: Path) -> None:
    streamed: list[ChatMessage] = []
    llm = FakeLLM([_response("Fixed it.")])
    agent = _agent(
        tmp_path,
        [_report(ok=False), _report(ok=True)],
        llm,
        max_repairs=2,
        on_message=lambda message: streamed.append(message),
    )

    await agent.run("Fix the bug")

    assert [message.role for message in streamed] == [
        ChatRole.USER,
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.ASSISTANT,
    ]
