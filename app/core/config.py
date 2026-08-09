"""Application settings.

Settings are loaded from environment variables and an optional ``.env`` file
and validated by Pydantic at startup so configuration errors fail fast.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the coding-agent backend.

    Attributes:
        app_name: Human-readable service name.
        app_env: Deployment environment (development, staging, production).
        debug: Enable SQL echo and verbose error pages.
        log_level: Root logging level.
        json_logs: Emit structured JSON logs instead of readable output.
        api_prefix: URL prefix for all API routes.
        database_url: Async SQLAlchemy URL for PostgreSQL.
        redis_url: URL for Redis (cache, rate limits, pub/sub).
        embedding_dimension: Fixed pgvector dimension. Changing this after
            vectors are inserted requires a data migration.
        executor_image: Docker image used for sandbox containers.
        sandbox_memory_mb: Memory cap per sandbox container.
        sandbox_cpu_nanos: CPU quota per sandbox container (1e9 = one vCPU).
        sandbox_network_enabled: Whether sandbox containers may reach the
            network. Keep off in production.
        sandbox_default_timeout_ms: Hard wall-clock timeout for sandboxed
            commands when a call does not override it.
        llm_provider: LLM backend to use (``anthropic`` or ``openai``).
        llm_model: Model identifier sent with every LLM request.
        llm_api_key: API key for the LLM provider. ``None`` falls back to the
            provider SDK's environment-variable handling.
        llm_base_url: Optional base URL for the LLM API. Point the OpenAI
            provider at a local OpenAI-compatible backend (vLLM, Ollama).
        llm_max_tokens: Cap on tokens generated per LLM request.
        llm_temperature: Sampling temperature for LLM requests.
        llm_timeout_seconds: Per-request timeout for LLM calls.
        task_max_attempts: Default cap on how many times a task may be run
            (retried) before it is left in its terminal state.
        pipeline_max_passes: Upper bound on rework round-trips (reviewer→
            coder, tester→coder) in the multi-agent pipeline before it
            terminates with the latest verdict.
        test_max_repairs: Upper bound on fix → re-run iterations in the
            test-and-repair loop before the tester reports FAIL.
        test_command: Override for the test command the tester runs; when
            ``None`` it is auto-detected from the workspace.
        retrieval_enabled: Feed an assembled context window into the agent
            loop at the start of each run.
        retrieval_max_chunks: Cap on chunks in a retrieved context window.
        retrieval_max_chars: Cap on total context content characters; the
            assembler keeps the highest-ranked chunks that fit the budget.
        command_policy_enabled: Classify sandboxed terminal commands against
            deny/confirm rules (destructive commands are blocked or require
            ``confirm=True``).
        command_deny_extra: Extra regex patterns, appended to the built-in
            deny list (commands blocked outright).
        command_confirm_extra: Extra regex patterns, appended to the built-in
            confirm list (commands that require ``confirm=True``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "coding-agent"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    json_logs: bool = True

    api_prefix: str = "/api/v1"

    database_url: str = "postgresql+asyncpg://coding:coding@localhost:5432/coding_agent"
    redis_url: str = "redis://localhost:6379/0"

    embedding_dimension: int = Field(default=1536, ge=1)

    executor_image: str = "coding-agent-executor:latest"
    sandbox_memory_mb: int = Field(default=512, ge=128)
    sandbox_cpu_nanos: int = Field(default=1_000_000_000, ge=100_000_000)
    sandbox_network_enabled: bool = False
    sandbox_default_timeout_ms: int = Field(default=30_000, ge=100)

    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_max_tokens: int = Field(default=4096, ge=1)
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_timeout_seconds: float = Field(default=120.0, ge=1.0)

    task_max_attempts: int = Field(default=3, ge=1)

    pipeline_max_passes: int = Field(default=2, ge=0)

    test_max_repairs: int = Field(default=2, ge=0)
    test_command: str | None = None

    retrieval_enabled: bool = True
    retrieval_max_chunks: int = Field(default=20, ge=1)
    retrieval_max_chars: int = Field(default=12_000, ge=256)

    command_policy_enabled: bool = True
    command_deny_extra: list[str] = Field(default_factory=list)
    command_confirm_extra: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Caching prevents re-parsing environment variables and the ``.env`` file
    on every access.
    """
    return Settings()
