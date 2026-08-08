"""Filesystem tool specifications.

Filesystem tools run on the host side of the executor, strictly confined to
the workspace root by :mod:`app.executor.paths`. They operate on the user's
real checkout so edits and builds affect the working copy the agent is
responsible for.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.schemas import ToolName, ToolSpec

MAX_READ_BYTES = 100_000
MAX_SEARCH_RESULTS = 100


class ReadFileArgs(BaseModel):
    """Read a file from the workspace."""

    path: str
    max_bytes: int = Field(default=MAX_READ_BYTES, ge=1, le=1_000_000)


class WriteFileArgs(BaseModel):
    """Create or overwrite a file in the workspace."""

    path: str
    content: str


class ListFilesArgs(BaseModel):
    """List entries under a workspace directory."""

    path: str = "."
    recursive: bool = False
    max_depth: int | None = Field(default=None, ge=0, le=20)


class SearchFilesArgs(BaseModel):
    """Find workspace files matching a glob pattern."""

    pattern: str
    path: str = "."
    case_sensitive: bool = False
    max_results: int = Field(default=MAX_SEARCH_RESULTS, ge=1, le=1000)


class DeleteFileArgs(BaseModel):
    """Delete a file or (optionally) a directory tree."""

    path: str
    recursive: bool = False


class MoveFileArgs(BaseModel):
    """Move or rename a file within the workspace."""

    source: str
    destination: str


def _spec(name: ToolName, description: str, args: type[BaseModel]) -> ToolSpec:
    return ToolSpec(name=name, description=description, arguments_schema=args.model_json_schema())


SPECS: list[ToolSpec] = [
    _spec(ToolName.FILE_READ, "Read a text file from the workspace.", ReadFileArgs),
    _spec(ToolName.FILE_WRITE, "Create or overwrite a file in the workspace.", WriteFileArgs),
    _spec(ToolName.FILE_LIST, "List files and directories under a workspace path.", ListFilesArgs),
    _spec(ToolName.FILE_SEARCH, "Search the workspace for files matching a glob.", SearchFilesArgs),
    _spec(ToolName.FILE_DELETE, "Delete a file or directory from the workspace.", DeleteFileArgs),
    _spec(ToolName.FILE_MOVE, "Move or rename a file within the workspace.", MoveFileArgs),
]

MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.FILE_READ: ReadFileArgs,
    ToolName.FILE_WRITE: WriteFileArgs,
    ToolName.FILE_LIST: ListFilesArgs,
    ToolName.FILE_SEARCH: SearchFilesArgs,
    ToolName.FILE_DELETE: DeleteFileArgs,
    ToolName.FILE_MOVE: MoveFileArgs,
}
