"""Tool registry: validated dispatch of tool calls to handlers.

The registry is a pure contract layer: it knows nothing about the sandbox,
the filesystem, or Docker. Handlers are injected at wiring time, which keeps
the registry trivially unit-testable and lets the executor swap in different
backends (e.g. a mock for evals) without changing the contract.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec

Handler = Callable[[ToolCall, BaseModel], Awaitable[ToolResult]]


@dataclass
class _RegisteredTool:
    """Internal bookkeeping for a registered tool."""

    spec: ToolSpec
    validator: Callable[[dict[str, Any]], BaseModel]
    handler: Handler


class ToolRegistry:
    """Maps :class:`ToolName` values to specs, validators, and handlers.

    Tools are registered with a Pydantic arguments model that doubles as the
    source of the LLM-facing JSON Schema and the runtime argument validator,
    so a single declaration stays the source of truth.
    """

    def __init__(self) -> None:
        self._tools: dict[ToolName, _RegisteredTool] = {}

    def register(
        self,
        name: ToolName,
        description: str,
        arguments_model: type[BaseModel],
        handler: Handler,
    ) -> None:
        """Register a tool.

        Args:
            name: The tool identifier.
            description: Human-readable purpose for the LLM.
            arguments_model: Pydantic model describing accepted arguments.
            handler: Async callable ``(call, validated_args) -> ToolResult``.
        """
        adapter = TypeAdapter(arguments_model)
        spec = ToolSpec(
            name=name,
            description=description,
            arguments_schema=arguments_model.model_json_schema(),
        )
        self._tools[name] = _RegisteredTool(
            spec=spec,
            validator=adapter.validate_python,
            handler=handler,
        )

    def unregister(self, name: ToolName) -> None:
        """Remove a tool. Idempotent."""
        self._tools.pop(name, None)

    def __contains__(self, name: ToolName) -> bool:
        return name in self._tools

    @property
    def names(self) -> frozenset[ToolName]:
        """All registered tool names."""
        return frozenset(self._tools)

    def specs(self) -> list[ToolSpec]:
        """The LLM-facing specs of every registered tool."""
        return [tool.spec for tool in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """Validate and dispatch a call, always returning a result.

        Failures (unknown tool, invalid arguments, handler exceptions) are
        converted into ``ok=False`` results rather than raised, so the
        orchestrator never has to reason about executor crashes.
        """
        entry = self._tools.get(call.tool)
        if entry is None:
            return ToolResult(
                call_id=call.id,
                tool=call.tool,
                ok=False,
                error=f"unknown tool: {call.tool.value}",
            )

        try:
            arguments = entry.validator(call.arguments)
        except ValidationError as exc:
            return ToolResult(
                call_id=call.id,
                tool=call.tool,
                ok=False,
                error=f"invalid arguments: {exc}",
            )

        started = time.perf_counter()
        try:
            result = await entry.handler(call, arguments)
        except Exception as exc:
            result = ToolResult(
                call_id=call.id,
                tool=call.tool,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result
