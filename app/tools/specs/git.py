"""Git tool specifications.

Git tools run on the host checkout via a hardened ``git`` invocation (no
hooks, no auth prompts). Commits are produced against the user's real
repository so the result is a normal local commit.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.schemas import ToolName, ToolSpec

MAX_MESSAGE_LENGTH = 10_000


class GitStatusArgs(BaseModel):
    """Show the current working-tree status. No arguments."""


class GitDiffArgs(BaseModel):
    """Show uncommitted changes, or the diff against a revision."""

    ref: str | None = Field(default=None, max_length=255)


class GitCommitArgs(BaseModel):
    """Create a commit from the current working-tree changes."""

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    allow_empty: bool = False


def _spec(name: ToolName, description: str, args: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=description, arguments_schema=args.model_json_schema())


SPECS: list[ToolSpec] = [
    _spec(ToolName.GIT_STATUS, "Show the working-tree status of the repository.", GitStatusArgs),
    _spec(
        ToolName.GIT_DIFF,
        "Show uncommitted changes or the diff against a revision.",
        GitDiffArgs,
    ),
    _spec(
        ToolName.GIT_COMMIT,
        "Stage all working-tree changes and create a commit.",
        GitCommitArgs,
    ),
]

MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.GIT_STATUS: GitStatusArgs,
    ToolName.GIT_DIFF: GitDiffArgs,
    ToolName.GIT_COMMIT: GitCommitArgs,
}
