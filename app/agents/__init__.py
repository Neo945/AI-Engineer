"""Specialized agents: planner, coder, reviewer, tester, debug, deploy.

Each agent isolates its own context window and evaluation, mirroring how
Cursor, Claude Code, and Devin decompose work into specialized subagents.
Phase 5 ships the coder agent; Phase 8 adds the composed multi-agent
pipeline (planner → coder → reviewer → tester).
"""

from __future__ import annotations

from app.agents.base import LoopAgent, LoopResult, format_tool_result
from app.agents.coder import CoderAgent, CoderResult
from app.agents.pipeline import PipelineAgent, PipelineResult, parse_verdict

__all__ = [
    "CoderAgent",
    "CoderResult",
    "LoopAgent",
    "LoopResult",
    "PipelineAgent",
    "PipelineResult",
    "format_tool_result",
    "parse_verdict",
]
