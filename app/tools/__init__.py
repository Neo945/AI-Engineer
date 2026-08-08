"""Typed tool definitions the agents can invoke.

Tools are declared as Pydantic schemas (name, description, input/output
contracts) and are executed remotely in the executor sandbox, never inside
the orchestrator process.
"""

from __future__ import annotations

from app.tools.registry import Handler, ToolRegistry
from app.tools.schemas import ToolCall, ToolName, ToolResult, ToolSpec
from app.tools.specs import ALL_SPECS, ARGUMENT_MODELS

__all__ = [
    "ALL_SPECS",
    "ARGUMENT_MODELS",
    "Handler",
    "ToolCall",
    "ToolName",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]
