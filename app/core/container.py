"""Dependency injection container.

A tiny, explicit composition root. It owns the long-lived resources the
application needs (async engine, session factory, redis client) and exposes
them to FastAPI via request dependencies. Keeping the wiring visible and
testable here avoids the ceremony of a third-party DI framework while still
honouring dependency inversion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.database.engine import build_async_engine
from app.database.session import create_session_factory
from app.llm.factory import build_llm_client
from app.orchestrator.broker import EventBroker
from app.orchestrator.cancellation import CancellationRegistry

if TYPE_CHECKING:
    from app.orchestrator.orchestrator import Orchestrator

_DEFAULT_POOL_SIZE: Final = 10
_DEFAULT_MAX_OVERFLOW: Final = 20


@dataclass
class Container:
    """Composition root holding all long-lived application resources.

    Attributes:
        settings: Resolved application settings.
        engine: Async SQLAlchemy engine (connection pool).
        session_factory: Factory producing request-scoped ``AsyncSession``.
        redis: Async Redis client (cache, rate limits, pub/sub).
        event_broker: In-process fan-out for orchestrator events.
        cancellations: In-process registry of pending task cancellations.
    """

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    event_broker: EventBroker = field(default_factory=EventBroker)
    cancellations: CancellationRegistry = field(default_factory=CancellationRegistry)
    _orchestrator: Orchestrator | None = field(init=False, default=None, repr=False)

    @classmethod
    def build(cls, settings: Settings | None = None) -> Container:
        """Construct a fully wired container.

        Args:
            settings: Optional settings; defaults to the cached instance.

        Returns:
            A configured container.
        """
        resolved = settings or get_settings()
        engine = build_async_engine(
            resolved,
            pool_size=_DEFAULT_POOL_SIZE,
            max_overflow=_DEFAULT_MAX_OVERFLOW,
        )
        return cls(
            settings=resolved,
            engine=engine,
            session_factory=create_session_factory(engine),
            redis=Redis.from_url(resolved.redis_url, decode_responses=True),
        )

    async def aclose(self) -> None:
        """Release all pooled resources. Idempotent."""
        await self.engine.dispose()
        await self.redis.aclose()

    def orchestrator(self) -> Orchestrator:
        """Return the shared orchestrator, building its LLM client lazily.

        The LLM client is constructed on first use so starting the application
        never requires LLM credentials; misconfiguration surfaces as a runtime
        error only when a task is actually run.
        """
        if self._orchestrator is None:
            from app.orchestrator.orchestrator import Orchestrator

            llm = build_llm_client(self.settings)
            self._orchestrator = Orchestrator(
                session_factory=self.session_factory,
                llm=llm,
                settings=self.settings,
                event_broker=self.event_broker,
                cancellations=self.cancellations,
            )
        return self._orchestrator
