"""Unit tests for the in-process orchestrator event broker."""

from __future__ import annotations

import uuid

from app.orchestrator.broker import EventBroker
from app.orchestrator.orchestrator import EventKind, OrchestratorEvent


def _event(task_id: uuid.UUID, kind: EventKind = "started") -> OrchestratorEvent:
    return OrchestratorEvent(task_id=task_id, kind=kind, detail="goal")


async def test_publish_delivers_to_subscriber() -> None:
    broker = EventBroker()
    task_id = uuid.uuid4()
    queue = await broker.subscribe(task_id)
    event = _event(task_id)

    await broker.publish(event)

    assert await queue.get() is event


async def test_publish_is_scoped_to_task() -> None:
    broker = EventBroker()
    queue = await broker.subscribe(uuid.uuid4())

    await broker.publish(_event(uuid.uuid4()))

    assert queue.empty()


async def test_publish_fans_out_to_multiple_subscribers() -> None:
    broker = EventBroker()
    task_id = uuid.uuid4()
    first = await broker.subscribe(task_id)
    second = await broker.subscribe(task_id)
    event = _event(task_id)

    await broker.publish(event)

    assert await first.get() is event
    assert await second.get() is event


async def test_unsubscribe_stops_delivery() -> None:
    broker = EventBroker()
    task_id = uuid.uuid4()
    queue = await broker.subscribe(task_id)
    await broker.unsubscribe(task_id, queue)

    await broker.publish(_event(task_id))

    assert queue.empty()
    assert broker._subscribers == {}


async def test_unsubscribe_is_idempotent_and_cleans_empty_sets() -> None:
    broker = EventBroker()
    task_id = uuid.uuid4()
    queue = await broker.subscribe(task_id)
    await broker.unsubscribe(task_id, queue)
    await broker.unsubscribe(task_id, queue)

    assert broker._subscribers == {}


async def test_publish_without_subscribers_is_noop() -> None:
    broker = EventBroker()
    await broker.publish(_event(uuid.uuid4()))
