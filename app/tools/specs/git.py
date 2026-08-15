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


class GitLogArgs(BaseModel):
    """Show the recent commit history as one line per commit."""

    limit: int = Field(default=20, ge=1, le=200)


class GitBranchArgs(BaseModel):
    """List branches, optionally creating a new one and checking it out."""

    create: str | None = Field(default=None, max_length=255)
    all: bool = False


class GitCheckoutArgs(BaseModel):
    """Switch the working tree to an existing branch."""

    branch: str = Field(min_length=1, max_length=255)


class GitPushArgs(BaseModel):
    """Push the current branch (or a named one) to a remote."""

    remote: str = Field(default="origin", min_length=1, max_length=255)
    branch: str | None = Field(default=None, max_length=255)
    force: bool = False
    set_upstream: bool = True


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
    _spec(
        ToolName.GIT_LOG,
        "Show the recent commit history as one line per commit.",
        GitLogArgs,
    ),
    _spec(
        ToolName.GIT_BRANCH,
        "List branches, or create a new branch and check it out.",
        GitBranchArgs,
    ),
    _spec(
        ToolName.GIT_CHECKOUT,
        "Switch the working tree to an existing branch.",
        GitCheckoutArgs,
    ),
    _spec(
        ToolName.GIT_PUSH,
        "Push the current branch (or a named one) to a remote.",
        GitPushArgs,
    ),
]

MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.GIT_STATUS: GitStatusArgs,
    ToolName.GIT_DIFF: GitDiffArgs,
    ToolName.GIT_COMMIT: GitCommitArgs,
    ToolName.GIT_LOG: GitLogArgs,
    ToolName.GIT_BRANCH: GitBranchArgs,
    ToolName.GIT_CHECKOUT: GitCheckoutArgs,
    ToolName.GIT_PUSH: GitPushArgs,
}
