"""Shared single-node LLM loop used by every stage agent.

A :class:`LoopAgent` is the smallest reusable unit of agent behaviour: a
ReAct-style self-loop that sends the accumulated conversation plus the tool
catalog to the LLM, executes any requested tools through the workspace
:class:`ToolExecutor`, feeds the results back, and terminates on a final
answer or the ``max_steps`` bound. Specialist agents (coder, planner,
reviewer, tester) are thin wrappers that supply a system prompt; composed
pipelines reuse the same loop for each stage, sharing one transcript.
"""

from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.executor.executor import ToolExecutor
from app.llm.messages import ChatMessage, ChatRole, ToolRequest
from app.llm.protocol import LLMProvider
from app.orchestrator.cancellation import TaskCancelled
from app.tools.schemas import ToolCall, ToolName, ToolResult

DEFAULT_MAX_STEPS = 8


class RunResult(Protocol):
    """Structural result every agent run produces.

    Both :class:`LoopResult` and the pipeline's result satisfy this, so the
    orchestrator can treat single and composed agents uniformly.
    """

    answer: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int


class AgentLike(Protocol):
    """Structural type of an agent the orchestrator can run for a goal."""

    async def run(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> RunResult: ...


class LoopState(TypedDict, total=False):
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
class LoopResult:
    """Outcome of a completed loop run.

    Attributes:
        answer: The agent's final message.
        messages: The full transcript (the seeded conversation plus every
            message produced by the loop), ready for persistence.
        input_tokens: Total input tokens across all LLM calls.
        output_tokens: Total output tokens across all LLM calls.
        steps: Number of LLM calls made.
    """

    answer: str
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    steps: int


class LoopAgent:
    """One ReAct loop bound to a single LLM provider and workspace executor.

    Args:
        llm: The LLM provider to call.
        executor: Executor for the workspace the agent works in.
        system_prompt: System prompt steering the agent's role and tools.
        max_steps: Upper bound on LLM calls per run.
        max_tokens: Cap on generated tokens per LLM call.
        temperature: Sampling temperature.
        on_message: Optional callback invoked with every produced message
            (each assistant turn and each tool result) in transcript order,
            as it is produced.
        should_cancel: Optional predicate checked at each step boundary;
            when it returns ``True`` the run raises :class:`TaskCancelled`
            and stops at the next safe point (cooperative cancellation).
    """

    def __init__(
        self,
        *,
        llm: LLMProvider,
        executor: ToolExecutor,
        system_prompt: str,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        on_message: Callable[[ChatMessage], Awaitable[None] | None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._on_message = on_message
        self._should_cancel = should_cancel
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(LoopState)
        builder.add_node("step", self._step)
        builder.add_edge(START, "step")
        builder.add_conditional_edges(
            "step",
            self._route,
            {"continue": "step", "end": END},
        )
        return builder.compile()

    async def run(
        self,
        goal: str,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> LoopResult:
        """Execute the loop for a single goal and return the final result.

        The goal is seeded as the leading user message; any
        ``initial_messages`` are appended after it. The full seeded
        conversation is streamed through ``on_message`` before the loop runs.

        Args:
            goal: The task to accomplish.
            initial_messages: Optional prior transcript to continue from.

        Returns:
            The final result including the full transcript.
        """
        seeded = [
            ChatMessage(role=ChatRole.USER, content=goal),
            *initial_messages,
        ]
        for message in seeded:
            await self._invoke_on_message(message)
        return await self.run_from(seeded)

    async def run_from(self, messages: Sequence[ChatMessage]) -> LoopResult:
        """Run the loop over an existing conversation.

        Unlike :meth:`run`, the passed-in ``messages`` are not re-emitted
        through ``on_message``; only newly produced messages are. This lets a
        composed pipeline share one transcript across stages by handing each
        stage the accumulated conversation and appending only the delta.
        """
        initial: LoopState = {
            "goal": "",
            "messages": list(messages),
            "step": 0,
            "max_steps": self._max_steps,
            "continue_loop": True,
            "final_answer": "",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        final = await self._graph.ainvoke(initial)
        return LoopResult(
            answer=final.get("final_answer", ""),
            messages=final.get("messages", []),
            input_tokens=final.get("input_tokens", 0),
            output_tokens=final.get("output_tokens", 0),
            steps=final.get("step", 0),
        )

    def _route(self, state: LoopState) -> str:
        if state.get("continue_loop") and state.get("step", 0) < state.get(
            "max_steps", DEFAULT_MAX_STEPS
        ):
            return "continue"
        return "end"

    async def _step(self, state: LoopState) -> dict[str, Any]:
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
