"""Developer-focused CLI for the coding-agent.

The CLI is a thin adapter over the same in-process agent core the gateway
uses: it binds a workspace + session via ``engineer init``, runs goals
through the orchestrator with live tool visibility, and exposes repository
git operations. No agent logic lives here.
"""

from __future__ import annotations

from app.cli.main import main

__all__ = ["main"]
