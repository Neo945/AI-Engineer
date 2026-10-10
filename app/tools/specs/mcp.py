"""MCP bridge tool specification.

Rather than minting a :class:`~app.tools.schemas.ToolName` member per remote
tool (which would make the enum unbounded and break the persistence/contract
guarantees of the tool layer), the executor exposes a single ``mcp_call`` tool
that forwards a call to any tool published by a configured Model Context
Protocol server. The MCP manager owns discovery, so the LLM learns the
available servers/tools from the tool description and this generic argument
shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools.schemas import ToolName, ToolSpec


class MCPCallArgs(BaseModel):
    """Invoke a tool on a configured MCP server.

    Attributes:
        server: Name of the configured MCP server to route to.
        tool: Name of the tool to invoke on that server.
        arguments: Arguments forwarded to the remote tool verbatim.
    """

    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


_SPEC = ToolSpec(
    name=ToolName.MCP_CALL,
    description=(
        "Call a tool published by an external Model Context Protocol (MCP) "
        "server (e.g. GitHub, Jira, databases, cloud APIs). Use when no "
        "built-in tool covers the task. Argument `server` names the "
        "configured MCP server and `tool` names the remote tool; pass its "
        "inputs as `arguments`. Unavailable unless MCP servers are configured."
    ),
    arguments_schema=MCPCallArgs.model_json_schema(),
)

SPECS: list[ToolSpec] = [_SPEC]

MODELS: dict[ToolName, type[BaseModel]] = {ToolName.MCP_CALL: MCPCallArgs}
