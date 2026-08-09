"""Unit tests for the tool registry: validation and dispatch."""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolCall, ToolName, ToolResult
from app.tools.specs import ALL_SPECS, ARGUMENT_MODELS


class _EchoArgs(BaseModel):
    value: int


async def _echo(call: ToolCall, args: _EchoArgs) -> ToolResult:
    return ToolResult(call_id=call.id, tool=call.tool, ok=True, output=f"got {args.value}")


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolName.FILE_READ,
        "Echo the value back",
        _EchoArgs,
        lambda call, args: _echo(call, cast(_EchoArgs, args)),
    )
    return registry


async def test_execute_dispatch_and_result_metadata() -> None:
    registry = _echo_registry()
    call = ToolCall(tool=ToolName.FILE_READ, arguments={"value": 42})
    result = await registry.execute(call)
    assert result.ok is True
    assert result.output == "got 42"
    assert result.call_id == call.id
    assert result.tool == ToolName.FILE_READ
    assert result.duration_ms is not None and result.duration_ms >= 0


async def test_unknown_tool_returns_failure() -> None:
    registry = _echo_registry()
    result = await registry.execute(ToolCall(tool=ToolName.GIT_COMMIT, arguments={"message": "x"}))
    assert result.ok is False
    assert "unknown tool" in (result.error or "")


async def test_invalid_arguments_returns_failure() -> None:
    registry = _echo_registry()
    result = await registry.execute(
        ToolCall(tool=ToolName.FILE_READ, arguments={"value": "not-an-int"})
    )
    assert result.ok is False
    assert "invalid arguments" in (result.error or "")


async def test_handler_exception_is_captured() -> None:
    registry = ToolRegistry()

    async def broken(_call: ToolCall, _args: BaseModel) -> ToolResult:
        raise RuntimeError("boom")

    registry.register(ToolName.FILE_WRITE, "always fails", _EchoArgs, broken)
    result = await registry.execute(ToolCall(tool=ToolName.FILE_WRITE, arguments={"value": 1}))
    assert result.ok is False
    assert result.error is not None and "RuntimeError" in result.error


async def test_specs_expose_llm_facing_schema() -> None:
    registry = _echo_registry()
    specs = registry.specs()
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == ToolName.FILE_READ
    assert spec.description == "Echo the value back"
    assert spec.arguments_schema["type"] == "object"
    assert "value" in spec.arguments_schema["properties"]


async def test_unregister_removes_tool() -> None:
    registry = _echo_registry()
    registry.unregister(ToolName.FILE_READ)
    assert ToolName.FILE_READ not in registry
    result = await registry.execute(ToolCall(tool=ToolName.FILE_READ))
    assert result.ok is False


def test_all_specs_have_argument_models() -> None:
    spec_names = {spec.name for spec in ALL_SPECS}
    model_names = set(ARGUMENT_MODELS)
    assert spec_names == model_names


def test_every_declared_tool_has_a_spec() -> None:
    declared = {
        ToolName.FILE_READ,
        ToolName.FILE_WRITE,
        ToolName.FILE_EDIT,
        ToolName.FILE_LIST,
        ToolName.FILE_SEARCH,
        ToolName.FILE_DELETE,
        ToolName.FILE_MOVE,
        ToolName.TERMINAL_RUN,
        ToolName.TEST_RUN,
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.GIT_COMMIT,
    }
    assert declared == {spec.name for spec in ALL_SPECS}


def test_argument_models_produce_object_schemas() -> None:
    for model in ARGUMENT_MODELS.values():
        assert model.model_json_schema()["type"] == "object"
