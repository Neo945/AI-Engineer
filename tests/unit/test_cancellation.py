"""Unit tests for the cooperative cancellation registry."""

from __future__ import annotations

import uuid

from app.orchestrator.cancellation import CancellationRegistry


async def test_unknown_task_is_not_cancelled() -> None:
    registry = CancellationRegistry()
    assert registry.is_requested(uuid.uuid4()) is False


async def test_request_cancel_sets_flag() -> None:
    registry = CancellationRegistry()
    task_id = uuid.uuid4()

    await registry.request_cancel(task_id)

    assert registry.is_requested(task_id) is True


async def test_request_cancel_before_run_is_observed_later() -> None:
    registry = CancellationRegistry()
    task_id = uuid.uuid4()

    await registry.request_cancel(task_id)

    assert registry.is_requested(task_id) is True


async def test_reset_clears_request() -> None:
    registry = CancellationRegistry()
    task_id = uuid.uuid4()
    await registry.request_cancel(task_id)

    await registry.reset(task_id)

    assert registry.is_requested(task_id) is False


async def test_reset_is_idempotent() -> None:
    registry = CancellationRegistry()
    task_id = uuid.uuid4()
    await registry.reset(task_id)
    await registry.reset(task_id)
    assert registry.is_requested(task_id) is False


async def test_discard_drops_state() -> None:
    registry = CancellationRegistry()
    task_id = uuid.uuid4()
    await registry.request_cancel(task_id)

    await registry.discard(task_id)

    assert registry.is_requested(task_id) is False


async def test_discard_is_idempotent() -> None:
    registry = CancellationRegistry()
    await registry.discard(uuid.uuid4())
    await registry.discard(uuid.uuid4())


async def test_cancels_are_scoped_per_task() -> None:
    registry = CancellationRegistry()
    first = uuid.uuid4()
    second = uuid.uuid4()

    await registry.request_cancel(first)

    assert registry.is_requested(first) is True
    assert registry.is_requested(second) is False
