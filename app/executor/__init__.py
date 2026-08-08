"""Sandboxed tool execution (Phase 4).

Runs terminal commands inside ephemeral, resource-limited Docker containers
per workspace; filesystem and git tools run host-side, confined to the
workspace root. This package is the primary security boundary of the system.
"""

from __future__ import annotations

from app.executor.executor import GitOutput, ToolExecutor
from app.executor.paths import PathTraversalError, resolve_within
from app.executor.sandbox import (
    Sandbox,
    SandboxLimits,
    SandboxManager,
    SandboxOutput,
)

__all__ = [
    "GitOutput",
    "PathTraversalError",
    "Sandbox",
    "SandboxLimits",
    "SandboxManager",
    "SandboxOutput",
    "ToolExecutor",
    "resolve_within",
]
