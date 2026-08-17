"""Coordinator agent: intent-driven dispatch to parallel specialists.

The coordinator is the P2 "coordinator + parallel agents" mode. Given a goal
it first makes a cheap no-tool LLM decision on two things: which read-only
specialists are worth running (security, performance) and whether the goal
requires modifying the repository. Read-only specialists run **concurrently**
over the same seed conversation, each confined to a read-only tool allowlist
so they can never mutate the workspace. When changes are needed, a coder
agent then implements them sequentially over the accumulated transcript (so
reads and writes never race). A final synthesis call aggregates everything
into one answer for the user.

The decision parse degrades gracefully: an unparseable dispatch reply falls
back to read-only analysis (both specialists, no changes) so the coordinator
never edits files it was not asked to edit. Specialist failures are captured
as a transcript entry rather than failing the whole run.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import DEFAULT_MAX_STEPS, LoopAgent, TokenHandler
from app.agents.coder import CoderAgent
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider, LLMResponse
from app.orchestrator.cancellation import TaskCancelled
from app.tools.schemas import ToolName

__all__ = [
    "CoordinatorAgent",
    "CoordinatorResult",
    "DispatchDecision",
    "Specialist",
    "parse_dispatch",
]

#: Tools a specialist may call. Everything that can mutate the workspace or
#: the git history is deliberately excluded, so parallel reads stay safe.
READ_ONLY_TOOLS = frozenset(
    {
        ToolName.FILE_READ,
        ToolName.FILE_LIST,
        ToolName.FILE_SEARCH,
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.GIT_LOG,
        ToolName.GIT_BRANCH,
    }
)

_DISPATCH_PROMPT = (
    "You are the coordinator of a team of coding agents. Given the user's "
    "goal, decide (1) which read-only specialists would add value and "
    "(2) whether the goal requires modifying the repository. Available "
    "specialists: security (vulnerability review) and performance (latency "
    "and scalability review). Both are read-only and can run in parallel. "
    "Reply with only a JSON object inside a fenced code block, like:\n"
    "```json\n"
    '{"specialists": ["security", "performance"], "needs_changes": false, '
    '"reason": "brief justification"}\n'
    "```\n"
    "specialists is a subset of the available specialists; use an empty "
    "array when none add value. needs_changes is true when the goal expects "
    "edits, tests, or commits in the repository."
)

_SYNTHESIS_PROMPT = (
    "You are a staff engineering lead. The conversation below contains a "
    "user goal, optional read-only specialist analyses (security and/or "
    "performance), and possibly a coder's changes. Produce a single, "
    "well-organized final answer for the user: a concise summary of the "
    "findings and/or changes, concrete severity-ranked issues with file "
    "references when applicable, and clear next steps. Do not invent issues "
    "the evidence does not support. Do not call any tools; just answer."
)

_SECURITY_PROMPT = (
    "You are a read-only security reviewer working inside a user's "
    "repository. Inspect the code for security vulnerabilities: injection, "
    "authentication and authorization gaps, secrets or keys in source, "
    "unsafe deserialization, path traversal, and crypto misuse. List each "
    "concrete finding with its file, line, severity (critical|high|medium|"
    "low|nit), and a suggested fix. You may only read files and git history; "
    "you must not modify anything or run mutating commands. If the code "
    "looks clean, say so explicitly."
)

_PERFORMANCE_PROMPT = (
    "You are a read-only performance reviewer working inside a user's "
    "repository. Inspect the code for performance problems: N+1 queries, "
    "unbounded loops or caches, blocking calls on async paths, missing "
    "indexes, and avoidable allocations. List each concrete finding with its "
    "file, line, expected impact, and a suggested fix. You may only read "
    "files and git history; you must not modify anything or run mutating "
    "commands. If the code looks fine, say so explicitly."
)


@dataclass(frozen=True)
class Specialist:
    """A named read-only specialist the coordinator can dispatch to.

    Attributes:
        name: Identifier used in dispatch decisions (e.g. ``security``).
        description: One-line description surfaced to the dispatch prompt.
        system_prompt: Read-only system prompt for the specialist loop.
    """

    name: str
    description: str
    system_prompt: str


SPECIALISTS: dict[str, Specialist] = {
    "security": Specialist(
        name="security",
        description="vulnerability review",
        system_prompt=_SECURITY_PROMPT,
    ),
    "performance": Specialist(
        name="performance",
        description="latency and scalability review",
        system_prompt=_PERFORMANCE_PROMPT,
    ),
}


class DispatchDecision(BaseModel):
    """What the coordinator decided to run.

    Attributes:
        specialists: Chosen read-only specialist names (subset of
            :data:`SPECIALISTS`), empty when none were selected.
        needs_changes: Whether the goal requires modifying the repository.
        reason: The model's justification, when provided.
    """

    specialists: list[str] = Field(default_factory=list)
    needs_changes: bool = False
    reason: str = ""


def parse_dispatch(text: str) -> DispatchDecision:
    """Parse a dispatch reply into a :class:`DispatchDecision`.

    Prefers a fenced or bare JSON object; unknown specialist names are
    dropped and duplicates removed. An unparseable reply falls back to
    read-only analysis (both specialists, no changes) so the coordinator
    degrades to "analyze, don't modify".
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            decision = DispatchDecision.model_validate(payload)
        except Exception:
            decision = None
        if decision is not None:
            return _coerce_decision(decision)
    return DispatchDecision(
        specialists=list(SPECIALISTS),
        reason="dispatch reply was not parseable; defaulting to read-only analysis",
    )


@dataclass
class CoordinatorResult:
    """Outcome of a coordinator run.

    Attributes:
        answer: The synthesized final answer.
        messages: The full transcript (goal, specialists, optional coder
            messages, and the synthesis) in order.
        input_tokens: Total input tokens across all LLM calls.
        output_tokens: Total output tokens across all LLM calls.
        steps: Total LLM calls made.
        decision: The dispatch decision that shaped the run.
        specialists_run: Specialists actually run, in registry order.
    """

    answer: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int
    decision: DispatchDecision
    specialists_run: tuple[str, ...]


class CoordinatorAgent:
    """Dispatch a goal to parallel read-only specialists, then act and sum up.

    Args:
        llm: The LLM provider to call.
        executor: Executor for the workspace the coordinator works in.
        max_steps: Upper bound on LLM calls per specialist and coder loop.
        max_tokens: Cap on generated tokens per LLM call.
        temperature: Sampling temperature.
        on_message: Optional callback invoked with every produced message in
            transcript order, as it is produced.
        on_token: Optional callback forwarded to the coder loop for live
            token rendering when ``stream`` is enabled.
        stream: When ``True`` the coder loop streams tokens.
        should_cancel: Optional predicate checked before each LLM call; when
            ``True`` the run raises :class:`TaskCancelled`.
        max_specialists: Cap on specialists run concurrently.
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
        max_specialists: int = 2,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._on_message = on_message
        self._on_token = on_token
        self._stream = stream
        self._should_cancel = should_cancel
        self._max_specialists = max(1, max_specialists)
        self._specialists = {
            name: LoopAgent(
                llm=llm,
                executor=executor,
                system_prompt=specialist.system_prompt,
                max_steps=max_steps,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=False,
                should_cancel=should_cancel,
                tool_names=sorted(READ_ONLY_TOOLS),
            )
            for name, specialist in SPECIALISTS.items()
        }

    async def run(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> CoordinatorResult:
        """Execute the coordinator flow for a goal and return the final result.

        The goal is seeded as the leading user message and streamed through
        ``on_message`` before anything runs, matching the other agents.
        """
        seeded = [ChatMessage(role=ChatRole.USER, content=goal), *initial_messages]
        for message in seeded:
            await self._invoke_on_message(message)

        decision, dispatch_in, dispatch_out = await self._dispatch(seeded)
        transcript = list(seeded)
        total_in = dispatch_in
        total_out = dispatch_out
        total_steps = 1  # the dispatch call

        selected = self._select_specialists(decision)
        specialists_run: list[str] = []
        if selected:
            results = await asyncio.gather(
                *(self._specialists[name].run_from(seeded) for name in selected),
                return_exceptions=True,
            )
            for name, result in zip(selected, results, strict=True):
                if isinstance(result, TaskCancelled):
                    raise result
                if isinstance(result, BaseException):
                    failure = ChatMessage(
                        role=ChatRole.ASSISTANT,
                        content=f"[specialist {name} failed: {result}]",
                    )
                    await self._invoke_on_message(failure)
                    transcript.append(failure)
                    continue
                delta = result.messages[len(seeded) :]
                for message in delta:
                    await self._invoke_on_message(message)
                transcript.extend(delta)
                total_in += result.input_tokens
                total_out += result.output_tokens
                total_steps += result.steps
                specialists_run.append(name)

        if decision.needs_changes:
            coder = CoderAgent(
                llm=self._llm,
                executor=self._executor,
                max_steps=self._max_steps,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                on_message=self._on_message,
                on_token=self._on_token,
                stream=self._stream,
                should_cancel=self._should_cancel,
            )
            coder_result = await coder.run_from(transcript)
            transcript = list(coder_result.messages)
            total_in += coder_result.input_tokens
            total_out += coder_result.output_tokens
            total_steps += coder_result.steps

        synthesis, synth_in, synth_out = await self._synthesize(transcript)
        final = ChatMessage(role=ChatRole.ASSISTANT, content=synthesis.content)
        await self._invoke_on_message(final)
        transcript.append(final)
        total_in += synth_in
        total_out += synth_out
        total_steps += 1

        return CoordinatorResult(
            answer=synthesis.content,
            messages=transcript,
            input_tokens=total_in,
            output_tokens=total_out,
            steps=total_steps,
            decision=decision,
            specialists_run=tuple(specialists_run),
        )

    def _select_specialists(self, decision: DispatchDecision) -> tuple[str, ...]:
        """Resolve a decision's specialist names to a stable, capped list."""
        selected: list[str] = []
        for name in SPECIALISTS:
            if name in decision.specialists and len(selected) < self._max_specialists:
                selected.append(name)
        return tuple(selected)

    async def _dispatch(
        self,
        messages: Sequence[ChatMessage],
    ) -> tuple[DispatchDecision, int, int]:
        """Classify the goal and return the decision plus its token usage."""
        await self._check_cancel()
        response = await self._llm.complete(
            messages,
            tools=[],
            system=_DISPATCH_PROMPT,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        return (
            parse_dispatch(response.content),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

    async def _synthesize(
        self,
        transcript: Sequence[ChatMessage],
    ) -> tuple[LLMResponse, int, int]:
        """Aggregate the accumulated transcript into a final answer."""
        await self._check_cancel()
        response = await self._llm.complete(
            transcript,
            tools=[],
            system=_SYNTHESIS_PROMPT,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        return response, response.usage.input_tokens, response.usage.output_tokens

    async def _check_cancel(self) -> None:
        if self._should_cancel is not None and self._should_cancel():
            raise TaskCancelled()

    async def _invoke_on_message(self, message: ChatMessage) -> None:
        if self._on_message is None:
            return
        result = self._on_message(message)
        if result is not None:
            await result


def _coerce_decision(decision: DispatchDecision) -> DispatchDecision:
    """Normalize a parsed decision against the specialist registry."""
    seen: set[str] = set()
    specialists: list[str] = []
    for name in decision.specialists:
        if name in SPECIALISTS and name not in seen:
            seen.add(name)
            specialists.append(name)
    return DispatchDecision(
        specialists=specialists,
        needs_changes=decision.needs_changes,
        reason=decision.reason,
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Return a JSON object embedded in ``text``, or ``None``."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
