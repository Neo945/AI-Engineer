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
        auth_secret: Secret key used to sign access tokens. Generate a long
            random value for any non-development deployment.
        auth_token_ttl_seconds: Lifetime of an issued access token.
        auth_token_issuer: ``iss`` claim stamped on every access token.
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
        memory_enabled: Recall and inject durable project memory into the
            agent loop at the start of each run.
        memory_max_entries: Cap on memory entries injected per run.
        memory_max_chars: Cap on total memory block characters; entries are
            dropped from the bottom until the block fits.
        eval_results_path: JSONL file where headless eval results accumulate.
        eval_default_timeout_seconds: Default wall-clock cap for one eval run
            when the benchmark task does not specify its own timeout.
        eval_keep_workspaces: Keep eval scratch workspaces on disk after a
            run instead of deleting them.
        command_policy_enabled: Classify sandboxed terminal commands against
            deny/confirm rules (destructive commands are blocked or require
            ``confirm=True``).
        command_deny_extra: Extra regex patterns, appended to the built-in
            deny list (commands blocked outright).
        command_confirm_extra: Extra regex patterns, appended to the built-in
            confirm list (commands that require ``confirm=True``).
        git_protect_dirty_tree: Refuse to modify a file that already had
            uncommitted changes when the executor was created, and refuse to
            commit while such pre-existing changes are still in the working
            tree, so the agent never silently overwrites or sweeps up the
            user's in-progress edits.
        otel_enabled: Export OpenTelemetry traces and metrics when true. When
            false every monitoring call resolves to a no-op provider, so the
            instrumentation is always safe and free.
        otel_service_name: ``service.name`` resource attribute attached to
            every span and metric data point.
        otel_exporter_endpoint: OTLP/HTTP endpoint (traces and metrics).
        otel_traces_enabled: Export spans when true; metrics only otherwise.
        otel_metrics_enabled: Export metrics when true; traces only otherwise.
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

    auth_secret: str = Field(default="dev-only-secret-change-me-in-prod", min_length=32)
    auth_token_ttl_seconds: int = Field(default=86_400, ge=60)
    auth_token_issuer: str = "coding-agent"

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

    cli_stream_tokens: bool = True

    task_max_attempts: int = Field(default=3, ge=1)

    pipeline_max_passes: int = Field(default=2, ge=0)

    test_max_repairs: int = Field(default=2, ge=0)
    test_command: str | None = None

    retrieval_enabled: bool = True
    retrieval_max_chunks: int = Field(default=20, ge=1)
    retrieval_max_chars: int = Field(default=12_000, ge=256)

    memory_enabled: bool = True
    memory_max_entries: int = Field(default=20, ge=1)
    memory_max_chars: int = Field(default=4_000, ge=256)

    eval_results_path: str = ".engineer/eval-results.jsonl"
    eval_default_timeout_seconds: int = Field(default=300, ge=30)
    eval_keep_workspaces: bool = False

    command_policy_enabled: bool = True
    command_deny_extra: list[str] = Field(default_factory=list)
    command_confirm_extra: list[str] = Field(default_factory=list)

    git_protect_dirty_tree: bool = True

    otel_enabled: bool = False
    otel_service_name: str = "coding-agent"
    otel_exporter_endpoint: str = "http://localhost:4318"
    otel_traces_enabled: bool = True
    otel_metrics_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    Caching prevents re-parsing environment variables and the ``.env`` file
    on every access.
    """
    return Settings()
