"""Test runner tool specification.

``test_run`` executes the project's test suite inside the sandbox and returns
a structured, parsed report (pass/fail counts and one entry per failing
test). The coder and repair stages use it instead of raw ``terminal_run`` so
failures arrive machine-readable rather than buried in a transcript blob.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.schemas import ToolName, ToolSpec

MAX_TEST_COMMAND_LENGTH = 10_000


class TestRunArgs(BaseModel):
    """Run the project's test suite inside the sandbox.

    ``command`` defaults to an auto-detected command when omitted. ``framework``
    hints at the output format for parsing (``pytest``, ``jest``); it is also
    auto-detected from the command when not given.
    """

    command: str | None = Field(default=None, max_length=MAX_TEST_COMMAND_LENGTH)
    framework: str | None = Field(default=None, max_length=32)
    workdir: str = "."
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)
    confirm: bool = False


def _spec(name: ToolName, description: str, args: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=description, arguments_schema=args.model_json_schema())


SPECS: list[ToolSpec] = [
    _spec(
        ToolName.TEST_RUN,
        "Run the project's test suite in the sandbox and return a parsed "
        "report of passing and failing tests.",
        TestRunArgs,
    ),
]

MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.TEST_RUN: TestRunArgs,
}
