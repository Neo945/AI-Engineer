"""ToolExecutor: dispatch of typed tool calls to sandboxed handlers.

Wires the pure :class:`ToolRegistry` to concrete handlers. Filesystem and
git tools run on the host, strictly confined to the workspace root by
:mod:`app.executor.paths`; terminal commands run inside the sandbox
container via :class:`SandboxManager`. The executor is bound to exactly one
workspace directory, matching the orchestrator's one-session-per-workspace
model.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from app.core.config import Settings
from app.executor.git import GitOutput, run_git
from app.executor.paths import PathTraversalError, resolve_within
from app.executor.policy import CommandPolicy, CommandTier, policy_message
from app.executor.sandbox import SandboxLimits, SandboxManager, SandboxOutput
from app.tools.registry import Handler, ToolRegistry
from app.tools.schemas import ToolCall, ToolName, ToolResult
from app.tools.specs import ALL_SPECS, ARGUMENT_MODELS
from app.tools.specs.filesystem import (
    DeleteFileArgs,
    ListFilesArgs,
    MoveFileArgs,
    ReadFileArgs,
    SearchFilesArgs,
    WriteFileArgs,
)
from app.tools.specs.git import GitCommitArgs, GitDiffArgs
from app.tools.specs.terminal import TerminalRunArgs

MAX_OUTPUT_CHARS = 200_000


class ToolExecutor:
    """Executes tool calls for one workspace.

    Attributes:
        registry: The underlying registry, useful for exposing LLM-facing
            specs to the orchestrator.
    """

    def __init__(
        self,
        *,
        workspace_dir: Path,
        registry: ToolRegistry,
        sandboxes: SandboxManager,
        mount_target: str = "/workspace",
        default_timeout_ms: int = 30_000,
        policy: CommandPolicy | None = None,
    ) -> None:
        self._workspace_dir = Path(os.path.realpath(workspace_dir))
        self._registry = registry
        self._sandboxes = sandboxes
        self._mount_target = Path(mount_target)
        self._default_timeout_ms = default_timeout_ms
        self._policy = policy or CommandPolicy()
        self._register()

    @classmethod
    def build(
        cls,
        *,
        workspace_dir: Path,
        settings: Settings,
        sandboxes: SandboxManager | None = None,
    ) -> ToolExecutor:
        """Build a fully wired executor from settings.

        Args:
            workspace_dir: Host path of the workspace checkout.
            settings: Application settings (sandbox limits, image, timeout).
            sandboxes: Optional pre-built manager (useful for tests); a new
                one is created otherwise.

        Returns:
            A configured executor.
        """
        limits = SandboxLimits(
            memory_mb=settings.sandbox_memory_mb,
            cpu_nanos=settings.sandbox_cpu_nanos,
            network_enabled=settings.sandbox_network_enabled,
        )
        manager = sandboxes or SandboxManager(image=settings.executor_image, limits=limits)
        return cls(
            workspace_dir=workspace_dir,
            registry=ToolRegistry(),
            sandboxes=manager,
            default_timeout_ms=settings.sandbox_default_timeout_ms,
            policy=CommandPolicy.from_settings(settings),
        )

    @property
    def registry(self) -> ToolRegistry:
        """The tool registry backing this executor."""
        return self._registry

    @property
    def sandboxes(self) -> SandboxManager:
        """The sandbox manager handling terminal execution for this workspace."""
        return self._sandboxes

    async def execute(self, call: ToolCall) -> ToolResult:
        """Validate and dispatch a tool call, always returning a result."""
        return await self._registry.execute(call)

    def _register(self) -> None:
        table: dict[ToolName, Handler] = {
            ToolName.FILE_READ: self._read_file,
            ToolName.FILE_WRITE: self._write_file,
            ToolName.FILE_LIST: self._list_files,
            ToolName.FILE_SEARCH: self._search_files,
            ToolName.FILE_DELETE: self._delete_file,
            ToolName.FILE_MOVE: self._move_file,
            ToolName.TERMINAL_RUN: self._terminal_run,
            ToolName.GIT_STATUS: self._git_status,
            ToolName.GIT_DIFF: self._git_diff,
            ToolName.GIT_COMMIT: self._git_commit,
        }
        for spec in ALL_SPECS:
            self._registry.register(
                spec.name,
                spec.description,
                ARGUMENT_MODELS[spec.name],
                table[spec.name],
            )

    async def _read_file(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(ReadFileArgs, args)
        try:
            path = resolve_within(self._workspace_dir, arguments.path)
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        if not path.is_file():
            return self._fail(call, f"not a file: {arguments.path}")
        data, truncated = await asyncio.to_thread(_read_bytes, path, arguments.max_bytes)
        return ToolResult(
            call_id=call.id,
            tool=call.tool,
            ok=True,
            output=data.decode("utf-8", errors="replace"),
            truncated=truncated,
        )

    async def _write_file(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(WriteFileArgs, args)
        try:
            path = resolve_within(self._workspace_dir, arguments.path)
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        await asyncio.to_thread(_write_file, path, arguments.content)
        return self._ok(call, f"wrote {len(arguments.content)} bytes to {arguments.path}")

    async def _list_files(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(ListFilesArgs, args)
        try:
            base = resolve_within(self._workspace_dir, arguments.path)
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        if not base.is_dir():
            return self._fail(call, f"not a directory: {arguments.path}")
        entries = await asyncio.to_thread(
            _list_entries,
            base,
            self._workspace_dir,
            arguments.recursive,
            arguments.max_depth,
        )
        output = "\n".join(entries) if entries else "(empty)"
        return self._ok(call, output)

    async def _search_files(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(SearchFilesArgs, args)
        try:
            base = resolve_within(self._workspace_dir, arguments.path)
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        if not base.is_dir():
            return self._fail(call, f"not a directory: {arguments.path}")
        try:
            matches = await asyncio.to_thread(
                _search_glob,
                base,
                arguments.pattern,
                arguments.case_sensitive,
                arguments.max_results,
            )
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        output = "\n".join(matches) if matches else "(no matches)"
        return self._ok(call, output)

    async def _delete_file(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(DeleteFileArgs, args)
        try:
            path = resolve_within(self._workspace_dir, arguments.path)
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        if path == self._workspace_dir:
            return self._fail(call, "refusing to delete the workspace root")
        if not path.exists() and not path.is_symlink():
            return self._fail(call, f"no such file: {arguments.path}")
        try:
            await asyncio.to_thread(_delete, path, arguments.recursive)
        except ValueError as exc:
            return self._fail(call, str(exc))
        return self._ok(call, f"deleted {arguments.path}")

    async def _move_file(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(MoveFileArgs, args)
        try:
            source = resolve_within(self._workspace_dir, arguments.source)
            destination = resolve_within(self._workspace_dir, arguments.destination)
        except PathTraversalError as exc:
            return self._fail(call, str(exc))
        if not source.exists():
            return self._fail(call, f"no such file: {arguments.source}")
        if destination == self._workspace_dir:
            return self._fail(call, "refusing to move onto the workspace root")
        await asyncio.to_thread(_move, source, destination)
        return self._ok(call, f"moved {arguments.source} -> {arguments.destination}")

    async def _terminal_run(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(TerminalRunArgs, args)
        tier = self._policy.classify(arguments.command)
        if tier is CommandTier.DENY:
            return self._fail(call, policy_message(tier, arguments.command))
        if tier is CommandTier.CONFIRM and not arguments.confirm:
            return self._fail(call, policy_message(tier, arguments.command))
        timeout_ms = arguments.timeout_ms or call.timeout_ms or self._default_timeout_ms
        sandbox = await self._sandboxes.get_or_start(self._workspace_dir)
        container_workdir: str | None = None
        if arguments.workdir != ".":
            try:
                workdir_path = resolve_within(self._workspace_dir, arguments.workdir)
            except PathTraversalError as exc:
                return self._fail(call, str(exc))
            if not workdir_path.is_dir():
                return self._fail(call, f"not a directory: {arguments.workdir}")
            container_workdir = str(
                self._mount_target / workdir_path.relative_to(self._workspace_dir)
            )
        output = await sandbox.run(
            arguments.command,
            timeout_ms=timeout_ms,
            workdir=container_workdir,
        )
        return self._sandbox_result(call, output, timeout_ms)

    async def _git_status(self, call: ToolCall, args: BaseModel) -> ToolResult:
        output = await run_git(
            self._workspace_dir,
            ["status", "--short", "--branch"],
            stdin_data=None,
            timeout_ms=self._default_timeout_ms,
        )
        return self._git_result(call, output)

    async def _git_diff(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(GitDiffArgs, args)
        command = ["diff", "--no-color"]
        if arguments.ref:
            command = ["diff", "--no-color", arguments.ref]
        output = await run_git(
            self._workspace_dir,
            command,
            stdin_data=None,
            timeout_ms=self._default_timeout_ms,
        )
        return self._git_result(call, output)

    async def _git_commit(self, call: ToolCall, args: BaseModel) -> ToolResult:
        arguments = cast(GitCommitArgs, args)
        stage = await run_git(
            self._workspace_dir,
            ["add", "-A"],
            stdin_data=None,
            timeout_ms=self._default_timeout_ms,
        )
        if stage.exit_code != 0:
            return self._git_result(call, stage)
        command = ["commit"]
        if arguments.allow_empty:
            command.append("--allow-empty")
        command += ["-F", "-"]
        output = await run_git(
            self._workspace_dir,
            command,
            stdin_data=arguments.message,
            timeout_ms=self._default_timeout_ms,
        )
        return self._git_result(call, output)

    def _sandbox_result(self, call: ToolCall, output: SandboxOutput, timeout_ms: int) -> ToolResult:
        if output.timed_out:
            return ToolResult(
                call_id=call.id,
                tool=call.tool,
                ok=False,
                output=output.stdout[:MAX_OUTPUT_CHARS],
                error=f"command timed out after {timeout_ms}ms",
                exit_code=output.exit_code,
                truncated=True,
            )
        ok = output.exit_code == 0
        text = output.stdout[:MAX_OUTPUT_CHARS]
        return ToolResult(
            call_id=call.id,
            tool=call.tool,
            ok=ok,
            output=text,
            error=None if ok else (output.stderr.strip() or f"command exited {output.exit_code}"),
            exit_code=output.exit_code,
            truncated=len(output.stdout) > MAX_OUTPUT_CHARS,
        )

    def _git_result(self, call: ToolCall, output: GitOutput) -> ToolResult:
        if output.timed_out:
            return self._fail(call, output.stderr.strip() or "git command timed out")
        ok = output.exit_code == 0
        if ok:
            return ToolResult(
                call_id=call.id,
                tool=call.tool,
                ok=True,
                output=output.stdout.strip(),
                exit_code=output.exit_code,
            )
        detail = (output.stdout + output.stderr).strip() or f"git exited {output.exit_code}"
        return ToolResult(
            call_id=call.id,
            tool=call.tool,
            ok=False,
            error=detail,
            exit_code=output.exit_code,
        )

    @staticmethod
    def _ok(call: ToolCall, output: str) -> ToolResult:
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, output=output)

    @staticmethod
    def _fail(call: ToolCall, error: str) -> ToolResult:
        return ToolResult(call_id=call.id, tool=call.tool, ok=False, error=error)


def _read_bytes(path: Path, limit: int) -> tuple[bytes, bool]:
    truncated = path.stat().st_size > limit
    with path.open("rb") as handle:
        return handle.read(limit), truncated


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _delete(path: Path, recursive: bool) -> None:
    if path.is_dir() and not path.is_symlink():
        if not recursive:
            raise ValueError(f"is a directory (pass recursive=True): {path.name}")
        shutil.rmtree(path)
    else:
        path.unlink()


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _list_entries(base: Path, root: Path, recursive: bool, max_depth: int | None) -> list[str]:
    if not recursive:
        return [entry.name + ("/" if entry.is_dir() else "") for entry in sorted(base.iterdir())]
    entries: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        for entry in sorted(directory.iterdir()):
            suffix = "/" if entry.is_dir() else ""
            entries.append(os.path.relpath(entry, root) + suffix)
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and (max_depth is None or depth < max_depth)
            ):
                walk(entry, depth + 1)

    walk(base, 0)
    return entries


def _search_glob(
    base: Path,
    pattern: str,
    case_sensitive: bool,
    max_results: int,
) -> list[str]:
    if os.path.isabs(pattern) or any(part == ".." for part in pattern.split("/")):
        raise PathTraversalError(f"unsafe search pattern: {pattern!r}")
    base_real = os.path.realpath(base)
    matches: list[str] = []
    for candidate in base.glob(pattern, case_sensitive=case_sensitive):
        real = os.path.realpath(candidate)
        if real != base_real and not real.startswith(base_real + os.sep):
            raise PathTraversalError(f"search escaped the workspace: {pattern!r}")
        matches.append(os.path.relpath(real, base_real))
        if len(matches) >= max_results:
            break
    return matches
