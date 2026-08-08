"""Shared pytest fixtures.

Unit tests run without infrastructure. Integration tests (marked
``integration``) require PostgreSQL and Redis reachable on localhost,
which ``make up`` provisions.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Return settings pointed at the local development infrastructure."""
    return Settings(
        app_env="test",
        json_logs=False,
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://coding:coding@localhost:5432/coding_agent",
        ),
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        _env_file=None,
    )
