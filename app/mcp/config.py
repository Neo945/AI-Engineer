"""MCP server configuration.

MCP servers are declared in a JSON file so operators can register external
tool sources (GitHub, Jira, databases, cloud APIs) without code changes. The
loader accepts the common ``{"servers": {...}}`` / ``{"mcpServers": {...}}``
envelopes as well as a bare ``{name: {...}}`` mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

__all__ = ["MCPConfigError", "MCPServerConfig", "load_mcp_servers"]


class MCPConfigError(ValueError):
    """Raised when the MCP server file is missing or malformed."""


class MCPServerConfig(BaseModel):
    """One external MCP server to connect to.

    Attributes:
        name: Stable identifier used by the ``mcp_call`` tool to route calls.
        command: Executable that speaks MCP over stdio.
        args: Command-line arguments for ``command``.
        env: Extra environment variables for the server process.
        cwd: Optional working directory for the server process.
    """

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


def load_mcp_servers(path: str | Path) -> dict[str, MCPServerConfig]:
    """Load and validate MCP server definitions from a JSON file.

    Args:
        path: Path to the JSON server file.

    Returns:
        A mapping of server name to its validated configuration.

    Raises:
        MCPConfigError: If the file is missing, not JSON, or malformed.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise MCPConfigError(f"MCP servers file not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConfigError(f"invalid MCP servers file {file_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise MCPConfigError(f"MCP servers file {file_path} must contain a JSON object")

    servers_raw = raw.get("servers", raw.get("mcpServers", raw))
    if not isinstance(servers_raw, dict):
        raise MCPConfigError(f"MCP servers file {file_path} must map names to servers")

    servers: dict[str, MCPServerConfig] = {}
    for name, entry in servers_raw.items():
        if not isinstance(entry, dict):
            raise MCPConfigError(f"MCP server {name!r} must be a JSON object")
        try:
            servers[str(name)] = MCPServerConfig.model_validate({"name": str(name), **entry})
        except Exception as exc:
            raise MCPConfigError(f"invalid MCP server {name!r}: {exc}") from exc
    return servers
