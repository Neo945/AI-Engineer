"""ReAct-style coder agent: a LangGraph single-node self-loop.

Each pass asks the LLM for the next action. If it requests tools, they are
executed through the workspace's :class:`ToolExecutor` and the results are
appended to the transcript for another pass; if it produces a final answer,
the loop ends. The loop is bounded by ``max_steps`` to guarantee termination.
"""

from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider
from app.orchestrator.cancellation import TaskCancelled
from app.tools.schemas import ToolCall, ToolName, ToolResult

_DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent working inside a user's repository. "
    "Use the provided tools to explore the code, make focused edits, run "
    "commands, and commit your work. Prefer small, verifiable changes. Run "
    "the relevant tests before finishing. When you are done, end your turn "
    "with a concise summary of what you changed and why."
)

_DEFAULT_MAX_STEPS = 8


class CoderState(TypedDict, total=False):
    """Accumulating state threaded through the LangGraph loop."""

    goal: str
    messages: Annotated[list[ChatMessage], operator.add]
    step: int
    max_steps: int
    continue_loop: bool
    final_answer: str
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]


@dataclass
class CoderResult:
    """Outcome of a completed agent run.

    Attributes:
        answer: The agent's final message.
        messages: The full transcript (user goal, assistant turns, tool
            requests and results), ready for persistence.
        input_tokens: Total input tokens across all LLM calls.
        output_tokens: Total output tokens across all LLM calls.
        steps: Number of LLM calls made.
    """

    answer: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int


class CoderAgent:
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
        max_steps: int = _DEFAULT_MAX_STEPS,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        on_message: Callable[[ChatMessage], Awaitable[None] | None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._on_message = on_message
        self._should_cancel = should_cancel
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(CoderState)
        builder.add_node("coder", self._step)
        builder.add_edge(START, "coder")
        builder.add_conditional_edges(
            "coder",
            self._route,
            {"continue": "coder", "end": END},
        )
        return builder.compile()

    async def run(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> CoderResult:
        """Execute the loop for a single goal and return the final result.

        Args:
            goal: The task to accomplish, seeded as the leading user message.
            initial_messages: Optional prior transcript to continue from,
                appended after the goal.

        Returns:
            The final result including the full transcript.
        """
        seeded = [
            ChatMessage(role=ChatRole.USER, content=goal),
            *initial_messages,
        ]
        initial: CoderState = {
            "goal": goal,
            "messages": seeded,
            "step": 0,
            "max_steps": self._max_steps,
            "continue_loop": True,
            "final_answer": "",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        for message in seeded:
            await self._invoke_on_message(message)
        final = await self._graph.ainvoke(initial)
        return CoderResult(
            answer=final.get("final_answer", ""),
            messages=final.get("messages", []),
            input_tokens=final.get("input_tokens", 0),
            output_tokens=final.get("output_tokens", 0),
            steps=final.get("step", 0),
        )

    def _route(self, state: CoderState) -> str:
        if state.get("continue_loop") and state.get("step", 0) < state.get(
            "max_steps", _DEFAULT_MAX_STEPS
        ):
            return "continue"
        return "end"

    async def _step(self, state: CoderState) -> dict[str, Any]:
        if self._should_cancel is not None and self._should_cancel():
            raise TaskCancelled()
        response = await self._llm.complete(
            state.get("messages") or [],
            tools=self._executor.registry.specs(),
            system=self._system_prompt,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        if response.tool_requests:
            assistant = ChatMessage(
                role=ChatRole.ASSISTANT,
                content=response.content,
                tool_requests=response.tool_requests,
            )
            await self._invoke_on_message(assistant)
            tool_messages = [
                await self._execute_tool(request) for request in response.tool_requests
            ]
            return {
                "messages": [assistant, *tool_messages],
                "step": state.get("step", 0) + 1,
                "continue_loop": True,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        final = ChatMessage(role=ChatRole.ASSISTANT, content=response.content)
        await self._invoke_on_message(final)
        return {
            "messages": [final],
            "step": state.get("step", 0) + 1,
            "continue_loop": False,
            "final_answer": response.content,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    async def _execute_tool(self, request: ToolRequest) -> ChatMessage:
        try:
            tool = ToolName(request.name)
        except ValueError:
            message = ChatMessage(
                role=ChatRole.TOOL,
                content=f"unknown tool: {request.name}",
                tool_call_id=request.id,
            )
            await self._invoke_on_message(message)
            return message
        result = await self._executor.execute(
            ToolCall(id=request.id, tool=tool, arguments=request.arguments)
        )
        message = ChatMessage(
            role=ChatRole.TOOL,
            content=format_tool_result(result),
            tool_call_id=result.call_id,
        )
        await self._invoke_on_message(message)
        return message

    async def _invoke_on_message(self, message: ChatMessage) -> None:
        if self._on_message is None:
            return
        result = self._on_message(message)
        if result is not None:
            await result


def format_tool_result(result: ToolResult) -> str:
    """Render a tool result as text for the model transcript."""
    parts: list[str] = []
    if result.output:
        parts.append(result.output)
    if not result.ok:
        detail = result.error or f"tool {result.tool} failed"
        parts.append(f"[error] {detail}")
    return "\n".join(parts) if parts else f"(tool {result.tool} returned no output)"
