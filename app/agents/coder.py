"""ReAct-style coder agent: a specialized :class:`LoopAgent`.

The coder is the default single agent: a LangGraph self-loop that sends the
goal plus tool specs to the LLM, executes requested tools through the
workspace executor, feeds results back as tool messages, and terminates on a
final answer or the ``max_steps`` bound. The loop machinery itself lives in
:mod:`app.agents.base` so the planner/reviewer/tester stages of the
multi-agent pipeline share it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.agents.base import DEFAULT_MAX_STEPS, LoopAgent, LoopResult, format_tool_result
from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage
from app.llm.protocol import LLMProvider

__all__ = ["DEFAULT_MAX_STEPS", "CoderAgent", "CoderResult", "format_tool_result"]

CoderResult = LoopResult

_DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent working inside a user's repository. "
    "Use the provided tools to explore the code, make focused edits, run "
    "commands, and commit your work. Prefer small, verifiable changes. Run "
    "the relevant tests before finishing. When you are done, end your turn "
    "with a concise summary of what you changed and why."
)


class CoderAgent(LoopAgent):
    """Single coder loop bound to one LLM provider and one workspace executor.

    Args:
        llm: The LLM provider to call.
        executor: Executor for the workspace the agent works in.
        system_prompt: System prompt; defaults to a focused coding prompt.
        max_steps: Upper bound on LLM calls per run.
        max_tokens: Cap on generated tokens per LLM call.
        temperature: Sampling temperature.
        on_message: Optional callback invoked with every produced message
            (the goal, each assistant turn, and each tool result) in
            transcript order, as it is produced.
        should_cancel: Optional predicate checked at each step boundary;
            when it returns ``True`` the run raises :class:`TaskCancelled`
            and stops at the next safe point (cooperative cancellation).
    """

    def __init__(
        self,
        *,
        llm: LLMProvider,
        executor: ToolExecutor,
        system_prompt: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        on_message: Callable[[ChatMessage], Awaitable[None] | None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            llm=llm,
            executor=executor,
            system_prompt=system_prompt or _DEFAULT_SYSTEM_PROMPT,
            max_steps=max_steps,
            max_tokens=max_tokens,
            temperature=temperature,
            on_message=on_message,
            should_cancel=should_cancel,
        )
