"""LLM client construction from application settings."""

from __future__ import annotations

from app.core.config import Settings
from app.llm.clients.anthropic import AnthropicClient
from app.llm.clients.openai import OpenAIClient
from app.llm.protocol import LLMProvider

_SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai"})


def build_llm_client(settings: Settings) -> LLMProvider:
    """Build the configured LLM provider adapter.

    Args:
        settings: Application settings. ``llm_provider`` selects the adapter
            (``anthropic`` or ``openai``); ``llm_base_url`` may route the
            OpenAI adapter to a local OpenAI-compatible backend.

    Returns:
        A ready-to-use :class:`LLMProvider` adapter.

    Raises:
        ValueError: If ``settings.llm_provider`` is not a supported provider.
    """
    provider = settings.llm_provider.strip().lower()
    if provider == "anthropic":
        return AnthropicClient(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if provider == "openai":
        return OpenAIClient(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise ValueError(
        f"unsupported llm_provider {settings.llm_provider!r}; "
        f"expected one of {sorted(_SUPPORTED_PROVIDERS)}"
    )
