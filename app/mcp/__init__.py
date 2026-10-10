"""Model Context Protocol (MCP) integration.

Exposes tools published by external MCP servers (GitHub, Jira, databases,
cloud APIs) to the agent through a single ``mcp_call`` bridge tool. The tool
layer stays closed and spec-based; MCP discovery and JSON-RPC transport live
here.
"""

from __future__ import annotations

from app.mcp.client import (
    MCPClient,
    MCPError,
    MCPToolInfo,
    MCPToolResult,
    MCPTransport,
    StdioTransport,
)
from app.mcp.config import MCPConfigError, MCPServerConfig, load_mcp_servers
from app.mcp.manager import MCPManager

__all__ = [
    "MCPClient",
    "MCPConfigError",
    "MCPError",
    "MCPManager",
    "MCPServerConfig",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPTransport",
    "StdioTransport",
    "load_mcp_servers",
]
