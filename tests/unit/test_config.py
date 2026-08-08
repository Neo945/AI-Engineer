"""Unit tests for application settings."""

from __future__ import annotations

import pytest
from pydantic_core import ValidationError

from app.core.config import Settings


def test_settings_defaults() -> None:
    """Default settings target the local development environment."""
    settings = Settings(
        _env_file=None,
        app_name="coding-agent",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
    )
    assert settings.app_name == "coding-agent"
    assert settings.api_prefix == "/api/v1"
    assert settings.debug is False
    assert settings.json_logs is True


def test_settings_reads_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override defaults."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    settings = Settings(_env_file=None)
    assert settings.log_level == "DEBUG"
    assert settings.embedding_dimension == 768


def test_settings_rejects_invalid_embedding_dimension() -> None:
    """A zero embedding dimension must fail validation."""
    with pytest.raises(ValidationError):
        Settings(embedding_dimension=0, _env_file=None)
