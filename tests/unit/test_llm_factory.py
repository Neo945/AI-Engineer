"""Unit tests for the LLM client factory and related settings."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.llm.clients.anthropic import AnthropicClient
from app.llm.clients.openai import OpenAIClient
from app.llm.factory import build_llm_client


def test_settings_llm_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "openai"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_api_key is None
    assert settings.llm_base_url is None
    assert settings.llm_max_tokens == 4096
    assert settings.llm_temperature == 0.0
    assert settings.llm_timeout_seconds == 120.0


def test_factory_builds_openai_client() -> None:
    settings = Settings(_env_file=None, llm_provider="openai", llm_model="m", llm_api_key="k")
    client = build_llm_client(settings)
    assert isinstance(client, OpenAIClient)
    assert client.model == "m"
    assert client.base_url is None


def test_factory_builds_anthropic_client() -> None:
    settings = Settings(_env_file=None, llm_provider="anthropic", llm_model="claude-x")
    client = build_llm_client(settings)
    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-x"


def test_factory_is_case_and_whitespace_insensitive() -> None:
    settings = Settings(_env_file=None, llm_provider="  OPENAI ", llm_api_key="k")
    assert isinstance(build_llm_client(settings), OpenAIClient)


def test_factory_openai_local_backend_needs_no_api_key() -> None:
    settings = Settings(
        _env_file=None, llm_provider="openai", llm_base_url="http://localhost:8000/v1"
    )
    client = build_llm_client(settings)
    assert isinstance(client, OpenAIClient)
    assert client.base_url == "http://localhost:8000/v1"


def test_factory_rejects_unknown_provider() -> None:
    settings = Settings(_env_file=None, llm_provider="local")
    with pytest.raises(ValueError, match="unsupported llm_provider"):
        build_llm_client(settings)
