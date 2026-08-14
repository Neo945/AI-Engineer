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
    assert settings.auth_token_ttl_seconds == 86_400
    assert settings.auth_token_issuer == "coding-agent"


def test_settings_reads_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables override defaults."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
    monkeypatch.setenv("AUTH_SECRET", "z" * 64)
    monkeypatch.setenv("AUTH_TOKEN_TTL_SECONDS", "3600")
    monkeypatch.setenv("AUTH_TOKEN_ISSUER", "other-service")
    settings = Settings(_env_file=None)
    assert settings.log_level == "DEBUG"
    assert settings.embedding_dimension == 768
    assert settings.auth_secret == "z" * 64
    assert settings.auth_token_ttl_seconds == 3600
    assert settings.auth_token_issuer == "other-service"


def test_settings_rejects_short_auth_secret() -> None:
    """A signing secret below the minimum length must fail validation."""
    with pytest.raises(ValidationError):
        Settings(auth_secret="too-short", _env_file=None)


def test_settings_rejects_zero_token_ttl() -> None:
    """A zero-length token lifetime must fail validation."""
    with pytest.raises(ValidationError):
        Settings(auth_token_ttl_seconds=0, _env_file=None)


def test_settings_rejects_invalid_embedding_dimension() -> None:
    """A zero embedding dimension must fail validation."""
    with pytest.raises(ValidationError):
        Settings(embedding_dimension=0, _env_file=None)
