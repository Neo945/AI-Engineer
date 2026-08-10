"""Structured planner agent: goal in, :class:`TaskPlan` out.

The planner is a specialized :class:`LoopAgent` that produces a concrete plan
with the sections the execution phase needs (objective, assumptions, files,
dependencies, risks, validation, steps) and parses the model's answer into a
:class:`TaskPlan`. It runs before execution so the human approval gate can
inspect and sign off on what the agent is about to do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.agents.base import (
    DEFAULT_MAX_STEPS,
    LoopAgent,
    TokenHandler,
)
from app.agents.planning import TaskPlan, format_plan, parse_plan
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage
from app.llm.protocol import LLMProvider

__all__ = ["PlannerAgent", "PlannerResult", "TaskPlan", "format_plan", "parse_plan"]

_PLANNER_SYSTEM_PROMPT = (
    "You are a planning agent working inside a user's repository. Produce a "
    "concrete, actionable plan for the given task. Do NOT modify any files or "
    "run commands; use read-only tools only if you need to inspect the code. "
    "End your answer with the plan formatted with these exact section "
    "headings, one per line:\n"
    "## Objective\n"
    "one or two sentences stating what the task accomplishes\n"
    "## Assumptions\n"
    "- assumptions the plan relies on (one bullet each)\n"
    "## Files\n"
    "- every file the plan will create or modify (one bullet each); if the "
    'task is read-only, write "(none)"\n'
    "## Dependencies\n"
    "- packages, services, or prior work the plan depends on\n"
    "## Risks\n"
    "- likely failure modes and how to mitigate them\n"
    "## Validation\n"
    "- how the result will be verified (tests, commands, manual checks)\n"
    "## Steps\n"
    "1. numbered, actionable implementation steps"
)


@dataclass
class PlannerResult:
    """Outcome of a planner run: the parsed plan plus accounting.

    Attributes:
        plan: The structured plan parsed from the model's answer.
        text: The raw planner answer (what the model actually said).
        messages: The full transcript produced by the planning loop.
        input_tokens: Total input tokens across all LLM calls.
        output_tokens: Total output tokens across all LLM calls.
        steps: Number of LLM calls made.
    """

    plan: TaskPlan
    text: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int


class PlannerAgent(LoopAgent):
    """One planning loop bound to an LLM provider and workspace executor.

    Args:
        llm: The LLM provider to call.
        executor: Executor for the workspace the plan is about.
        max_steps: Upper bound on LLM calls per run.
        max_tokens: Cap on generated tokens per LLM call.
        temperature: Sampling temperature.
        on_message: Optional callback invoked with every produced message in
            transcript order.
        on_token: Optional callback invoked with incremental text deltas
            while the model generates (when ``stream`` is enabled).
        stream: When ``True``, generate via the provider's ``stream()`` and
            forward deltas to ``on_token``; falls back to ``complete()``
            when streaming is unavailable.
        should_cancel: Optional cooperative-cancellation predicate checked at
            each step boundary.
    """

    def __init__(
        self,
        *,
        llm: LLMProvider,
        executor: ToolExecutor,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        on_message: Callable[[ChatMessage], Awaitable[None] | None] | None = None,
        on_token: TokenHandler | None = None,
        stream: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            llm=llm,
            executor=executor,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            max_steps=max_steps,
            max_tokens=max_tokens,
            temperature=temperature,
            on_message=on_message,
            on_token=on_token,
            stream=stream,
            should_cancel=should_cancel,
        )

    async def plan(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> PlannerResult:
        """Plan ``goal`` and return the parsed :class:`TaskPlan`."""
        result = await super().run(goal, initial_messages=initial_messages)
        return PlannerResult(
            plan=parse_plan(result.answer),
            text=result.answer,
            messages=result.messages,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            steps=result.steps,
        )
