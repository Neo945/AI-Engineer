"""Cooperative task cancellation primitives.

Cancellation is cooperative: the agent loop checks a per-task flag at each
step boundary (before an LLM call) and raises :class:`TaskCancelled` when a
cancel has been requested, rather than being interrupted mid-call. This keeps
sessions consistent (no torn DB commits) while still stopping long runs at
the next safe point.

The :class:`CancellationRegistry` is the single-process stand-in for a
distributed signal; like :class:`app.orchestrator.broker.EventBroker`, it can
be swapped for a shared store (Redis) if the gateway and orchestrator split.
"""

from __future__ import annotations

import asyncio
import uuid


class TaskCancelled(Exception):
    """Raised when an agent run is cancelled between steps."""


class CancellationRegistry:
    """Tracks a pending cancellation request per task id.

    The orchestrator reads :meth:`is_requested` through the agent's
    ``should_cancel`` hook at every step boundary; the cancel endpoint calls
    :meth:`request_cancel`. A fresh run (retry) calls :meth:`reset` to drop
    any request from a previous attempt, and :meth:`discard` releases the
    state once a task reaches a terminal status.
    """

    def __init__(self) -> None:
        self._events: dict[uuid.UUID, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def reset(self, task_id: uuid.UUID) -> None:
        """Clear any pending cancellation so a fresh run can proceed."""
        async with self._lock:
            self._events.pop(task_id, None)

    async def request_cancel(self, task_id: uuid.UUID) -> None:
        """Record a cancellation request for ``task_id``.

        Safe to call before a run has registered; the request stays set for
        the next run's step check.
        """
        async with self._lock:
            event = self._events.setdefault(task_id, asyncio.Event())
            event.set()

    def is_requested(self, task_id: uuid.UUID) -> bool:
        """Return whether a cancellation is pending for ``task_id``."""
        event = self._events.get(task_id)
        return event is not None and event.is_set()

    async def discard(self, task_id: uuid.UUID) -> None:
        """Drop the cancellation state for a terminal task. Idempotent."""
        async with self._lock:
            self._events.pop(task_id, None)
