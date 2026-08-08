"""Generic base repository."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import Base


class BaseRepository[ModelT: Base, IdT]:
    """Common persistence operations shared by all repositories.

    Subclasses must set ``model`` to the concrete ORM type and expose
    domain-specific query methods; raw ``select`` statements stay inside
    repositories so services never leak SQL.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, entity_id: IdT) -> ModelT | None:
        """Fetch an entity by primary key, or ``None``."""
        return await self._session.get(self.model, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        """Persist a new entity and return it with generated defaults.

        Database-generated values (id, timestamps, server defaults) are
        fetched back via RETURNING, so ``entity`` is fully populated.
        """
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        """Delete an entity from the database."""
        await self._session.delete(entity)
        await self._session.flush()
