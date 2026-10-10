"""Unit tests for the MCP integration (client, manager, tool, CLI)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from rich.console import Console

from app.cli.commands import cmd_mcp_list
from app.cli.context import CliContext, CliError
from app.cli.main import build_parser
from app.core.config import Settings
from app.executor.executor import ToolExecutor
from app.executor.sandbox import SandboxManager
from app.mcp import (
    MCPClient,
    MCPConfigError,
    MCPManager,
    MCPServerConfig,
    load_mcp_servers,
)
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolCall, ToolName


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


class FakeTransport:
    """In-memory MCP transport for tests."""

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        call_result: dict[str, Any] | None = None,
        fail_list: bool = False,
    ) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._tools = tools or []
        self._call_result = call_result or {"content": [{"type": "text", "text": "ok"}]}
        self._fail_list = fail_list

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "initialize":
            return {"serverInfo": {"name": "fake"}}
        if method == "tools/list":
            if self._fail_list:
                from app.mcp import MCPError

                raise MCPError("boom")
            return {"tools": self._tools}
        if method == "tools/call":
            return self._call_result
        return {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))

    async def aclose(self) -> None:
        self.closed = True


def _config(name: str = "fake") -> MCPServerConfig:
    return MCPServerConfig(name=name, command="fake-server")


def _client(transport: FakeTransport, name: str = "fake") -> MCPClient:
    return MCPClient(_config(name), transport=transport)


def _write_servers(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"servers": {"gh": {"command": "github-mcp", "args": ["--stdio"]}}}),
        encoding="utf-8",
    )
    return path


def test_load_mcp_servers_from_envelope(tmp_path: Path) -> None:
    servers = load_mcp_servers(_write_servers(tmp_path))
    assert set(servers) == {"gh"}
    assert servers["gh"].command == "github-mcp"
    assert servers["gh"].args == ["--stdio"]


def test_load_mcp_servers_bare_mapping(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"db": {"command": "db-mcp"}}), encoding="utf-8")
    servers = load_mcp_servers(path)
    assert servers["db"].command == "db-mcp"


def test_load_mcp_servers_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MCPConfigError, match="not found"):
        load_mcp_servers(tmp_path / "nope.json")


def test_load_mcp_servers_malformed(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MCPConfigError, match="invalid"):
        load_mcp_servers(path)


async def test_client_handshake_and_list_tools() -> None:
    transport = FakeTransport(
        tools=[
            {
                "name": "create_issue",
                "description": "Open an issue",
                "inputSchema": {"type": "object"},
            },
            "junk",
        ]
    )
    client = _client(transport)

    tools = await client.list_tools()

    methods = [method for method, _ in transport.requests]
    assert methods[0] == "initialize"
    assert "tools/list" in methods
    assert ("notifications/initialized", {}) in transport.notifications
    assert len(tools) == 1
    assert tools[0].server == "fake"
    assert tools[0].name == "create_issue"
    assert tools[0].input_schema == {"type": "object"}


async def test_client_caches_tools() -> None:
    transport = FakeTransport(tools=[{"name": "t", "inputSchema": {}}])
    client = _client(transport)
    await client.list_tools()
    await client.list_tools()
    assert [m for m, _ in transport.requests].count("tools/list") == 1


async def test_client_call_tool_success() -> None:
    transport = FakeTransport(call_result={"content": [{"type": "text", "text": "created #1"}]})
    result = await _client(transport).call_tool("create_issue", {"title": "x"})
    assert result.ok is True
    assert result.output == "created #1"
    assert (
        "tools/call",
        {"name": "create_issue", "arguments": {"title": "x"}},
    ) in transport.requests


async def test_client_call_tool_reports_server_error() -> None:
    transport = FakeTransport(
        call_result={"isError": True, "content": [{"type": "text", "text": "denied"}]}
    )
    result = await _client(transport).call_tool("create_issue", {})
    assert result.ok is False
    assert result.is_error is True
    assert result.error == "denied"


async def test_client_close_delegates() -> None:
    transport = FakeTransport()
    client = _client(transport)
    await client.aclose()
    assert transport.closed is True


def test_manager_from_settings_disabled() -> None:
    assert MCPManager.from_settings(_settings()) is None


def test_manager_from_settings_missing_file() -> None:
    with pytest.raises(MCPConfigError, match="mcp_servers_file"):
        MCPManager.from_settings(_settings(mcp_enabled=True))


def test_manager_from_settings_ok(tmp_path: Path) -> None:
    settings = _settings(mcp_enabled=True, mcp_servers_file=str(_write_servers(tmp_path)))
    manager = MCPManager.from_settings(settings)
    assert manager is not None
    assert manager.servers == ("gh",)


async def test_manager_lists_across_servers() -> None:
    manager = MCPManager(
        {"a": _config("a"), "b": _config("b")},
        client_factory=lambda cfg: _client(
            FakeTransport(tools=[{"name": f"{cfg.name}-tool", "inputSchema": {}}]), cfg.name
        ),
    )
    tools = await manager.list_tools()
    assert {tool.server for tool in tools} == {"a", "b"}


async def test_manager_skips_failing_server() -> None:
    manager = MCPManager(
        {"bad": _config("bad"), "good": _config("good")},
        client_factory=lambda cfg: _client(
            FakeTransport(tools=[{"name": "t", "inputSchema": {}}], fail_list=cfg.name == "bad"),
            cfg.name,
        ),
    )
    tools = await manager.list_tools()
    assert [tool.server for tool in tools] == ["good"]


async def test_manager_unknown_server_returns_error() -> None:
    manager = MCPManager({})
    result = await manager.call("nope", "t", {})
    assert result.ok is False
    assert "unknown MCP server" in (result.error or "")


async def test_manager_close_is_best_effort() -> None:
    transport = FakeTransport()
    manager = MCPManager({"a": _config("a")}, client_factory=lambda cfg: _client(transport))
    await manager.call("a", "t", {})
    await manager.aclose()
    assert transport.closed is True


def _executor(tmp_path: Path, *, mcp: MCPManager | None) -> ToolExecutor:
    return ToolExecutor(
        workspace_dir=tmp_path,
        registry=ToolRegistry(),
        sandboxes=cast(SandboxManager, object()),
        mcp=mcp,
    )


async def test_executor_routes_mcp_call(tmp_path: Path) -> None:
    manager = MCPManager(
        {"gh": _config("gh")},
        client_factory=lambda cfg: _client(
            FakeTransport(call_result={"content": [{"type": "text", "text": "done"}]}), cfg.name
        ),
    )
    executor = _executor(tmp_path, mcp=manager)

    result = await executor.execute(
        ToolCall(
            tool=ToolName.MCP_CALL,
            arguments={"server": "gh", "tool": "create_issue", "arguments": {"title": "x"}},
        )
    )

    assert result.ok is True
    assert result.output == "done"


async def test_executor_mcp_unconfigured_is_friendly(tmp_path: Path) -> None:
    executor = _executor(tmp_path, mcp=None)
    result = await executor.execute(
        ToolCall(
            tool=ToolName.MCP_CALL,
            arguments={"server": "gh", "tool": "create_issue"},
        )
    )
    assert result.ok is False
    assert "MCP is not configured" in (result.error or "")


async def test_cmd_mcp_list_disabled() -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    code = await cmd_mcp_list(ctx)
    assert code == 0
    assert "MCP is disabled" in buffer.getvalue()


async def test_cmd_mcp_list_renders_tools() -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    manager = MCPManager(
        {"gh": _config("gh")},
        client_factory=lambda cfg: _client(
            FakeTransport(
                tools=[{"name": "create_issue", "description": "Open one", "inputSchema": {}}]
            ),
            cfg.name,
        ),
    )
    code = await cmd_mcp_list(ctx, manager=manager)
    assert code == 0
    out = buffer.getvalue()
    assert "create_issue" in out
    assert "Open one" in out


async def test_cmd_mcp_list_misconfigured(tmp_path: Path) -> None:
    console, _ = _console()
    ctx = CliContext(
        console=console,
        settings=_settings(mcp_enabled=True, mcp_servers_file=str(tmp_path / "missing.json")),
    )
    with pytest.raises(CliError):
        await cmd_mcp_list(ctx)


def test_parser_mcp_list() -> None:
    args = build_parser().parse_args(["mcp", "list"])
    assert args.command == "mcp"
    assert callable(args.handler)
