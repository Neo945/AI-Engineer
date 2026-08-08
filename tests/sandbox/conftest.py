"""Fixtures for sandbox integration tests.

These tests exercise real Docker containers and therefore require:

1. A reachable Docker daemon (``make up`` also needs one).
2. The executor image built: ``make executor-image``.

Either prerequisite being absent skips the suite with a clear message rather
than failing every test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiodocker
import pytest
from aiodocker.exceptions import DockerError

from app.core.config import Settings
from app.executor.executor import ToolExecutor
from app.executor.sandbox import SandboxLimits, SandboxManager


@pytest.fixture
async def docker_client() -> AsyncIterator[aiodocker.Docker]:
    """Yield a Docker client, skipping if the daemon is unreachable."""
    docker = aiodocker.Docker()
    try:
        await docker.version()
    except Exception as exc:
        await docker.close()
        pytest.skip(f"Docker daemon unavailable: {exc}")
    yield docker
    await docker.close()


@pytest.fixture
async def executor_image_ready(docker_client: aiodocker.Docker, settings: Settings) -> None:
    """Skip unless the executor image is present locally."""
    try:
        await docker_client.images.inspect(settings.executor_image)
    except DockerError as exc:
        if exc.status == 404:
            pytest.skip(f"executor image missing; run 'make executor-image': {exc}")
        raise


@pytest.fixture
async def sandbox_manager(
    settings: Settings, executor_image_ready: None
) -> AsyncIterator[SandboxManager]:
    """Yield a manager configured from settings and clean up after."""
    limits = SandboxLimits(
        memory_mb=settings.sandbox_memory_mb,
        cpu_nanos=settings.sandbox_cpu_nanos,
        network_enabled=settings.sandbox_network_enabled,
    )
    manager = SandboxManager(image=settings.executor_image, limits=limits)
    yield manager
    await manager.close()


@pytest.fixture
async def executor(
    tmp_path: Path, settings: Settings, executor_image_ready: None
) -> AsyncIterator[ToolExecutor]:
    """Yield an executor bound to a fresh temporary workspace."""
    tool_executor = ToolExecutor.build(workspace_dir=tmp_path, settings=settings)
    yield tool_executor
    await tool_executor.sandboxes.close()
