"""MCP server manager: discovery and dispatch across configured servers.

Owns one :class:`~app.mcp.client.MCPClient` per configured server, connecting
lazily on first use so a broken or unused server never blocks the others.
Discovery failures are isolated (``list_tools`` skips servers that error) and
calls always return a result, matching the executor's never-raise contract.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.mcp.client import MCPClient, MCPError, MCPToolInfo, MCPToolResult
from app.mcp.config import MCPConfigError, MCPServerConfig, load_mcp_servers

__all__ = ["MCPManager"]

ClientFactory = Callable[[MCPServerConfig], MCPClient]


class MCPManager:
    """Coordinates calls to a set of MCP servers.

    Args:
        servers: Mapping of server name to configuration.
        timeout: Per-request timeout in seconds.
        client_factory: Optional factory for the per-server clients (tests
            inject clients backed by a fake transport).
    """

    def __init__(
        self,
        servers: dict[str, MCPServerConfig],
        *,
        timeout: float = 30.0,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._configs = dict(servers)
        self._timeout = timeout
        self._client_factory: ClientFactory = client_factory or (
            lambda config: MCPClient(config, timeout=timeout)
        )
        self._clients: dict[str, MCPClient] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> MCPManager | None:
        """Build a manager from settings, or ``None`` when MCP is disabled.

        Raises:
            MCPConfigError: When MCP is enabled but the server file is missing,
                empty, or malformed.
        """
        if not settings.mcp_enabled:
            return None
        if not settings.mcp_servers_file:
            raise MCPConfigError("mcp_enabled is set but mcp_servers_file is empty")
        servers = load_mcp_servers(settings.mcp_servers_file)
        if not servers:
            raise MCPConfigError(f"no MCP servers declared in {settings.mcp_servers_file}")
        return cls(servers, timeout=settings.mcp_timeout_seconds)

    @property
    def servers(self) -> tuple[str, ...]:
        """Names of the configured MCP servers."""
        return tuple(self._configs)

    def _client(self, server: str) -> MCPClient:
        client = self._clients.get(server)
        if client is not None:
            return client
        config = self._configs.get(server)
        if config is None:
            raise MCPError(f"unknown MCP server: {server}")
        client = self._client_factory(config)
        self._clients[server] = client
        return client

    async def list_tools(self) -> list[MCPToolInfo]:
        """Discover tools across all servers, skipping servers that error."""
        tools: list[MCPToolInfo] = []
        for name in self._configs:
            try:
                tools.extend(await self._client(name).list_tools())
            except MCPError:
                continue
        return tools

    async def call(self, server: str, tool: str, arguments: dict[str, object]) -> MCPToolResult:
        """Invoke ``tool`` on ``server``, returning a failed result on error."""
        try:
            client = self._client(server)
        except MCPError as exc:
            return MCPToolResult(ok=False, error=str(exc), is_error=True)
        return await client.call_tool(tool, arguments)

    async def aclose(self) -> None:
        """Close every connected client, isolating per-client failures."""
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:
                continue
        self._clients.clear()
