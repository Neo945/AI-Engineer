"""Specialized agents: planner, coder, reviewer, tester, debug, deploy.

Each agent isolates its own context window and evaluation, mirroring how
Cursor, Claude Code, and Devin decompose work into specialized subagents.
Phase 5 ships the coder agent first.
"""

from __future__ import annotations

from app.agents.coder import CoderAgent, CoderResult, format_tool_result

__all__ = ["CoderAgent", "CoderResult", "format_tool_result"]
