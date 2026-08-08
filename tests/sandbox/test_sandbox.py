"""Integration tests for the sandbox container itself.

These prove the security properties of the boundary: non-root execution,
read-only root filesystem, disabled network, enforced resource limits,
hard timeouts, and a writable workspace mount shared with the host.
"""

from __future__ import annotations

from pathlib import Path

import aiodocker
import pytest
from aiodocker.exceptions import DockerError

from app.executor.sandbox import SandboxManager

pytestmark = pytest.mark.integration


async def test_container_runs_as_non_root(sandbox_manager: SandboxManager, tmp_path: Path) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("id -u", timeout_ms=5000)
    assert output.exit_code == 0
    assert output.stdout.strip() != "0"


async def test_resource_limits_are_applied(sandbox_manager: SandboxManager, tmp_path: Path) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    info = await sandbox.container.show()
    host = info["HostConfig"]
    assert host["Memory"] == sandbox_manager.limits.memory_bytes
    assert host["NanoCpus"] == sandbox_manager.limits.cpu_nanos
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == "none"


async def test_root_filesystem_is_readonly(sandbox_manager: SandboxManager, tmp_path: Path) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("touch /sandbox-root-test", timeout_ms=5000)
    assert output.exit_code != 0


async def test_network_is_disabled(sandbox_manager: SandboxManager, tmp_path: Path) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("curl -sS --max-time 2 http://example.com", timeout_ms=8000)
    assert output.exit_code != 0


async def test_workspace_mount_is_shared_with_host(
    sandbox_manager: SandboxManager, tmp_path: Path
) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("pwd", timeout_ms=5000)
    assert output.stdout.strip() == "/workspace"

    output = await sandbox.run("echo data > created.txt && cat created.txt", timeout_ms=5000)
    assert output.exit_code == 0
    assert (tmp_path / "created.txt").read_text() == "data\n"


async def test_stdout_and_stderr_are_separated(
    sandbox_manager: SandboxManager, tmp_path: Path
) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("echo out && echo err 1>&2", timeout_ms=5000)
    assert output.exit_code == 0
    assert output.stdout.strip() == "out"
    assert output.stderr.strip() == "err"


async def test_hard_timeout_does_not_poison_sandbox(
    sandbox_manager: SandboxManager, tmp_path: Path
) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("sleep 30", timeout_ms=1000)
    assert output.timed_out is True

    again = await sandbox.run("echo alive", timeout_ms=5000)
    assert again.exit_code == 0
    assert "alive" in again.stdout


async def test_exec_workdir(sandbox_manager: SandboxManager, tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    output = await sandbox.run("pwd", timeout_ms=5000, workdir="/workspace/sub")
    assert output.stdout.strip() == "/workspace/sub"


async def test_stop_then_recreate(sandbox_manager: SandboxManager, tmp_path: Path) -> None:
    first = await sandbox_manager.get_or_start(tmp_path)
    await sandbox_manager.stop(tmp_path)
    second = await sandbox_manager.get_or_start(tmp_path)
    assert second.id != first.id


async def test_close_removes_containers(
    sandbox_manager: SandboxManager, tmp_path: Path, docker_client: aiodocker.Docker
) -> None:
    sandbox = await sandbox_manager.get_or_start(tmp_path)
    container_id = sandbox.id
    await sandbox_manager.close()
    with pytest.raises(DockerError):
        await docker_client.containers.get(container_id)
