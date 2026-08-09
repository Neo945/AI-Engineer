"""Host-side git invocation shared by the executor and the CLI.

Git tools run on the host checkout (never inside the sandbox) via a hardened
``git`` invocation: hooks are disabled, auth prompts are suppressed, and the
system config is ignored so a workspace checkout behaves deterministically
regardless of the user's global git settings.

Commits are produced against the user's real repository so the result is a
normal local commit.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_MS = 30_000

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@dataclass
class GitOutput:
    """Result of a host-side git invocation."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


async def run_git(
    repo: Path,
    args: list[str],
    *,
    stdin_data: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    cwd: Path | None = None,
) -> GitOutput:
    """Run ``git <args>`` in ``repo`` and capture the output.

    Args:
        repo: The repository root git runs in.
        args: Git arguments, e.g. ``["status", "--short"]``. Leading ``-c``
            options (e.g. identity overrides for commits) are supported.
        stdin_data: Optional stdin payload (used for ``git commit -F -``).
        timeout_ms: Hard wall-clock budget; the process is killed on expiry.
        cwd: Override the working directory (defaults to ``repo``).

    Returns:
        Aggregated result. On timeout, ``exit_code`` is ``None`` and
        ``timed_out`` is true.
    """
    command = ["git", "-c", "core.hooksPath=/dev/null", *args]
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd or repo),
        env={**os.environ, **_GIT_ENV},
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timeout_secs = max(1.0, timeout_ms / 1000)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin_data.encode("utf-8") if stdin_data is not None else None),
            timeout=timeout_secs,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return GitOutput(
            exit_code=None,
            stdout="",
            stderr=f"git command timed out after {timeout_ms}ms",
            timed_out=True,
        )
    return GitOutput(
        exit_code=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )
