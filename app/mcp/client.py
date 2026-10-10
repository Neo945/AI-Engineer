"""Minimal Model Context Protocol client.

Speaks MCP over stdio using newline-delimited JSON-RPC 2.0: ``initialize``,
``notifications/initialized``, ``tools/list``, and ``tools/call``. The
transport is abstracted behind :class:`MCPTransport` so the client can be
unit-tested without spawning a subprocess, and a misbehaving server surfaces
as an :class:`MCPError` (or a failed :class:`MCPToolResult`) rather than
crashing the agent loop.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.mcp.config import MCPServerConfig

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPTransport",
    "StdioTransport",
]

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "coding-agent", "version": "0.1.0"}


class MCPError(RuntimeError):
    """Raised when an MCP server returns an error or misbehaves."""


class MCPToolInfo(BaseModel):
    """A tool advertised by an MCP server.

    Attributes:
        server: Name of the MCP server publishing the tool.
        name: Tool name as published by the server.
        description: Human-readable purpose.
        input_schema: JSON Schema for the tool's arguments.
    """

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolResult(BaseModel):
    """Outcome of an MCP ``tools/call``.

    Attributes:
        ok: Whether the call succeeded.
        output: Rendered text content returned by the tool.
        error: Error detail when ``ok`` is false.
        is_error: Whether the server itself flagged the result as an error.
    """

    ok: bool
    output: str = ""
    error: str | None = None
    is_error: bool = False


@runtime_checkable
class MCPTransport(Protocol):
    """Transport for JSON-RPC messages to an MCP server."""

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request and await its matching response."""
        ...

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no response expected)."""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...


class StdioTransport:
    """MCP transport over a subprocess's stdin/stdout pipes."""

    def __init__(self, config: MCPServerConfig, *, timeout: float = 30.0) -> None:
        self._config = config
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._process is not None:
            return self._process
        env = {**os.environ, **self._config.env}
        self._process = await asyncio.create_subprocess_exec(
            self._config.command,
            *self._config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._config.cwd,
            env=env,
        )
        return self._process

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            process = await self._ensure()
            self._id += 1
            request_id = self._id
            await self._write(
                process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            while True:
                message = await asyncio.wait_for(self._read(process), timeout=self._timeout)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise MCPError(f"{method} failed: {message['error']}")
                result = message.get("result", {})
                return result if isinstance(result, dict) else {"result": result}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        async with self._lock:
            process = await self._ensure()
            await self._write(process, {"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise MCPError("MCP server stdin is unavailable")
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _read(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        if process.stdout is None:
            raise MCPError("MCP server stdout is unavailable")
        line = await process.stdout.readline()
        if not line:
            raise MCPError("MCP server closed the connection")
        try:
            message = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPError(f"invalid MCP message: {exc}") from exc
        if not isinstance(message, dict):
            raise MCPError("invalid MCP message: expected a JSON object")
        return message

    async def aclose(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


class MCPClient:
    """Client for a single MCP server.

    Args:
        config: Server configuration (command/args/env).
        transport: Optional pre-built transport (tests inject a fake); a
            :class:`StdioTransport` is used otherwise.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        transport: MCPTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._transport: MCPTransport = transport or StdioTransport(config, timeout=timeout)
        self._connected = False
        self._tools: list[MCPToolInfo] | None = None

    @property
    def name(self) -> str:
        """The configured server name."""
        return self._config.name

    async def connect(self) -> None:
        """Perform the MCP handshake (idempotent)."""
        if self._connected:
            return
        await self._transport.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        await self._transport.notify("notifications/initialized", {})
        self._connected = True

    async def list_tools(self) -> list[MCPToolInfo]:
        """Return the server's tools, cached after the first discovery."""
        if self._tools is not None:
            return self._tools
        await self.connect()
        result = await self._transport.request("tools/list", {})
        raw = result.get("tools") or []
        tools: list[MCPToolInfo] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            schema = item.get("inputSchema") or item.get("input_schema") or {}
            tools.append(
                MCPToolInfo(
                    server=self._config.name,
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Invoke a tool on this server, always returning a result."""
        await self.connect()
        try:
            result = await self._transport.request(
                "tools/call", {"name": name, "arguments": arguments}
            )
        except MCPError as exc:
            return MCPToolResult(ok=False, error=str(exc), is_error=True)
        output = _render_content(result.get("content"))
        if bool(result.get("isError")):
            return MCPToolResult(
                ok=False,
                output=output,
                error=output or "MCP tool reported an error",
                is_error=True,
            )
        return MCPToolResult(ok=True, output=output)

    async def aclose(self) -> None:
        """Close the transport and drop cached state."""
        self._connected = False
        self._tools = None
        await self._transport.aclose()


def _render_content(content: Any) -> str:
    """Flatten an MCP content array into a single text string."""
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item))
        else:
            parts.append(str(item))
    return "\n".join(part for part in parts if part)
