# AI Engineer — Full Architecture & Setup Guide

> Generated from full source analysis of the `coding-agent` repository (122
> source files, 57 test files, 22 packages, Phase 20 complete).

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Principles](#2-architecture-principles)
3. [Package Map & Responsibilities](#3-package-map--responsibilities)
4. [Inter-Package Dependency Graph](#4-inter-package-dependency-graph)
5. [Deep Dive: Each Package](#5-deep-dive-each-package)
6. [Data Model](#6-data-model)
7. [Request Lifecycle Flows](#7-request-lifecycle-flows)
8. [Security Model](#8-security-model)
9. [Configuration Reference](#9-configuration-reference)
10. [API Contract](#10-api-contract)
11. [CLI Reference](#11-cli-reference)
12. [Testing Strategy](#12-testing-strategy)
13. [Setup & Deployment Guide](#13-setup--deployment-guide)
14. [Known Issues & Platform Notes](#14-known-issues--platform-notes)
15. [Roadmap](#15-roadmap)

---

## 1. System Overview

**coding-agent** is an autonomous AI software engineering agent backend.
A user gives it a natural-language goal about a Git repository (fix a bug,
add a feature, refactor); the agent reads the code, edits files, runs
commands and tests, and returns a summary of what it changed.

The system follows the architecture lineage of Cursor Agent, Claude Code,
Devin, and GitHub Copilot Workspace.

### Status at Phase 20

| Metric | Value |
|--------|-------|
| Source files | 122 |
| Test files | 57 |
| Test count | 642 (541 unit + 101 integration) |
| Lint (`ruff`) | Clean |
| Type check (`mypy --strict`) | Clean |
| DB migrations | 0001–0005 (pgvector, core schema, tool_call_id, ordinal, attempt) |
| Agent types | `coder`, `pipeline`, `coordinator` |
| Tools | 17 (filesystem 7, terminal 1, git 7, test 1, MCP 1) |
| LLM providers | Anthropic, OpenAI, Gemini, OpenAI-compatible local backends |
| CLI commands | 20+ (init, run, review, audit, graph, arch, design, analyze, modernize, memory, mcp, eval, test, commit, pr, ...) |

---

## 2. Architecture Principles

| Principle | Implementation |
|-----------|---------------|
| **Modular monolith** | One deployable with strictly separated feature packages; extractable into services later |
| **Interface-independent core** | CLI, HTTP API, and future Web UI are thin adapters over the same agent core |
| **Provider-agnostic LLM** | `LLMProvider` protocol with Anthropic/OpenAI/local adapters; agent loop never touches a provider SDK directly |
| **Defense-in-depth security** | Path confinement → sandboxed terminals (Docker) → command safety policy → dirty-tree protection |
| **Durable transcript** | Per-message persistence with immediate commit; survives restarts; source of truth for SSE streaming |
| **Offline-first retrieval** | Keyword + symbol search works immediately; pgvector semantic search is a drop-in layer |
| **Graceful degradation** | LLM report parsing degrades from JSON → prose → empty; tool failures never crash the agent |
| **Typed contracts** | Pydantic v2 models for every API boundary, tool call, LLM message, and config field |
| **Composition over inheritance** | Service classes compose repositories; agents compose `LoopAgent`; DI via `Container` dataclass |

---

## 3. Package Map & Responsibilities

| Package | Purpose | Phase |
|---------|---------|-------|
| `app/core` | Settings, structured logging, DI container (composition root) | 1 |
| `app/database` | Async SQLAlchemy engine, sessions, ORM models, repositories, Alembic migrations | 2–3 |
| `app/gateway` | FastAPI app factory, routes, request dependencies, Pydantic schemas | 1–3 |
| `app/cli` | Developer CLI (`engineer`), commands, context management | P0-A |
| `app/llm` | Provider-agnostic LLM protocol + Anthropic/OpenAI client adapters | 5a |
| `app/agents` | Shared `LoopAgent` + `CoderAgent`, `PlannerAgent`, `PipelineAgent`, `CoordinatorAgent`, `RepairAgent` | 5–8, 18 |
| `app/orchestrator` | Task state machine, event bus, cancellation registry, retry/cancel | 5–7 |
| `app/tools` | Typed tool contracts (`ToolCall`/`ToolResult`/`ToolSpec`), registry, argument models | 4 |
| `app/executor` | `ToolExecutor`, path confinement, Docker sandbox (`SandboxManager`), command safety policy, git invocation, test output parser | 4 |
| `app/retrieval` | File discovery, language detection, AST symbol extraction, chunking, keyword/symbol search, `ContextAssembler` | P0-B/C |
| `app/memory` | Durable workspace memory (fact/decision/preference/conversation), keyword recall, `MemoryService` | P0, 10 |
| `app/git` | LLM-generated commit messages and PR descriptions | 13 |
| `app/audit` | Staff-engineer production-readiness scoring (correctness/security/performance/maintainability/testability/observability) | 14 |
| `app/architecture` | Deterministic file-level dependency graph + LLM architecture analysis | 15 |
| `app/design` | System-design document generation (architecture, APIs, data model, Mermaid diagrams) | 16 |
| `app/analysis` | Distributed-systems concern scanner (sync/async HTTP, retries, idempotency, concurrency, locking, caching, timeouts, circuit breakers, messaging) + LLM interpretation | 17 |
| `app/modernization` | Legacy-modernization report generation (migration steps + risk findings) seeded by the dependency graph | 17 |
| `app/mcp` | Model Context Protocol client (stdio JSON-RPC 2.0), server config loader, and manager with lazy per-server clients | 18 |
| `app/review` | `ReviewFinding` schema (severity/file/line/problem/reason/fix), verdict extraction, findings formatting | P1 |
| `app/evals` | Headless SWE-bench-style evaluation harness (6 benchmark tasks, JSONL result store, model comparison) | 11 |
| `app/monitoring` | OpenTelemetry traces/metrics (no-op by default), `InstrumentedLLM` wrapper, HTTP middleware | 12 |

---

## 4. Inter-Package Dependency Graph

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Entry Points                                  │
│  app/cli ─────────────────┐    ┌─────────── app/gateway                │
│  (engineer CLI)           │    │  (FastAPI HTTP API)                   │
└───────────────────────────┤    ├──────────────────────────────────────┘
                            │    │
                            ▼    ▼
                    ┌──────────────────┐
                    │   app/orchestrator│ ←── app/monitoring (InstrumentedLLM)
                    │   (task runner)   │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
      ┌──────────┐  ┌──────────┐  ┌──────────────┐
      │app/agents │  │ app/llm  │  │app/executor  │
      │(LoopAgent,│  │(provider │  │(ToolExecutor,│
      │ coder,    │  │ protocol,│  │ sandbox,     │
      │ pipeline, │  │ openai,  │  │ paths, git,  │
      │ coord,    │  │ anthropic│  │ policy,      │
      │ repair,   │  │ factory) │  │ test_parser) │
      │ planner)  │  └──────────┘  └──────┬───────┘
      └─────┬────┘                        │
            │                             │
            ▼                             ▼
      ┌──────────┐                 ┌──────────────┐
      │app/tools │                 │  (host fs,    │
      │(schemas, │                 │   Docker,     │
      │ registry,│                 │   subprocess) │
      │ specs)   │                 └──────────────┘
      └──────────┘
            │
            ▼
    ┌──────────────────────────────────────────────────────────┐
    │                    app/database                          │
    │  (async SQLAlchemy, models, repositories, Alembic)       │
    │  Used by: orchestrator, gateway, CLI, memory, retrieval, │
    │           evals, analysis                                │
    └──────────────────────────────────────────────────────────┘
            │
            ▼
    ┌──────────────────┐
    │    app/core       │  ←── composition root
    │  (Settings,       │      imports: database, orchestrator.broker,
    │   Container,      │                orchestrator.cancellation
    │   logging,        │      lazy imports: orchestrator.orchestrator,
    │   security)       │                   llm.factory, monitoring.tracing
    └──────────────────┘

  ┌─────────────────────────────────────────────────┐
  │            Feature Packages (LLM-driven modes)   │
  │  app/audit    app/architecture   app/design      │
  │  app/analysis  app/modernization app/review      │
  │  app/evals     app/memory        app/retrieval   │
  │  app/git       app/mcp                           │
  └─────────────────────────────────────────────────┘
  All follow the same pattern:
    deterministic artifact → LLM prompt → parse JSON → render Markdown
```

---

## 5. Deep Dive: Each Package

### 5.1 `app/core` — Composition Root

**`config.py`** — `Settings(BaseSettings)` with ~40 env-driven fields. Loaded from `.env`, validated by Pydantic at startup. Key namespaces: `database_*`, `redis_*`, `sandbox_*`, `llm_*`, `retrieval_*`, `memory_*`, `eval_*`, `otel_*`, `command_policy_*`, `git_*`. Singleton via `get_settings()` (lru_cache).

**`logging.py`** — Structured logging via structlog. JSON lines in production; readable console in development. Configurable via `LOG_LEVEL` and `JSON_LOGS`.

**`container.py`** — `@dataclass Container` is the composition root. Holds: `Settings`, `AsyncEngine`, `async_sessionmaker`, `Redis`, `EventBroker`, `CancellationRegistry`. Lazy `orchestrator()` method builds LLM client on first use (so app boots without LLM credentials). Circular-import breaker: runtime imports of `orchestrator.orchestrator`, `llm.factory`, `monitoring.tracing`.

**`security.py`** — Password hashing (PBKDF2-HMAC-SHA256, 600k iterations) and signed access tokens (HMAC-SHA256, JWT-shaped). Constant-time comparison. No external crypto dependency.

### 5.2 `app/database` — Persistence Layer

**Engine/Session:** `build_async_engine` (asyncpg, pool_pre_ping); `create_session_factory` (expire_on_commit=False for post-commit ORM access).

**Models (7 tables):**

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `users` | Authenticated users | email (unique), hashed_password, is_active, is_superuser |
| `workspaces` | Repository bindings | owner_id (FK), repo_path, default_branch |
| `sessions` | Conversation contexts | workspace_id (FK), user_id (FK), status (enum), meta (JSONB) |
| `tasks` | Units of work | session_id (FK), parent_task_id (FK, self-ref tree), agent_type, status (enum), goal, result, error, attempt, max_attempts, input_tokens, output_tokens, plan (JSONB) |
| `messages` | Append-only transcript | session_id (FK), task_id (FK), role (enum), content, ordinal (indexed), tool_call_id, tool_calls (JSONB) |
| `code_chunks` | Retrieval index | workspace_id (FK), file_path, start_line, end_line, content, language, embedding (Vector 1536), meta (JSONB with symbols) |
| `memory_entries` | Durable memory | workspace_id (FK), kind (enum: fact/decision/preference/conversation), content, embedding (Vector 1536) |

**Mixins:** `UUIDPrimaryKeyMixin` (gen_random_uuid), `TimestampMixin` (created_at/updated_at with server defaults).

**Enums (native PG, value-stored):** SessionStatus, TaskStatus, MessageRole, MemoryKind.

**Repositories (8):** `BaseRepository` (generic get/add/delete) + concrete repos for each model. `TaskRepository.get_for_run` eager-loads session+workspace and supports `with_for_update` for atomic status guards. `MessageRepository.max_ordinal` returns `-1` for empty sessions. `MemoryRepository.keyword_search` uses `ILIKE` substring matching. `CodeChunkRepository.replace_workspace_index` does delete-then-insert in one transaction.

### 5.3 `app/gateway` — HTTP Entry Point

**`main.py`** — `create_app()` factory: builds `Container` in lifespan, wires OTel, includes routers under `/api/v1`. Module-level `app = create_app()` for uvicorn.

**`dependencies.py`** — FastAPI typed dependencies using `Annotated` aliases: `ContainerDep`, `SessionDep`, `OrchestratorDep` (503 when LLM unconfigured), `EventBrokerDep`, `CancellationRegistryDep`, `CurrentUserDep` (JWT bearer auth), `OwnedSessionDep`/`OwnedTaskDep`/`OwnedWorkspaceDep` (404/403 ownership checks).

**`schemas.py`** — Pydantic response/request models with `from_attributes=True` for ORM mode. `TaskDetailResponse` extends `TaskResponse` with ordered `messages[]`.

**Routes:**

| Method | Path | Status | Purpose |
|--------|------|--------|---------|
| GET | `/healthz` | 200 | Liveness probe |
| GET | `/readyz` | 200/503 | Readiness (Postgres + Redis) |
| POST | `/auth/register` | 201 | Create account + return token |
| POST | `/auth/login` | 200 | Authenticate + return token |
| GET | `/auth/me` | 200 | Current user profile |
| POST | `/auth/password` | 200 | Change password |
| GET | `/workspaces` | 200 | List user's workspaces |
| POST | `/workspaces` | 201 | Create workspace |
| GET | `/workspaces/{id}` | 200 | Get workspace (ownership check) |
| GET | `/workspaces/{id}/sessions` | 200 | List sessions |
| POST | `/workspaces/{id}/sessions` | 201 | Create session |
| GET | `/sessions/{id}` | 200 | Get session (ownership check) |
| POST | `/sessions/{id}/tasks` | **202** | Create + run task in background |
| GET | `/sessions/{id}/tasks` | 200 | List session's tasks |
| GET | `/tasks/{id}` | 200 | Task detail + transcript |
| POST | `/tasks/{id}/retry` | 202 | Retry terminal task |
| POST | `/tasks/{id}/cancel` | 200 | Cancel running task |
| GET | `/sessions/{id}/tasks/{id}/events` | 200 | SSE stream (replay + live) |
| GET | `/ui` | 200 | Embedded single-page web UI (served **without** the API prefix) |

**`ui.py`** — `GET /ui` serves a dependency-free single-page app (login/register,
workspaces, sessions, task creation, transcript view with polling) whose embedded
JavaScript is bound to `API_PREFIX` at request time via `render_ui(prefix)`. The
router is included without the API prefix, so the UI lives at `/ui`.

### 5.4 `app/cli` — Developer CLI

Entry point: `engineer` (defined in `pyproject.toml [project.scripts]`).

**Context management:** `app/cli/context.py` — `CliContext` holds settings, console (Rich), workspace state (`.engineer/state.json`), session id. `load_state(repo)` / `save_state()` persist workspace binding.

**Commands (20+):** `init`, `status`, `index`, `run`, `tasks`, `diff`, `commit`, `pr`, `cancel`, `review`, `audit`, `graph`, `arch`, `design`, `analyze`, `modernize`, `test`, `memory`, `mcp list`, `eval`, `version`.

**LLM plumbing:** `_build_llm(ctx)` creates provider from settings; `_llm_failure_hint` / `LLM_UNCONFIGURED_HINT` provide friendly errors; `llm_owned` pattern closes the provider in `finally`.

### 5.5 `app/llm` — Provider Abstraction

**Protocol:** `LLMProvider` — `complete(messages, tools, system, max_tokens, temperature) -> LLMResponse` and `stream(...) -> AsyncIterator[LLMStreamEvent]`.

**Messages:** `ChatMessage` (role, content, tool_call_id, tool_requests), `ChatRole` (SYSTEM/USER/ASSISTANT/TOOL), `ToolRequest` (id, name, arguments).

**Adapters:**

| Provider | System prompt | Tool calls | Key handling |
|----------|--------------|------------|--------------|
| `AnthropicClient` | Top-level `system` param | `ToolUseBlock` → `ToolRequest` | Maps stop reasons; stream via `messages.stream()` |
| `OpenAIClient` | Leading `system` message | JSON-serialized `function.arguments` | Accumulates chunks; handles local backends with placeholder key |
| `GeminiClient` | Top-level `system_instruction` | `function_call` parts → `ToolRequest` | Google GenAI SDK; maps function-call parts to tool requests |

**Factory:** `build_llm_client(settings)` dispatches on `LLM_PROVIDER` (`"anthropic"` / `"openai"` / `"gemini"`).

### 5.6 `app/agents` — Agent Loops

**`base.py` — `LoopAgent`:** The shared single-node ReAct loop. LangGraph `StateGraph(LoopState)` with one `"step"` node. Each pass: check cancellation → call LLM → if tool requests → execute via `ToolExecutor` → append results → loop. Terminates on final answer or `max_steps` (default 8). Supports `tool_names` allowlist for read-only confinement. `on_message` hook fires for every produced message.

**`coder.py` — `CoderAgent(LoopAgent)`:** Default single agent with coding system prompt.

**`planner.py` — `PlannerAgent(LoopAgent)`:** Produces structured `TaskPlan` (objective, assumptions, files, dependencies, risks, validation, steps). `parse_plan` is a tolerant parser handling `## Heading` and `Prefix: content` styles.

**`pipeline.py` — `PipelineAgent`:** LangGraph graph: `planner → coder → reviewer → tester`. Each stage runs a fresh `LoopAgent` via `run_from` (no re-emission of prior messages). Reviewer `CHANGES_NEEDED` and tester `FAIL` route back to coder. Rework bounded by `pipeline_max_passes` (default 2). `parse_verdict` extracts `VERDICT: PASS|CHANGES_NEEDED` from first line.

**`coordinator.py` — `CoordinatorAgent`:** Cheap no-tool dispatch decision → parallel read-only specialists (security, performance, confined to `READ_ONLY_TOOLS` allowlist) via `asyncio.gather` → optional sequential `CoderAgent` → synthesis. `DispatchDecision` parsed from JSON; unparseable → fallback to read-only.

**`repair.py` — `RepairAgent`:** Deterministic test-and-repair loop. Runs tests via `TEST_RUN` tool, parses structured `TestReport`, feeds failures back to LLM for fixes, re-runs tests. Bounded by `max_repairs` (default 2).

### 5.7 `app/orchestrator` — Task Lifecycle

**`orchestrator.py` — `Orchestrator`:**

Entry point: `run_task(task_id)`. Lifecycle:
1. `get_for_run(for_update=True)` — atomic row lock + status guard
2. Status → RUNNING, `attempt += 1`, set `started_at`, commit
3. Emit `started` event
4. Build executor + agent (dispatches on `task.agent_type`)
5. Assemble context (memory recall + retrieval context)
6. Run agent → `on_message` hook persists + commits each message
7. Status → COMPLETED (answer, tokens, timestamps) or FAILED (error)

Additional methods: `retry_task` (reset terminal → PENDING, clear results, keep transcript), `approve_task`, `reject_task`, `plan_task`.

**`cancellation.py` — `CancellationRegistry`:** In-process per-task cancel flags via `asyncio.Event`. Cooperative: agent checks `should_cancel` at step boundaries; raises `TaskCancelled`.

**`broker.py` — `EventBroker`:** In-process pub/sub fan-out per task. Best-effort delivery (slow subscribers dropped). Bridge to Redis pub/sub documented for scale-out.

### 5.8 `app/tools` — Tool Contracts

**`ToolName(StrEnum)`** — 17 tools: `FILE_READ`, `FILE_WRITE`, `FILE_EDIT`, `FILE_LIST`, `FILE_SEARCH`, `FILE_DELETE`, `FILE_MOVE`, `TERMINAL_RUN`, `TEST_RUN`, `GIT_STATUS`, `GIT_DIFF`, `GIT_COMMIT`, `GIT_LOG`, `GIT_BRANCH`, `GIT_CHECKOUT`, `GIT_PUSH`, `MCP_CALL`.

**`ToolRegistry`:** Maps `ToolName` → `ToolSpec` + Pydantic validator + async handler. `execute(call)` never raises — all failures become `ToolResult(ok=False)`. Measures `duration_ms`.

**Specs:** Organized in `specs/filesystem.py`, `specs/terminal.py`, `specs/git.py`, `specs/tests.py`, `specs/mcp.py`. Aggregated in `specs/__init__.py`.

### 5.9 `app/executor` — Sandboxed Execution

**`paths.py`:** `resolve_within(root, *parts)` — realpath containment check. Symlink-safe. Rejects any path escaping the workspace root.

**`sandbox.py`:** `SandboxManager` creates one Docker container per workspace (`sleep infinity`). `Sandbox.run()` wraps script in `timeout -s KILL`, acquires lock (serial execution), streams stdout/stderr. On timeout: SIGKILL + container destroy + fresh on next call. Container config: non-root (host uid:gid), read-only rootfs, no network (unless enabled), tmpfs `/tmp` (128MB), memory/CPU caps, workspace bind-mount.

**`executor.py`:** `ToolExecutor` wires 17 handlers to the `ToolRegistry`. Filesystem/git tools run on the host (via `asyncio.to_thread` / `create_subprocess_exec`), confined by `paths.py`. Terminal runs via sandbox. `MCP_CALL` dispatches through an optional `MCPManager` (`build()` wires it from settings); when MCP is unconfigured the tool returns a friendly error instead of crashing. **Dirty-tree protection:** snapshots uncommitted paths on first mutation; refuses to write/commit anything that was dirty before the session began.

**`git.py`:** Hardened git invocation: `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `GIT_CONFIG_NOSYSTEM=1`, hooks disabled. Branch name validation. `parse_porcelain` for git status.

**`policy.py`:** Command safety policy: deny-tier (rm, git reset --hard, git push --force, etc.) blocked; confirm-tier requires `confirm=true`. Extra patterns configurable via `COMMAND_DENY_EXTRA`/`COMMAND_CONFIRM_EXTRA`.

**`test_parser.py`:** Parses pytest/jest output into structured `TestReport` (passed, failed, skipped, errors, failures with file/line/message). Generic fallback for unknown frameworks.

**`patch.py`:** Unified diff parser and applier. Exact-match patching (no fuzz). Fuzzy position search (±200 lines). `apply_unified_diff` for the `edit_file` tool.

### 5.10 `app/retrieval` — Repository Intelligence

**Pipeline:** discovery → language detection → symbol extraction → chunking.

**`discovery.py`:** Walks workspace, filters by extension (Python, TS, JS, Java, Go, Rust, C, C++, C#), excludes generated/vendored dirs (node_modules, __pycache__, .git, dist, build, etc.), max file size 512KB.

**`languages.py`:** Extension→language registry. Two parsers: `ast` (Python, accurate AST) and `clike` (TS/JS/Java/Go/Rust/C/C++/C#, brace-matching heuristic).

**`symbols.py`:** `Symbol(kind, name, start_line, end_line, qualified_name)`. Python: stdlib `ast` module (class/function/method/import, nested qualification). C-like: regex-based line scan with brace-end detection.

**`chunking.py`:** Splits source at symbol boundaries. Max 60 lines, 3-line overlap. Every chunk records contained symbols.

**`retriever.py`:** `extract_query_terms` (identifiers, stopwords removed) + `extract_qualified_names` (dotted refs). `Retriever.ranked_search` merges symbol lookups (score 10.0) with keyword hits (compactness-weighted scoring). When an embedder is configured it also blends **semantic** similarity (pgvector `embedding_search` on `code_chunks`) into the ranking. De-duplicated, sorted by score.

**`embedder.py`:** `Embedder` protocol with `HashingEmbedder` (deterministic, offline — no network) and `OpenAIEmbedder` (embeddings API), plus `build_embedder(settings)` and `cosine_similarity`.

**`indexer.py`:** `RepositoryIndexer.index` — discover → extract symbols → chunk → embed (when configured) → persist via `CodeChunkRepository.replace_workspace_index` in one transaction.

**`context.py`:** `ContextAssembler.assemble(workspace_id, goal)` — extracts terms, pulls candidates (keyword + symbol + semantic when an embedder is supplied), always includes top match, trims to char budget (default 12,000 chars, 20 chunks).

### 5.11 `app/memory` — Durable Memory

`MemoryService` wraps `MemoryRepository`. Four kinds: `FACT`, `DECISION`, `PREFERENCE`, `CONVERSATION`.

- `remember(workspace_id, content, kind)` — persists entry (and its embedding when an embedder is configured)
- `recall(workspace_id, query)` — keyword search (terms ≥3 chars) ranked by compactness, blended with semantic recall when entries carry embeddings
- `format_memory_block(entries)` — renders as markdown for agent context injection

Embeddings are produced by the `app/retrieval/embedder.py` `Embedder` (`HashingEmbedder` works fully offline; `OpenAIEmbedder` calls the embeddings API).

### 5.12 `app/monitoring` — Observability

**No-op by default.** Set `OTEL_ENABLED=true` + `OTEL_EXPORTER_ENDPOINT` to activate.

**`instruments.py`:** 4 counters + 4 histograms: `llm.calls`, `llm.tokens` (input/output), `llm.duration`, `tool.calls`, `task.runs`, `task.duration`, `http.server.requests`, `http.request.duration`.

**`middleware.py`:** ASGI middleware adds server span + request metrics per HTTP request.

**`tracing.py`:** `InstrumentedLLM` decorator wraps any `LLMProvider` — emits spans + token metrics per `complete()`/`stream()` call. Manual span lifecycle for async generators.

**`telemetry.py`:** OTel bootstrap: `TracerProvider` + `MeterProvider` with OTLP/HTTP export.

### 5.13 `app/audit` — Production-Readiness Audit

`AUDIT_PROMPT` instructs the LLM as a staff engineer. Reads changes via read-only tools, scores 7 dimensions (0–100) with cited evidence, emits `AuditReport` (summary, verdict, scores, findings). `resolve_verdict` derives PASS/CHANGES_NEEDED from scores (≥70 threshold). Reuses `ReviewFinding` for uniform findings rendering.

### 5.14 `app/architecture` — Dependency Graph + Analysis

**`deps.py`:** `build_file_graph(root)` — discovers files, extracts imports (Python AST, TS/JS/Go/Rust/Java/C#/C/C++ regex), resolves each to a known file (membership-checked, no false edges). Metrics: hub files, cycles (Tarjan), orphans, layers (longest-path depth), per-language counts.

**`render.py`:** Deterministic text or Mermaid rendering. Flowchart with positional node IDs, sanitized labels.

**`report.py`:** `ARCHITECTURE_PROMPT` seeds LLM with graph summary → `ArchitectureReport` (components, layers, key files, recommendations, optional mermaid).

### 5.15 `app/design` — System Design Mode

`DESIGN_PROMPT` instructs LLM as staff architect. No workspace required. Generates `DesignReport`: summary, assumptions, architecture, components, API contracts, data model, events, caching, failure handling, scaling, observability, Mermaid diagrams (flowchart + sequence), risks. Rendering as Markdown.

### 5.16 `app/analysis` — Distributed-Systems Analysis

**`scan.py`:** Deterministic regex scanner over source files. 10 concern patterns: sync_http, async_http, retries, idempotency, concurrency, locking, caching, timeouts, circuit_breaker, messaging. Caps at 40 hits per concern, 120 chars per evidence line.

**`report.py`:** `ANALYSIS_PROMPT` seeds LLM with scan evidence → `AnalysisReport` (summary, findings using `ReviewFinding`, recommendations). `--scan-only` skips LLM.

### 5.17 `app/git` — Git Workflow Helpers

**`commit.py`:** `generate_commit_message(llm, diff)` — LLM-drafts conventional commit message (subject <72 chars + body).

**`pr.py`:** `generate_pr_description(llm, diff, commits, base, branch)` → `PRDescription` (title, summary, tests, risks, migration). `parse_pr_description` degrades from JSON → first-line title. `render_pr` outputs Markdown.

### 5.18 `app/review` — Code Review Findings

Shared across review/audit/analysis modes.

- `ReviewFinding(severity, file, line, problem, reason, fix)` — severity as `FindingSeverity` enum (critical/high/medium/low/nit)
- `parse_findings(text)` — extracts JSON array from fenced block
- `sort_findings` — severity order then file
- `format_findings` — `SEVERITY file:line — problem` with indented why/fix
- `extract_verdict(text)` — first `VERDICT:` line

### 5.19 `app/evals` — Evaluation Harness

**`tasks.py`:** 6 benchmark tasks (fix-auth-bug, add-rest-endpoint, add-db-migration, fix-failing-test, optimize-query, find-security-issue). Each scaffolds a self-contained repo with failing tests.

**`runner.py`:** `EvalRunner.run(task, workspace_dir)` — scaffold → git init → provision DB rows → orchestrator run (with timeout) → verify via `SandboxVerifier` → `EvalResultRecord`.

**`results.py`:** `ResultStore` — append-only JSONL. `summarize()` computes per-model pass rates.

### 5.20 `app/modernization` — Legacy Modernization

**`report.py`:** `MODERNIZATION_PROMPT` seeds the LLM as a modernization engineer with the workspace's dependency-graph summary (and an optional target stack). Produces a `ModernizationReport` — summary, ordered `MigrationStep`s, and risks (reusing `ReviewFinding`) — rendered as Markdown by `render_modernization`. Parsing degrades gracefully (prose → summary). Wired to `engineer modernize` (plan-only; seeded by `build_file_graph`/`graph_summary`).

### 5.21 `app/mcp` — Model Context Protocol

**`config.py`:** `MCPServerConfig` + `load_mcp_servers(path)` read a JSON/`.mcp.json`-style server list; `MCPConfigError` on malformed input.

**`client.py`:** `MCPClient` speaks JSON-RPC 2.0 over a `MCPTransport`; `StdioTransport` spawns a server subprocess and frames requests/responses on its stdio. Exposes `MCPToolInfo`/`MCPToolResult`; isolated failures surface as `MCPError`.

**`manager.py`:** `MCPManager.from_settings(settings)` builds lazily; one client per configured server; a failing server never breaks the others. The executor's `MCP_CALL` tool routes through this manager.

---

## 6. Data Model

```
users ───────────────────────────────────────────────────────┐
  │ 1:N                                                      │
workspaces ──────────────────────────────────────────────────┤
  │ 1:N                    1:N                               │
sessions ────────────── tasks ───────────────────────────────┤
  │ 1:N (ordinal)         │ self-ref (parent_task_id)       │
  └──── messages ─────────┘                                  │
                                                             │
code_chunks ──────────────────────────────── (per workspace) │
memory_entries ────────────────────────────── (per workspace)┘
```

**Key ordering rules:**
- Tasks: `created_at ASC, id ASC`
- Messages: `ordinal ASC, created_at ASC, id ASC` (ordinal assigned starting at `max_ordinal + 1`)

**Migrations:**

| Rev | Description |
|-----|-------------|
| 0001 | Enable pgvector extension |
| 0002 | Core schema (users, workspaces, sessions, tasks, messages, code_chunks, enums) |
| 0003 | `messages.tool_call_id`, `messages.tool_calls` (JSONB) |
| 0004 | `messages.ordinal` (int, indexed) for deterministic transcript ordering |
| 0005 | `tasks.attempt`, `tasks.max_attempts` for durable retry checkpoint |

---

## 7. Request Lifecycle Flows

### 7.1 Task Creation + Execution (async, SSE-streamed)

```
Client                Gateway               Orchestrator
  │ POST /tasks          │                      │
  │ {goal, agent_type}   │                      │
  ├─────────────────────▶│                      │
  │                      │ 404 if no session    │
  │                      │ create Task(PENDING) │
  │                      │ commit               │
  │                      │ BackgroundTasks.run   │
  │◀─────── 202 ────────│                      │
  │                      │                      │
  │ (client opens SSE)   │                      │
  │                      │    run_task(task_id) │
  │                      │                      │──▶ get_for_run(for_update=True)
  │                      │                      │    status=RUNNING, attempt+1
  │                      │                      │    ordinal_base = max_ordinal
  │                      │                      │
  │                      │                      │    agent.run(goal)
  │                      │                      │      LLM.call(tools) → tool_requests
  │                      │                      │      execute(tools) → results
  │                      │                      │      persist message, commit
  │                      │                      │      emit "message" → Broker
  │                      │                      │      ... loop ...
  │                      │                      │      final answer
  │                      │                      │
  │                      │                      │    status=COMPLETED
  │                      │                      │    emit "completed"
  │                      │                      │
  │◀─────── SSE events ──│── snapshot ──────────│
  │◀─────── SSE events ──│── messages ──────────│
  │◀─────── SSE events ──│── completed ─────────│
```

### 7.2 Pipeline Agent Flow

```
goal → PlannerAgent (plan) → CoderAgent (implement) → ReviewerAgent (judge)
                                                                    │
                                          PASS ──────────── ▶ TesterAgent
                                          CHANGES_NEEDED ── ▶ CoderAgent (rework)
                                                                    │
                                              PASS ────────── ▶ END
                                              FAIL ─────────── ▶ CoderAgent (rework)
                                                        (bounded by pipeline_max_passes)
```

### 7.3 Coordinator Agent Flow

```
goal → Dispatch Decision (no-tool LLM call)
         │
         ├──▶ Security Specialist (read-only, parallel)
         ├──▶ Performance Specialist (read-only, parallel)
         │
         └──▶ CoderAgent (if needs_changes, sequential)
                  │
                  └──▶ Synthesis (aggregate all findings + changes)
```

### 7.4 Cancel Flow

```
POST /tasks/{id}/cancel
  → status=CANCELLED + finished_at (committed immediately)
  → CancellationRegistry.request_cancel(task_id)
  → Agent checks should_cancel() at next step boundary
  → Raises TaskCancelled
  → Orchestrator finalizes (keeps already-committed CANCELLED)
```

### 7.5 Retry Flow

```
POST /tasks/{id}/retry  (must be terminal, attempt < max_attempts)
  → status=PENDING (clear result/error/tokens/timestamps)
  → CancellationRegistry.discard(task_id)
  → BackgroundTasks.run_task(task_id)
  → PENDING → RUNNING (attempt+1)
  → Transcript preserved; new messages append after existing ordinals
```

---

## 8. Security Model

| Threat | Control |
|--------|---------|
| Path traversal / symlink | `resolve_within()` realpath containment; rejected before host I/O |
| Malicious terminal commands | Docker sandbox: non-root, read-only rootfs, no network, memory/CPU caps, hard timeout |
| Command safety | Deny-tier blocks (rm, git reset --hard, git push --force, etc.); confirm-tier for risky commands |
| LLM key leakage | Keys only via env/.env; never logged; never committed (.gitignore) |
| Workspace escape via git | `GIT_TERMINAL_PROMPT=0`, hooks disabled, host git with bounded args |
| Output blowup | `MAX_OUTPUT_CHARS` (200k) truncation |
| Unbounded loops | `max_steps` bound + per-call `max_tokens` + LLM timeout |
| Dirty-tree protection | Snapshots uncommitted paths on first mutation; refuses to touch/commit pre-existing dirty files |
| Auth | PBKDF2-HMAC-SHA256 passwords; HMAC-SHA256 signed tokens; ownership checks on all resources |

Sandbox container: `User` = host uid/gid (mount stays writable, non-root), `Tmpfs /tmp`, `ReadonlyRootfs`, `NetworkMode: none`, single workspace bind. Timed-out commands destroy the container; fresh one on next call.

---

## 9. Configuration Reference

All via environment variables or `.env` file. Pydantic validates at startup.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_NAME` | `coding-agent` | Application name |
| `APP_ENV` | `development` | Environment |
| `DEBUG` | `false` | Enables SQL echo / verbose errors |
| `LOG_LEVEL` | `INFO` | Structured log level |
| `JSON_LOGS` | `true` | JSON vs console log format |
| `API_PREFIX` | `/api/v1` | Route prefix |

### Database

| Variable | Default |
|----------|---------|
| `DATABASE_URL` | `postgresql+asyncpg://coding:coding@localhost:5432/coding_agent` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `EMBEDDING_DIMENSION` | `1536` |

### Sandbox / Executor

| Variable | Default | Constraint |
|----------|---------|------------|
| `EXECUTOR_IMAGE` | `coding-agent-executor:latest` | — |
| `SANDBOX_MEMORY_MB` | `512` | ≥128 |
| `SANDBOX_CPU_NANOS` | `1_000_000_000` | ≥100M |
| `SANDBOX_NETWORK_ENABLED` | `false` | keep off in prod |
| `SANDBOX_DEFAULT_TIMEOUT_MS` | `30_000` | ≥100 |

### LLM

| Variable | Default | Notes |
|----------|---------|-------|
| `LLM_PROVIDER` | `openai` | `anthropic` \| `openai` \| `gemini` |
| `LLM_MODEL` | `gpt-4o-mini` | — |
| `LLM_API_KEY` | *(empty)* | Falls back to SDK env |
| `LLM_BASE_URL` | *(empty)* | For vLLM/Ollama |
| `LLM_MAX_TOKENS` | `4096` | ≥1 |
| `LLM_TEMPERATURE` | `0.0` | 0.0–2.0 |
| `LLM_TIMEOUT_SECONDS` | `120.0` | ≥1 |

### Agent Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `TASK_MAX_ATTEMPTS` | `3` | Retry budget per task |
| `PIPELINE_MAX_PASSES` | `2` | Rework rounds in pipeline |
| `COORDINATOR_MAX_SPECIALISTS` | `2` | Concurrent read-only specialists |
| `TEST_MAX_REPAIRS` | `2` | Fix→re-run iterations |
| `TEST_COMMAND` | *(empty)* | Override auto-detected runner |

### Retrieval / Memory

| Variable | Default |
|----------|---------|
| `RETRIEVAL_ENABLED` | `true` |
| `RETRIEVAL_MAX_CHUNKS` | `20` |
| `RETRIEVAL_MAX_CHARS` | `12000` |
| `MEMORY_ENABLED` | `true` |
| `MEMORY_MAX_ENTRIES` | `20` |
| `MEMORY_MAX_CHARS` | `4000` |

### Embeddings

| Variable | Default | Notes |
|----------|---------|-------|
| `EMBEDDING_ENABLED` | `true` | Compute embeddings for indexed chunks and memory |
| `EMBEDDING_PROVIDER` | `hash` | `hash` (offline, deterministic) \| `openai` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Used when provider is `openai` |
| `EMBEDDING_API_KEY` | *(unset)* | Falls back to `LLM_API_KEY` |
| `EMBEDDING_BASE_URL` | *(unset)* | Falls back to `LLM_BASE_URL` |
| `EMBEDDING_DIMENSION` | `1536` | pgvector column dimension |

### MCP

| Variable | Default | Notes |
|----------|---------|-------|
| `MCP_ENABLED` | `false` | Expose the `mcp_call` tool to agents |
| `MCP_SERVERS_FILE` | *(unset)* | JSON file declaring servers (command, args, env) |
| `MCP_TIMEOUT_SECONDS` | `30.0` | Per-request timeout for MCP server calls |

### Safety

| Variable | Default |
|----------|---------|
| `COMMAND_POLICY_ENABLED` | `true` |
| `COMMAND_DENY_EXTRA` | `[]` |
| `COMMAND_CONFIRM_EXTRA` | `[]` |
| `GIT_PROTECT_DIRTY_TREE` | `true` |

### Observability

| Variable | Default |
|----------|---------|
| `OTEL_ENABLED` | `false` |
| `OTEL_SERVICE_NAME` | `coding-agent` |
| `OTEL_EXPORTER_ENDPOINT` | `http://localhost:4318` |
| `OTEL_TRACES_ENABLED` | `true` |
| `OTEL_METRICS_ENABLED` | `true` |

---

## 10. API Contract

### Responses

**TaskResponse:**
```json
{
  "id": "uuid",
  "session_id": "uuid",
  "parent_task_id": null,
  "agent_type": "coder|pipeline|coordinator",
  "status": "pending|planning|running|reviewing|testing|completed|failed|cancelled",
  "goal": "string",
  "result": "string|null",
  "error": "string|null",
  "attempt": 0,
  "max_attempts": 3,
  "input_tokens": 0,
  "output_tokens": 0,
  "plan": "object|null",
  "plan_needs_approval": false,
  "plan_approved": null,
  "started_at": "datetime|null",
  "finished_at": "datetime|null",
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

**TaskDetailResponse:** TaskResponse + `messages: [{id, session_id, task_id, role, content, ordinal, tool_call_id, tool_calls, token_count, created_at}]`

### SSE Frame Format

```
event: snapshot|message|completed|failed|cancelled
data: {json}
```

`snapshot`/terminal frames carry `TaskResponse`; `message` frames carry `MessageResponse`. 15s keepalive comments. Closes on terminal status.

### Error Codes

| Code | Meaning |
|------|---------|
| 404 | Session or task not found |
| 409 | Retry when not terminal / max attempts exhausted; cancel when terminal |
| 422 | Validation error (missing goal, invalid agent_type, etc.) |
| 503 | LLM not configured |

---

## 11. CLI Reference

```bash
# Workspace management
engineer init                         # bind current repo
engineer status                       # show bound workspace

# Indexing
engineer index                        # discover + chunk + symbol-extract

# Tasks
engineer "goal"                       # plan + approve + run
engineer run -y "goal"                # auto-approve
engineer run --agent-type pipeline    # multi-agent pipeline
engineer run --agent-type coordinator # parallel specialists
engineer tasks                        # list session tasks
engineer cancel                       # cancel running task

# Code workflow
engineer diff                         # working-tree diff
engineer commit -m "msg"              # explicit message
engineer commit --generate            # LLM-drafted message
engineer pr [--draft] [--title "..."] # PR description + push + gh

# Testing
engineer test [--command] [--framework] [--fix] [--repairs N]

# Analysis modes
engineer review [--ref main]          # code review
engineer audit [--ref main]           # production-readiness audit
engineer graph [node] [--mermaid]     # dependency graph
engineer arch [--mermaid]             # LLM architecture analysis
engineer design "goal"                # system design document
engineer analyze [--scan-only]        # distributed-systems analysis
engineer modernize [--target "..."]   # legacy-modernization plan (graph-seeded)

# MCP
engineer mcp list                     # list configured MCP servers + tools

# Memory
engineer memory add "fact" [--kind fact]
engineer memory list [--kind preference]
engineer memory recall "query"
engineer memory clear

# Evals
engineer eval list                    # list benchmark tasks
engineer eval run [task-id] [--model] # run benchmark
engineer eval results                 # show results
engineer eval compare                 # compare models
```

---

## 12. Testing Strategy

### Test Structure

```
tests/
├── conftest.py          # shared fixtures (settings, FakeLLM, container, db_session)
├── unit/                # 541 tests — no infra required
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_paths.py
│   ├── test_executor_fs.py
│   ├── test_tool_registry.py
│   ├── test_llm_*.py
│   ├── test_coder_agent.py
│   ├── test_pipeline_agent.py
│   ├── test_coordinator_agent.py
│   ├── test_cli.py
│   ├── test_event_broker.py
│   ├── test_cancellation.py
│   ├── test_memory_*.py
│   ├── test_retrieval_*.py
│   ├── test_audit_*.py
│   ├── test_architecture_*.py
│   ├── test_design_*.py
│   ├── test_analysis_*.py
│   └── ...
├── integration/         # 101 tests — requires PostgreSQL + Redis
│   ├── test_health.py
│   ├── test_repositories.py
│   ├── test_orchestrator.py
│   └── test_tasks_api.py
└── sandbox/             # real Docker sandbox execution
    └── test_sandbox.py
```

### Key Testing Patterns

- **`FakeLLM`** (`tests/unit/fake_llm.py`): Scripted responses, records `.calls`. Tests never hit the network.
- **Stub executor**: In-memory file operations; no Docker required for unit tests.
- **Dependency overrides**: `app.dependency_overrides[get_orchestrator]` for ASGI integration tests.
- **Autouse table truncation**: Clean state between integration tests.
- **Markers**: `@pytest.mark.integration` for tests needing PostgreSQL/Redis.

### Running Tests

```bash
# Unit tests only (no infra)
make test-unit
# or on Windows:
.venv\Scripts\python.exe -m pytest -m "not integration"

# Full suite (needs make up + executor image)
make test
# or on Windows:
.venv\Scripts\python.exe -m pytest

# Lint + format check
make lint
# or:
.venv\Scripts\ruff.exe check app tests
.venv\Scripts\ruff.exe format --check app tests

# Type check
make typecheck
# or:
.venv\Scripts\mypy.exe app
```

---

## 13. Setup & Deployment Guide

### Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | 3.13+ | Runtime |
| Docker + Compose v2 | Latest | PostgreSQL, Redis, sandbox containers |
| Git | 2.x | Repository operations |
| `gh` (optional) | Latest | `engineer pr` opens PRs via GitHub CLI |

### Quick Start (Linux/macOS)

```bash
# 1. Provision infrastructure
make up                           # PostgreSQL 16 + pgvector, Redis 7

# 2. Create venv and install
make venv                         # creates .venv
make dev-install                  # editable install + dev deps

# 3. Apply migrations
make migrate                      # alembic upgrade head

# 4. Configure LLM
cp .env.example .env              # edit LLM_API_KEY

# 5. Run the API
make run                          # http://localhost:8000, /docs for Swagger

# 6. (Optional) Build sandbox image
make executor-image               # needed for terminal tools + integration tests
```

### Windows Setup

```powershell
# 1. Install Python 3.13 via winget
winget install --id Python.Python.3.13 --source winget

# 2. Create venv
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e . -r requirements-dev.txt

# 3. Copy env
Copy-Item .env.example .env       # edit LLM_API_KEY

# 4. Set git identity (required for tests)
git config --global user.name "Your Name"
git config --global user.email "you@example.com"

# 5. Run unit tests (no Docker needed)
.venv\Scripts\python.exe -m pytest -m "not integration"

# 6. Lint + type check
.venv\Scripts\ruff.exe check app tests
.venv\Scripts\mypy.exe app
```

**Note on Windows:** 8 unit tests fail due to CRLF line endings in `file_read` output and Windows-specific git output format. These are environment artifacts, not bugs. The app targets Linux/macOS for production use.

### First API Use

```bash
# 1. Create a user, workspace, and session via psql or the auth API
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"securepass123"}'

# 2. Create workspace + session (via API or direct SQL)

# 3. Run a task
curl -X POST http://localhost:8000/api/v1/sessions/{session-id}/tasks \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"goal": "Fix the README port number"}'

# 4. Stream progress
curl http://localhost:8000/api/v1/sessions/{sid}/tasks/{tid}/events \
  -H 'Authorization: Bearer <token>'
```

### CLI Usage

```bash
# Bind workspace
engineer init

# Run a task
engineer "fix the failing test in auth.py"

# Review changes
engineer review --ref main

# Generate PR
engineer pr
```

### Database Management

```bash
# Apply migrations
make migrate

# Create new migration
make migrate-revision MESSAGE="add column"

# Rollback
make migrate-downgrade

# Direct SQL
make psql
```

### Observability

```bash
# Enable OTel (set in .env)
OTEL_ENABLED=true
OTEL_EXPORTER_ENDPOINT=http://localhost:4318

# Start Jaeger or Grafana Tempo
docker run -d -p 16686:16686 jaegertracing/all-in-one:latest
```

### CI/CD & Kubernetes Deployment

- **`.github/workflows/ci.yml`** — on push/PR: `ruff check` + `ruff format --check`, `mypy app`, and `pytest -m "not integration"` (a git-identity step is configured so git-touching tests have an author).
- **`.github/workflows/release.yml`** — on a `v*` tag: builds and pushes the image to GHCR (`docker/build-push-action@v6`) using `infra/Dockerfile`.
- **`deploy/k8s/`** — production manifests managed by Kustomize: `namespace.yaml`, `configmap.yaml`, `secret.example.yaml` (copy to `secret.yaml` and fill), `postgres.yaml` (pgvector StatefulSet), `redis.yaml`, `api.yaml` (a `migrate` initContainer runs `alembic upgrade head` before the API starts; liveness `/api/v1/healthz`, readiness `/api/v1/readyz`), `ingress.yaml`, and `kustomization.yaml`.

```bash
make deploy         # kubectl apply -k deploy/k8s
make deploy-delete  # kubectl delete -k deploy/k8s
```

Run tests over the same manifests with `tests/unit/test_deploy_manifests.py` (validates the YAML parses and key resources/probes are present).

---

## 14. Known Issues & Platform Notes

### Pre-existing

- **Local LLM config**: `.env.example` defaults to `opencode.ai/zen/v1` with model `big-pickle`. Tests use `FakeLLM` and never hit the network.
- **Embeddings default offline**: `EMBEDDING_PROVIDER=hash` (a deterministic `HashingEmbedder`) works with no network, so semantic search functions out of the box; set `EMBEDDING_PROVIDER=openai` for real embeddings. `CodeChunk.embedding` and `MemoryEntry.embedding` (pgvector) are populated by the indexer and memory service when an embedder is configured.

### Windows-Specific (this environment)

8 unit tests fail on Windows due to:
- **CRLF line endings**: `FILE_READ` opens files in text mode, which converts `\n` to `\r\n` on Windows. Tests expect Unix `\n`.
- **Git output format**: Some tests assert exact git log output that differs on Windows.

These are environment artifacts. The app is developed and tested on Linux/macOS.

---

## 15. Roadmap

### Completed

| Phase | Feature |
|-------|---------|
| 1–3 | Gateway, core, database, persistence |
| 4 | Tool system + Docker sandbox |
| 5 | LLM abstraction + coder agent + orchestrator + gateway task API |
| 6 | SSE streaming (async tasks) |
| 7 | Retries, cancellation, durable checkpoints |
| 8 | Multi-agent pipeline (planner→coder→reviewer→tester) |
| P0-A | Developer CLI |
| P0-B/C | Repository indexing + context retrieval |
| P0-D | Planning + approval |
| P0-E | Safety controls + edit_file |
| P0-F | Test execution + repair loop |
| 9 | Auth + workspace/session management |
| 10 | Memory |
| 11 | Evaluation framework |
| 12 | Observability (OTel) |
| 13 | Git workflow (tools + commit + PR) |
| 14 | Staff-engineer audit |
| 15 | Dependency graph + architecture analysis |
| 16 | System design mode |
| 17 | Distributed-systems analysis |
| 18 | Coordinator + parallel specialists |
| 17 (P2) | Legacy modernization mode (`engineer modernize`) |
| 18 (P2) | MCP tool integration (`mcp_call`, `engineer mcp list`) |
| 19 | Deployment: CI/CD workflows, K8s manifests, `make deploy` |
| 20 | Web UI (`/ui` single-page app served by the gateway) |

### Remaining (P2)

| Item | Feature |
|------|---------|
| 20 (b) | VS Code extension (the web UI covers the browser case; no Node toolchain is assumed in this repo) |
| — | Autoscaling policies / Helm chart (beyond the Kustomize base) |

---

*Document generated from full source analysis of the coding-agent repository.*
*Last updated: 2026-10-10*
