"""In-process event broker fanning orchestrator events to subscribers.

The broker is the single-process stand-in for a Redis pub/sub channel: the
orchestrator publishes every :class:`OrchestratorEvent` it emits and
subscribers (currently SSE streams) wait on an ``asyncio.Queue`` per task.
Terminal events are published like any other; callers decide when to stop.

Because the orchestrator and gateway share one process today, an in-process
broker is simpler and lossless. If the gateway and orchestrator are later
split into services, this class is replaced by Redis pub/sub with the same
interface.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.orchestrator.orchestrator import OrchestratorEvent


class EventBroker:
    """Routes :class:`OrchestratorEvent` instances to per-task subscriber queues."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[OrchestratorEvent]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, task_id: uuid.UUID) -> asyncio.Queue[OrchestratorEvent]:
        """Register a subscriber queue for ``task_id`` and return it.

        The returned queue receives every event published for the task from
        now on; the caller is responsible for calling :meth:`unsubscribe`.
        """
        queue: asyncio.Queue[OrchestratorEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(task_id, set()).add(queue)
        return queue

    async def unsubscribe(
        self,
        task_id: uuid.UUID,
        queue: asyncio.Queue[OrchestratorEvent],
    ) -> None:
        """Remove ``queue`` from ``task_id``'s subscribers (idempotent)."""
        async with self._lock:
            subscribers = self._subscribers.get(task_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(task_id, None)

    async def publish(self, event: OrchestratorEvent) -> None:
        """Deliver ``event`` to every subscriber of its task.

        Delivery is best-effort: a slow or full subscriber is dropped rather
        than blocking the orchestrator, so a lagging SSE client never stalls
        the agent.
        """
        async with self._lock:
            subscribers = set(self._subscribers.get(event.task_id, ()))
        for queue in subscribers:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)
