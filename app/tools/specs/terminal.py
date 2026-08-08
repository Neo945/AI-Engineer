"""Terminal tool specification.

Terminal commands run inside the sandbox container, never on the host. The
workspace is bind-mounted into the container at a fixed path, so commands
operate on the same checkout the filesystem tools manage.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.schemas import ToolName, ToolSpec

MAX_COMMAND_LENGTH = 10_000


class TerminalRunArgs(BaseModel):
    """Execute a shell command inside the sandbox.

    ``command`` is a shell script string, executed with ``sh -c``. The
    sandbox enforces a hard wall-clock timeout regardless of this value.
    """

    command: str = Field(min_length=1, max_length=MAX_COMMAND_LENGTH)
    workdir: str = "."
    timeout_ms: int | None = Field(default=None, ge=100, le=600_000)


def _spec(name: ToolName, description: str, args: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=description, arguments_schema=args.model_json_schema())


SPECS: list[ToolSpec] = [
    _spec(
        ToolName.TERMINAL_RUN,
        "Run a shell command inside the sandbox and return its output.",
        TerminalRunArgs,
    ),
]

MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.TERMINAL_RUN: TerminalRunArgs,
}
