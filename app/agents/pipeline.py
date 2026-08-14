"""Planner → coder → reviewer → tester pipeline composed in a LangGraph graph.

Every stage runs a :class:`LoopAgent` over the *same* accumulated transcript:
each stage hands the accumulated conversation to the loop via ``run_from``
and returns only the messages it produced, so the transcript grows
monotonically across stages and is persisted exactly once per message by the
shared ``on_message`` hook.

Control flow: planner produces the plan, the coder implements it, the
reviewer judges it, and the tester verifies it. A non-PASS reviewer routes
back to the coder for rework, as does a failing tester; each round-trip is
counted in ``pass_count`` and bounded by ``max_passes`` so the pipeline always
terminates. The final answer is the last stage's message (reviewer or tester
answer), which carries the verdict and feedback.
"""

from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.base import DEFAULT_MAX_STEPS, LoopAgent, TokenHandler
from app.agents.repair import RepairAgent
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider

_PLANNER_PROMPT = (
    "You are a planning agent working inside a user's repository. Analyze the "
    "task and produce a concrete, step-by-step plan the coder can follow. "
    "Number the steps and keep each one actionable. Do NOT modify any files "
    "or run commands; use read-only tools only if you need to inspect the "
    "repository. End your turn with the final plan."
)

_CODER_PROMPT = (
    "You are a coding agent working inside a user's repository. "
    "Use the provided tools to explore the code, make focused edits, run "
    "commands, and commit your work. Prefer small, verifiable changes. Run "
    "the relevant tests before finishing. When you are done, end your turn "
    "with a concise summary of what you changed and why."
)

REVIEWER_PROMPT = (
    "You are a code reviewer working inside a user's repository. Inspect the "
    "changes the coder made using read-only tools (diff, read, status). "
    "Evaluate correctness, style, and test coverage against the plan. Begin "
    "your final reply with exactly one line: 'VERDICT: PASS' if the work is "
    "acceptable, or 'VERDICT: CHANGES_NEEDED' followed by specific, "
    "actionable feedback the coder can implement. Put nothing before that "
    "line. "
    "After the verdict, list each finding as an entry in a single JSON array "
    "inside a fenced code block, like:\n"
    '```json\n'
    '[{"severity": "high", "file": "app/auth.py", "line": 12, '
    '"problem": "short description", "reason": "why it matters", '
    '"fix": "suggested fix"}]\n'
    "```\n"
    "severity is one of critical, high, medium, low, nit; line may be null; "
    "reason and fix are optional. Emit an empty array [] when the review is "
    "clean."
)


class PipelineState(TypedDict, total=False):
    """State threaded through the pipeline graph.

    ``messages`` and the token counters accumulate via ``operator.add``;
    ``step`` sums the LLM calls across every stage. ``pass_count`` counts
    rework round-trips (reviewer→coder, tester→coder) and is bounded by
    ``max_passes``.
    """

    goal: str
    messages: Annotated[list[ChatMessage], operator.add]
    step: Annotated[int, operator.add]
    max_steps: int
    pass_count: int
    max_passes: int
    plan: str
    feedback: str
    final_answer: str
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]


@dataclass
class PipelineResult:
    """Outcome of a completed pipeline run.

    Attributes:
        answer: The pipeline's final answer (the last stage's message).
        messages: The full transcript (goal plus every stage's messages).
        input_tokens: Total input tokens across all stage LLM calls.
        output_tokens: Total output tokens across all stage LLM calls.
        steps: Total LLM calls across all stages.
        passes: Number of rework round-trips (reviewer/tester rejections).
    """

    answer: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int
    passes: int


def parse_verdict(answer: str) -> bool:
    """Return ``True`` when the answer declares PASS.

    Stage prompts require the final message to begin with a
    ``VERDICT: PASS|CHANGES_NEEDED|FAIL`` line, but a verdict line later in
    the answer is honoured too; anything without a ``PASS`` verdict is
    treated as a rejection (fail-safe).
    """
    lines = answer.strip().splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            token = stripped.split(":", 1)[1].strip().upper()
            return "PASS" in token.split()
    first = (lines or [""])[0].strip().upper()
    return "PASS" in first.split()


@dataclass
class _StageResult:
    """One stage loop's output: the delta transcript plus accounting."""

    answer: str
    delta: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int


class PipelineAgent:
    """Compose the planner/coder/reviewer/tester loops into one pipeline.

    Args:
        llm: The LLM provider to call.
        executor: Executor for the workspace all stages work in.
        max_passes: Upper bound on rework round-trips before the pipeline
            terminates with the latest reviewer/tester verdict.
        max_repairs: Upper bound on fix → re-run iterations in the tester's
            test-and-repair loop.
        test_command: Override for the test command the tester runs;
            auto-detected from the workspace when ``None``.
        max_steps: Per-stage bound on LLM calls (each stage loop).
        max_tokens: Cap on generated tokens per LLM call.
        temperature: Sampling temperature.
        on_message: Optional callback invoked with every produced message in
            transcript order, as it is produced (shared across stages).
        on_token: Optional callback invoked with incremental text deltas
            while a stage generates (when ``stream`` is enabled).
        stream: When ``True``, stages generate via the provider's
            ``stream()`` and forward deltas to ``on_token``; falls back to
            ``complete()`` when streaming is unavailable.
        should_cancel: Optional predicate checked at each stage's step
            boundaries; when ``True`` the run raises :class:`TaskCancelled`.
    """

    def __init__(
        self,
        *,
        llm: LLMProvider,
        executor: ToolExecutor,
        max_passes: int = 2,
        max_repairs: int = 2,
        test_command: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        on_message: Callable[[ChatMessage], Awaitable[None] | None] | None = None,
        on_token: TokenHandler | None = None,
        stream: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._max_passes = max_passes
        self._max_repairs = max_repairs
        self._test_command = test_command
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._on_message = on_message
        self._on_token = on_token
        self._stream = stream
        self._should_cancel = should_cancel
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(PipelineState)
        builder.add_node("planner", self._planner)
        builder.add_node("coder", self._coder)
        builder.add_node("reviewer", self._reviewer)
        builder.add_node("tester", self._tester)
        builder.add_edge(START, "planner")
        builder.add_edge("planner", "coder")
        builder.add_edge("coder", "reviewer")
        builder.add_conditional_edges(
            "reviewer",
            self._route_review,
            {"coder": "coder", "tester": "tester", "end": END},
        )
        builder.add_conditional_edges(
            "tester",
            self._route_test,
            {"coder": "coder", "end": END},
        )
        return builder.compile()

    async def run(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> PipelineResult:
        """Execute the full pipeline for a goal and return the final result.

        The goal is seeded as the leading user message (streamed through
        ``on_message`` before the graph runs); any ``initial_messages`` are
        appended after it, matching the coder agent. Every stage then reads
        the full seeded conversation via ``run_from``.
        """
        goal_message = ChatMessage(role=ChatRole.USER, content=goal)
        await self._invoke_on_message(goal_message)
        for message in initial_messages:
            await self._invoke_on_message(message)
        initial: PipelineState = {
            "goal": goal,
            "messages": [goal_message, *initial_messages],
            "step": 0,
            "max_steps": self._max_steps,
            "pass_count": 0,
            "max_passes": self._max_passes,
            "plan": "",
            "feedback": "",
            "final_answer": "",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        final = await self._graph.ainvoke(initial)
        return PipelineResult(
            answer=final.get("final_answer", ""),
            messages=final.get("messages", []),
            input_tokens=final.get("input_tokens", 0),
            output_tokens=final.get("output_tokens", 0),
            steps=final.get("step", 0),
            passes=final.get("pass_count", 0),
        )

    async def _planner(self, state: PipelineState) -> dict[str, Any]:
        result = await self._run_stage(state, system_prompt=_PLANNER_PROMPT)
        return {
            "messages": result.delta,
            "step": result.steps,
            "plan": result.answer,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    async def _coder(self, state: PipelineState) -> dict[str, Any]:
        result = await self._run_stage(state, system_prompt=_CODER_PROMPT)
        return {
            "messages": result.delta,
            "step": result.steps,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    async def _reviewer(self, state: PipelineState) -> dict[str, Any]:
        result = await self._run_stage(state, system_prompt=REVIEWER_PROMPT)
        passed = parse_verdict(result.answer)
        return {
            "messages": result.delta,
            "step": result.steps,
            "pass_count": state.get("pass_count", 0) + (0 if passed else 1),
            "feedback": result.answer,
            "final_answer": result.answer,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    async def _tester(self, state: PipelineState) -> dict[str, Any]:
        result = await self._run_repair_stage(state)
        passed = parse_verdict(result.answer)
        return {
            "messages": result.delta,
            "step": result.steps,
            "pass_count": state.get("pass_count", 0) + (0 if passed else 1),
            "feedback": result.answer,
            "final_answer": result.answer,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    def _route_review(self, state: PipelineState) -> str:
        if parse_verdict(state.get("feedback", "")):
            return "tester"
        if state.get("pass_count", 0) < state.get("max_passes", self._max_passes):
            return "coder"
        return "end"

    def _route_test(self, state: PipelineState) -> str:
        if parse_verdict(state.get("feedback", "")):
            return "end"
        if state.get("pass_count", 0) < state.get("max_passes", self._max_passes):
            return "coder"
        return "end"

    async def _run_stage(
        self,
        state: PipelineState,
        *,
        system_prompt: str,
    ) -> _StageResult:
        """Run one stage's loop over the accumulated transcript.

        A fresh :class:`LoopAgent` runs over ``state["messages"]`` without
        re-emitting them (``run_from``), so only the delta is returned and the
        shared transcript stays consistent across stages.
        """
        agent = LoopAgent(
            llm=self._llm,
            executor=self._executor,
            system_prompt=system_prompt,
            max_steps=self._max_steps,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            on_message=self._on_message,
            on_token=self._on_token,
            stream=self._stream,
            should_cancel=self._should_cancel,
        )
        previous = list(state.get("messages") or [])
        result = await agent.run_from(previous)
        return _StageResult(
            answer=result.answer,
            delta=result.messages[len(previous) :],
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            steps=result.steps,
        )

    async def _run_repair_stage(
        self,
        state: PipelineState,
    ) -> _StageResult:
        """Run the structured test-and-repair loop over the transcript."""
        agent = RepairAgent(
            llm=self._llm,
            executor=self._executor,
            max_repairs=self._max_repairs,
            max_steps=self._max_steps,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            on_message=self._on_message,
            on_token=self._on_token,
            stream=self._stream,
            should_cancel=self._should_cancel,
            test_command=self._test_command,
        )
        previous = list(state.get("messages") or [])
        result = await agent.run_from(previous)
        return _StageResult(
            answer=result.answer,
            delta=result.messages[len(previous) :],
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            steps=result.steps,
        )

    async def _invoke_on_message(self, message: ChatMessage) -> None:
        if self._on_message is None:
            return
        result = self._on_message(message)
        if result is not None:
            await result
