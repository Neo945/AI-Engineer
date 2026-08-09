"""Unit tests for the structured task plan (parse/format) and PlannerAgent."""

from __future__ import annotations

from typing import cast

import pytest
from tests.unit.fake_llm import FakeLLM

from app.agents.planner import PlannerAgent
from app.agents.planning import TaskPlan, format_plan, parse_plan
from app.executor.executor import ToolExecutor
from app.llm.protocol import LLMResponse, LLMUsage
from app.orchestrator.cancellation import TaskCancelled
from app.tools.schemas import ToolCall, ToolResult, ToolSpec


class _StubRegistry:
    def specs(self) -> list[ToolSpec]:
        return []


class _StubExecutor:
    def __init__(self) -> None:
        self.registry = _StubRegistry()

    async def execute(self, call: ToolCall) -> ToolResult:  # pragma: no cover
        raise AssertionError("planner must not execute tools")


def _plan_text() -> str:
    return """Here is my plan:

## Objective
Add a reset password flow to the auth service.

## Assumptions
- The user already has an account.
- Emails are sent by the existing mailer.

## Files
- src/auth/reset.py
- src/auth/__init__.py

## Dependencies
- None

## Risks
- Token expiry races; mitigate by short TTL and idempotent reset.

## Validation
- Run the unit tests.

## Steps
1. Add the reset token model.
2. Wire the reset route.
3. Run the tests.
"""


def test_parse_plan_markdown_headings() -> None:
    plan = parse_plan(_plan_text())

    assert plan.objective == "Add a reset password flow to the auth service."
    assert plan.assumptions == [
        "The user already has an account.",
        "Emails are sent by the existing mailer.",
    ]
    assert plan.files == ["src/auth/reset.py", "src/auth/__init__.py"]
    assert plan.dependencies == []
    assert plan.risks == ["Token expiry races; mitigate by short TTL and idempotent reset."]
    assert plan.validation == ["Run the unit tests."]
    assert plan.steps == ["Add the reset token model.", "Wire the reset route.", "Run the tests."]


def test_parse_plan_prefix_headings() -> None:
    text = """Objective: Add a reset password flow.

Assumptions:
- The user has an account.

Files:
- src/auth/reset.py

Steps:
1. Add the reset token model.
2. Wire the reset route.
"""
    plan = parse_plan(text)

    assert plan.objective == "Add a reset password flow."
    assert plan.assumptions == ["The user has an account."]
    assert plan.files == ["src/auth/reset.py"]
    assert plan.steps == ["Add the reset token model.", "Wire the reset route."]


def test_parse_plan_ignores_unknown_sections() -> None:
    text = """## Objective
Do the thing.
## Notes
This is prose, not a plan section.
## Files
- one.txt
"""
    plan = parse_plan(text)

    assert plan.objective == "Do the thing."
    assert plan.files == ["one.txt"]
    assert plan.assumptions == []
    assert plan.steps == []


def test_parse_plan_without_headings_falls_back_to_objective() -> None:
    text = "Just fix the bug on line 12."
    plan = parse_plan(text)

    assert plan.objective == "Just fix the bug on line 12."
    assert plan.files == []
    assert plan.steps == []


def test_parse_plan_single_word_and_nested_bullets() -> None:
    text = """Objective: tidy up.

## Risks
- one
  - nested

## Steps
1. first
2. second
- third
"""
    plan = parse_plan(text)

    assert plan.objective == "tidy up."
    assert plan.risks == ["one", "nested"]
    assert plan.steps == ["first", "second", "third"]


@pytest.mark.parametrize(
    ("files", "steps", "risks", "objective", "expected"),
    [
        ([], [], [], "Read the README.", False),
        (["src/one.py"], [], [], "Do it.", True),
        ([], ["rm -rf vendor/"], [], "Do it.", True),
        ([], [], ["git push --force"], "Do it.", True),
        ([], [], [], "Drop table users", True),
    ],
)
def test_needs_approval(
    files: list[str],
    steps: list[str],
    risks: list[str],
    objective: str,
    expected: bool,
) -> None:
    plan = TaskPlan(
        objective=objective,
        assumptions=[],
        files=files,
        dependencies=[],
        risks=risks,
        validation=[],
        steps=steps,
    )
    assert plan.needs_approval is expected


def test_parse_plan_drops_placeholder_items() -> None:
    text = """## Objective
Read the README.
## Files
- (none)
## Dependencies
- None
## Steps
- (none)
"""
    plan = parse_plan(text)

    assert plan.files == []
    assert plan.dependencies == []
    assert plan.steps == []
    assert plan.needs_approval is False


def test_format_plan_roundtrip() -> None:
    plan = TaskPlan(
        objective="Objective here.",
        assumptions=["one", "two"],
        files=["a.py", "b.py"],
        dependencies=["pytest"],
        risks=["risk"],
        validation=["run tests"],
        steps=["step one", "step two"],
    )
    reparsed = parse_plan(format_plan(plan))

    assert reparsed == plan


def test_to_dict_roundtrip() -> None:
    plan = TaskPlan(
        objective="Objective here.",
        assumptions=["one"],
        files=["a.py"],
        dependencies=[],
        risks=["risk"],
        validation=["run tests"],
        steps=["step one"],
    )
    assert TaskPlan.from_dict(plan.to_dict()) == plan


async def test_planner_agent_returns_parsed_plan() -> None:
    fake = FakeLLM(
        [
            LLMResponse(
                content=_plan_text(),
                stop_reason="end_turn",
                usage=LLMUsage(input_tokens=7, output_tokens=3),
                model="fake-model",
            )
        ]
    )
    stub = _StubExecutor()
    executor = cast(ToolExecutor, stub)
    agent = PlannerAgent(llm=fake, executor=executor)
    result = await agent.plan("Add a reset password flow.")

    assert result.plan.objective == "Add a reset password flow to the auth service."
    assert result.plan.files == ["src/auth/reset.py", "src/auth/__init__.py"]
    assert result.text == _plan_text()
    assert result.steps == 1
    assert result.input_tokens == 7
    assert result.output_tokens == 3
    assert fake.calls[0]["system"] is not None
    assert fake.calls[0]["max_tokens"] == 4096
    assert fake.calls[0]["temperature"] == 0.0
    assert [m.role.name for m in result.messages] == ["USER", "ASSISTANT"]


async def test_planner_agent_streams_on_message_and_seeds_initial() -> None:
    from app.llm.messages import ChatMessage, ChatRole

    fake = FakeLLM(
        [
            LLMResponse(content=_plan_text(), stop_reason="end_turn"),
        ]
    )
    executor = cast(ToolExecutor, _StubExecutor())
    seen: list[str] = []
    initial = ChatMessage(role=ChatRole.USER, content="prior context")
    agent = PlannerAgent(
        llm=fake,
        executor=executor,
        on_message=lambda message: seen.append(message.role.name),
    )
    result = await agent.plan("Do the thing.", initial_messages=[initial])

    assert seen == ["USER", "USER", "ASSISTANT"]
    assert result.messages[1] is initial


async def test_planner_agent_obeys_cancellation() -> None:
    fake = FakeLLM(
        [
            LLMResponse(content=_plan_text(), stop_reason="end_turn"),
        ]
    )
    executor = cast(ToolExecutor, _StubExecutor())
    agent = PlannerAgent(llm=fake, executor=executor, should_cancel=lambda: True)

    with pytest.raises(TaskCancelled):
        await agent.plan("Do the thing.")
