"""Sandboxed command execution via ephemeral Docker containers.

The sandbox is the primary security boundary of the system. Terminal
commands run here: inside a short-lived, resource-capped container running
as a non-root user with no network, a read-only root filesystem, and a hard
wall-clock timeout. Only the workspace bind mount is writable.

The workspace directory is bind-mounted at a fixed container path (default
``/workspace``), so terminal commands operate on the exact checkout the
filesystem tools manage. The container user is the numeric host owner of the
workspace, preserving non-root execution while keeping the mount writable.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiodocker
from aiodocker.exceptions import DockerError

if TYPE_CHECKING:
    from aiodocker.containers import DockerContainer

_MOUNT_TARGET = "/workspace"
_TMP_SIZE = "size=128m"
_TIMEOUT_EXIT_CODES = frozenset({124, 137})


@dataclass(frozen=True)
class SandboxLimits:
    """Resource limits applied to every sandbox container.

    Attributes:
        memory_mb: Maximum container memory in megabytes.
        cpu_nanos: Maximum CPU quota in nanoseconds per second (1e9 = one
            vCPU).
        network_enabled: Allow the container to reach the network. Off by
            default; terminal commands cannot exfiltrate or probe the host
            network.
    """

    memory_mb: int = 512
    cpu_nanos: int = 1_000_000_000
    network_enabled: bool = False

    @property
    def memory_bytes(self) -> int:
        return self.memory_mb * 1024 * 1024


@dataclass
class SandboxOutput:
    """Aggregated result of a sandboxed command.

    Attributes:
        exit_code: Process exit code, or ``None`` if the sandbox itself
            failed before the command produced one.
        stdout: Merged stdout output.
        stderr: Merged stderr output.
        timed_out: Whether the command hit its hard timeout and was killed.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class Sandbox:
    """A running executor container bound to one workspace.

    Commands are serialized with a lock: concurrent ``docker exec`` calls on
    the same container share a filesystem and process tree, so running them
    in parallel could produce interleaved or corrupt state.
    """

    container: DockerContainer
    on_destroy: Callable[[Sandbox], Coroutine[Any, Any, None]] | None = None

    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def id(self) -> str:
        """Docker container id."""
        return self.container.id

    async def run(
        self,
        script: str,
        *,
        timeout_ms: int,
        workdir: str | None = None,
        on_chunk: Callable[[int, str], None] | None = None,
    ) -> SandboxOutput:
        """Run a shell script under a hard timeout and aggregate its output.

        Args:
            script: Shell script string, executed with ``sh -c``.
            timeout_ms: Hard wall-clock budget. The command is wrapped in
                the ``timeout`` utility, which SIGKILLs it when the budget
                is exhausted; a second async guard kills the container if
                Docker itself stalls.
            workdir: Container-absolute working directory, or ``None`` to
                use the container default (the workspace mount root).
            on_chunk: Optional sync callback ``(stream, text)`` invoked as
                output arrives, where ``stream`` is 1 (stdout) or 2
                (stderr).

        Returns:
            Aggregated output. If the hard timeout fired, ``timed_out`` is
            true and the container is destroyed (the manager spawns a fresh
            one on the next request).
        """
        if on_chunk is None:
            on_chunk = _noop_chunk
        seconds = max(1, math.ceil(timeout_ms / 1000))
        wrapped = ["timeout", "-s", "KILL", str(seconds), "sh", "-c", script]
        kwargs: dict[str, Any] = {"cmd": wrapped, "stdout": True, "stderr": True, "tty": False}
        if workdir is not None:
            kwargs["workdir"] = workdir

        async with self._lock:
            outer_timeout = timeout_ms / 1000 + 10
            try:
                return await asyncio.wait_for(
                    self._execute(kwargs, on_chunk),
                    timeout=outer_timeout,
                )
            except TimeoutError:
                await self._force_kill()
                if self.on_destroy is not None:
                    await self.on_destroy(self)
                return SandboxOutput(
                    exit_code=None,
                    stdout="",
                    stderr="",
                    timed_out=True,
                )
            except DockerError as exc:
                return SandboxOutput(
                    exit_code=None,
                    stdout="",
                    stderr=f"sandbox error: {exc}",
                )

    async def _execute(
        self,
        kwargs: dict[str, Any],
        on_chunk: Callable[[int, str], None],
    ) -> SandboxOutput:
        exec_instance = await self.container.exec(**kwargs)
        stream = exec_instance.start()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            while True:
                message = await stream.read_out()
                if message is None:
                    break
                text = message.data.decode("utf-8", errors="replace")
                if message.stream == 1:
                    stdout_parts.append(text)
                else:
                    stderr_parts.append(text)
                on_chunk(message.stream, text)
        finally:
            await stream.close()

        info = await exec_instance.inspect()
        exit_code = info.get("ExitCode")
        return SandboxOutput(
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            timed_out=exit_code in _TIMEOUT_EXIT_CODES,
        )

    async def close(self) -> None:
        """Remove the container. Idempotent; ignores already-gone errors."""
        with contextlib.suppress(DockerError):
            await self.container.delete(force=True)

    async def _force_kill(self) -> None:
        with contextlib.suppress(DockerError):
            await self.container.kill(signal="SIGKILL")


def _noop_chunk(_stream: int, _text: str) -> None:
    """Default chunk callback for callers that do not stream."""


class SandboxManager:
    """Owns the Docker client and the set of active sandboxes.

    One sandbox container is lazily created per workspace directory and
    reused across terminal calls. If a command times out and kills its
    container, the manager drops it and the next request spawns a fresh one,
    so callers never see a dead container.
    """

    def __init__(
        self,
        *,
        image: str,
        limits: SandboxLimits,
        mount_target: str = _MOUNT_TARGET,
    ) -> None:
        self._docker = aiodocker.Docker()
        self._image = image
        self._limits = limits
        self._mount_target = mount_target
        self._sandboxes: dict[str, Sandbox] = {}

    @property
    def limits(self) -> SandboxLimits:
        """The resource limits applied to every container."""
        return self._limits

    async def get_or_start(self, workspace_dir: Path) -> Sandbox:
        """Return the active sandbox for ``workspace_dir``, starting one if
        needed. The directory is created if it does not exist."""
        workspace_dir.mkdir(parents=True, exist_ok=True)
        key = os.path.realpath(workspace_dir)
        sandbox = self._sandboxes.get(key)
        if sandbox is not None:
            return sandbox
        container = await self._docker.containers.create(self._container_config(key))
        await container.start()
        sandbox = Sandbox(container=container)
        sandbox.on_destroy = self._drop
        self._sandboxes[key] = sandbox
        return sandbox

    async def stop(self, workspace_dir: Path) -> None:
        """Destroy the sandbox for a workspace, if one is active."""
        key = os.path.realpath(workspace_dir)
        sandbox = self._sandboxes.pop(key, None)
        if sandbox is not None:
            await sandbox.close()

    async def close(self) -> None:
        """Destroy every sandbox and release the Docker client."""
        for sandbox in self._sandboxes.values():
            await sandbox.close()
        self._sandboxes.clear()
        await self._docker.close()

    async def _drop(self, sandbox: Sandbox) -> None:
        for key, candidate in list(self._sandboxes.items()):
            if candidate is sandbox:
                del self._sandboxes[key]

    def _container_config(self, workspace_key: str) -> dict[str, Any]:
        stat = os.stat(workspace_key)
        name = "sandbox-" + hashlib.sha1(workspace_key.encode()).hexdigest()[:12]
        return {
            "Image": self._image,
            "Cmd": ["sleep", "infinity"],
            "name": name,
            "WorkingDir": self._mount_target,
            "User": f"{stat.st_uid}:{stat.st_gid}",
            "Env": ["HOME=/tmp", "PYTHONUNBUFFERED=1"],
            "Tmpfs": {
                "/tmp": _TMP_SIZE,
            },
            "HostConfig": {
                "Memory": self._limits.memory_bytes,
                "NanoCpus": self._limits.cpu_nanos,
                "ReadonlyRootfs": True,
                "NetworkMode": "none" if not self._limits.network_enabled else "default",
                "Binds": [f"{workspace_key}:{self._mount_target}"],
            },
        }
