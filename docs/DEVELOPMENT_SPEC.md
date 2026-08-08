# Development Record — Steps, Specs, and Results

A faithful log of every development step for the coding-agent, from the
first commit to the current head. Each step lists its **spec** (what it had
to achieve), the **deliverables** (what was built), and the **result**
(how it was verified).

Final state at the time of writing:

- **Tests:** 112 passing (unit + integration + sandbox)
- **Linting:** `ruff` clean, `ruff format --check` clean
- **Types:** `mypy --strict` clean over 80 source files
- **Database:** migrations `0001`–`0004` applied, downgrade/upgrade
  round-trips verified
- **Repository:** committed and pushed to `main` at
  `github.com/Neo945/AI-Engineer`

---

## Step 1 — Project scaffold and infrastructure (Phase 1)

**Spec**

Provide a modular-monolith skeleton: a FastAPI gateway, validated
configuration, structured logging, a dependency-injection composition root,
and locally provisioned PostgreSQL + Redis, all driven from a single
`Makefile`.

**Deliverables**

- `app/gateway/` — app factory with lifespan-managed `Container`, health and
  readiness endpoints (`/api/v1/healthz`, `/api/v1/readyz`)
- `app/core/` — `Settings` (pydantic-settings), structlog config, `Container`
- `docker-compose.yml` — PostgreSQL 16 (+ pgvector) and Redis 7
- `Makefile`, `pyproject.toml`, `requirements*.txt`, `.gitignore`, `.env.example`
- `infra/Dockerfile` — runtime image for the API process

**Result**

- `make up` provisions both containers healthy; `make run` serves the API at
  `http://localhost:8000` with Swagger at `/docs`.
- `GET /api/v1/readyz` reaches PostgreSQL and Redis and reports status.
- Config is validated at startup and fails fast on invalid values.

---

## Step 2 — Persistence layer (Phase 2–3)

**Spec**

Provide an async SQLAlchemy engine and session factory, an Alembic
migration chain (pgvector enabled), the core domain models, repositories,
and value-stored enums so database rows stay stable if enum identifiers are
renamed later.

**Deliverables**

- `app/database/` — engine, session factory, `Base`, `mixins`, `enums`
- Models: `User`, `Workspace`, `Session`, `Task`, `Message`, `CodeChunk`
  (tasks form a tree via `parent_task_id`; messages are append-only)
- Repositories: `base`, `user`, `workspace`, `session`, `task`, `message`
- Migrations `0001` (pgvector) and `0002` (core schema)

**Result**

- Migrations apply cleanly; model persistence and repository integration
  tests pass against real PostgreSQL.
- `Session`, `Task`, and `Message` statuses use native PG enums stored by
  value.

---

## Step 3 — Sandboxed tool execution (Phase 4)

**Spec**

Provide a `ToolExecutor` that runs agent tools — filesystem, terminal, and
git — inside ephemeral Docker containers that are resource-capped,
non-root, network-less (unless enabled), and hard-timeout bounded, and that
can only ever touch files inside the bound workspace.

**Deliverables**

- `app/executor/` — `ToolExecutor` (with `.build` from settings), `paths.py`
  (workspace-rooted path resolution), `sandbox.py`
- `app/tools/` — `schemas.py` (`ToolName`, `ToolCall`, `ToolResult`,
  `ToolSpec`), `registry.py`, `specs/` (filesystem, terminal, git)
- `infra/executor/Dockerfile` — the sandbox runtime image

**Result**

- Path-traversal attempts are rejected by `paths.py` (unit tests).
- `ToolExecutor` runs real commands in the sandbox image (sandbox
  integration tests, requires `make executor-image`).
- Tool specs are validated against JSON schemas before dispatch.

---

## Step 4 — LLM provider abstraction (Phase 5a)

**Spec**

Provide provider-agnostic chat message and response types plus an async
`LLMProvider` protocol with `complete()`/`stream()`, and adapters for
Anthropic, OpenAI, and OpenAI-compatible local backends (vLLM/Ollama),
selected from settings.

**Deliverables**

- `app/llm/` — `messages.py` (`ChatMessage`, `ChatRole`, `ToolRequest`),
  `protocol.py` (`LLMProvider`, `LLMResponse`, `LLMStreamEvent`,
  `LLMUsage`), `factory.py` (`build_llm_client`), `clients/` (anthropic,
  openai)
- `Settings.llm_*` + `.env.example` LLM section
- `requirements.txt`/`pyproject.toml`: `anthropic`, `openai`

**Result**

- 25 unit tests for messages, factory, and both adapters pass.
- System prompts are mapped per provider convention (Anthropic top-level
  `system`; OpenAI leading `system` message); tool round-trips and token
  usage are normalized.
- Local backends work with no API key (placeholder key only when a
  `base_url` is set).
- Unknown providers raise `ValueError` with a clear message.

---

## Step 5 — Coder agent loop (Phase 5b)

**Spec**

Provide a LangGraph agent that sends the goal plus tool specs to the LLM,
executes the requested tools through the executor, feeds results back as
tool messages, and terminates on a final answer or a `max_steps` bound,
returning the full transcript and token totals.

**Deliverables**

- `app/agents/coder.py` — `CoderAgent`, `CoderResult`, `format_tool_result`
- `tests/unit/fake_llm.py` — scriptable `LLMProvider` fake
- `tests/unit/test_coder_agent.py`

**Result**

- 7 unit tests: tool-then-answer, no-tools, unknown tool handled gracefully,
  `max_steps` termination, failed-result formatting, transcript seeding,
  and `format_tool_result` variants.

---

## Step 6 — Orchestrator and durable persistence (Phase 5c)

**Spec**

Provide an `Orchestrator.run_task()` that owns the task lifecycle
(`PENDING → RUNNING → COMPLETED/FAILED`), persists every produced message to
the transcript in deterministic order, records the answer/error, token
totals, and timestamps, and emits lifecycle events through an injectable
callback.

**Deliverables**

- `app/orchestrator/orchestrator.py` — `Orchestrator`, `OrchestratorEvent`
- Migration `0003` — `messages.tool_call_id` + `messages.tool_calls`
- Migration `0004` — `messages.ordinal` (indexed) for deterministic
  transcript ordering
- `TaskRepository.get_for_run` (eager-loads session + workspace to avoid
  async lazy loads), `MessageRepository.max_ordinal`/`list_by_task`

**Result**

- 3 integration tests against real PostgreSQL + FakeLLM + stub executor
  (success round-trip, failure state, unknown id).
- Migration `0004` downgrade/upgrade round-trip verified.
- Caught and fixed a real bug: messages persisted in one flush share a
  single `created_at`, so order was previously nondeterministic.

---

## Step 7 — Gateway task API (Phase 5d)

**Spec**

Expose the agent loop over HTTP: create and run a task inline, list a
session's tasks, and fetch a task with its persisted transcript, with
correct precedence for 404 (missing session) and 503 (LLM not configured).

**Deliverables**

- `app/gateway/schemas.py` — `TaskCreateRequest`, `TaskResponse`,
  `MessageResponse`, `TaskDetailResponse`
- `app/gateway/routes/tasks.py` — `POST /sessions/{id}/tasks`,
  `GET /sessions/{id}/tasks`, `GET /tasks/{id}`
- `app/gateway/dependencies.py` — `get_orchestrator` (503 when LLM
  unconfigured)
- `Container.orchestrator()` — lazy LLM client construction (startup never
  requires LLM credentials)
- `tests/integration/test_tasks_api.py`

**Result**

- 6 ASGI integration tests pass (completed run + persisted transcript, tool
  round-trip in detail, 503 when LLM unconfigured, 404 for missing session,
  ordered listing, 404 for missing task).
- Two subtle bugs found and fixed: `expire_on_commit=False` required an
  explicit `db.refresh()` after the run; and the 503 orchestrator dependency
  had to be declared after the 404 session dependency so missing sessions
  win.

---

## Step 8 — Streaming agent events over SSE (Phase 6)

**Spec**

Make task execution asynchronous and stream its progress to clients: run
tasks in the background after `POST`, persist the transcript incrementally
so partial work is durable and visible while running, and expose a
Server-Sent Events endpoint that replays the current state and then emits
each new message and status transition live until the task terminates.

**Deliverables**

- `app/orchestrator/broker.py` — `EventBroker`, an in-process pub/sub
  fan-out per task (the single-process stand-in for Redis pub/sub)
- `app/orchestrator/orchestrator.py` — `EventKind` gains `message`;
  `OrchestratorEvent` gains `ordinal`; `Orchestrator` takes an optional
  `event_broker`; transcript rows are committed per message (immediately
  visible to concurrent readers) via a `CoderAgent.on_message` hook
- `app/agents/coder.py` — optional `on_message` callback invoked for the
  goal, every assistant turn, and every tool result in transcript order
- `app/gateway/routes/tasks.py` — `POST /sessions/{id}/tasks` now returns
  `202` and runs the task in the background; new
  `GET /sessions/{id}/tasks/{task_id}/events` SSE endpoint that replays
  (snapshot + transcript) then streams live updates, with 15s keepalives,
  and closes on a terminal event
- `app/core/container.py` / `app/gateway/dependencies.py` — shared
  `event_broker` wired through the container and `EventBrokerDep`
- `tests/unit/test_event_broker.py`; `on_message` unit tests; orchestrator
  integration tests updated for message events and partial transcripts on
  failure; tasks API tests updated for `202` plus new SSE stream tests

**Result**

- 14 new tests; full suite at 112 passing.
- The SSE stream opens with a replay (task snapshot + persisted transcript)
  and then live messages; DB state is the source of truth and broker events
  only wake the streamer, so subscribers never miss or duplicate a message.
- Failures keep the partial transcript (the goal message and any completed
  steps), instead of discarding everything.
- Migration-independent: no schema changes were required.

---

## Final integrated result

With all eight steps in place, an end-to-end request flow is verified:

1. `POST /api/v1/sessions/{id}/tasks` creates a task (status `pending`) and
   runs the LangGraph coder loop against the configured LLM in the
   background, executing tools in the sandbox and persisting the transcript
   incrementally.
2. `GET /api/v1/sessions/{id}/tasks/{task_id}/events` replays the current
   state and then streams each message and status transition live until the
   task terminates.
3. `GET /api/v1/tasks/{id}` returns the task with its full, ordered
   transcript and token accounting.
4. Every step is covered by tests, lint, and strict typing; the work is
   committed and pushed to GitHub.

### What is intentionally out of scope (future phases)

- Retries, cancellation, and durable checkpoints
- Multi-agent pipeline: planner → coder → reviewer → tester
- Authentication/authorization and user-facing session management
- Retrieval (AST indexing + embeddings), memory, evals, monitoring
