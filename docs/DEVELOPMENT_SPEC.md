# Development Record — Steps, Specs, and Results

A faithful log of every development step for the coding-agent, from the
first commit to the current head. Each step lists its **spec** (what it had
to achieve), the **deliverables** (what was built), and the **result**
(how it was verified).

Final state at the time of writing:

- **Tests:** 150 passing (unit + integration + sandbox)
- **Linting:** `ruff` clean, `ruff format --check` clean
- **Types:** `mypy --strict` clean over 85 source files
- **Database:** migrations `0001`–`0005` applied, downgrade/upgrade
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

## Step 9 — Retries, cancellation, and durable checkpoints (Phase 7)

**Spec**

Make task lifecycle operations explicit and durable: track how many times a
task has run, allow a terminal task to be retried within a bound, and allow
a pending/running task to be cancelled cooperatively. The task row (status +
`attempt`) plus the incrementally persisted transcript form the durable
checkpoint; a retry re-runs the same task and appends to the transcript.

**Deliverables**

- `app/orchestrator/cancellation.py` — `TaskCancelled` exception and
  `CancellationRegistry`, an in-process per-task cancel flag (the stand-in
  for a distributed signal) with `request_cancel` / `reset` / `discard`
- `app/agents/coder.py` — optional `should_cancel` predicate checked at each
  step boundary; raising `TaskCancelled` stops the loop at the next safe
  point (cooperative cancellation, never mid-call)
- `app/database/models/task.py` + `alembic/versions/0005_*.py` —
  `tasks.attempt` (run counter, durable checkpoint) and `tasks.max_attempts`
  (retry budget); `app/gateway/schemas.py` exposes both on `TaskResponse`
- `app/orchestrator/orchestrator.py` — `run_task` guards terminal/running
  tasks (row lock via `get_for_run(for_update=True)`) and increments
  `attempt` with the RUNNING transition; new `retry_task` resets a terminal
  task to `pending` (clearing error/result/timestamps, keeping the
  transcript); `_cancel` finalizes `cancelled` and keeps any earlier
  transition; `EventKind` gains `cancelled`
- `app/gateway/routes/tasks.py` — `POST /tasks/{id}/retry` (resets a
  terminal task and schedules a rerun, `409` if running or budget
  exhausted) and `POST /tasks/{id}/cancel` (persists `cancelled` and records
  the cooperative cancel, `409` for terminal tasks)
- `app/core/config.py` — `task_max_attempts` default for new tasks;
  `app/core/container.py` / `app/gateway/dependencies.py` — shared
  `cancellations` registry wired through the container and
  `CancellationRegistryDep`
- `tests/unit/test_cancellation.py`; `should_cancel` unit tests; orchestrator
  integration tests for attempt tracking, retry reset/rerun, guard rejections,
  and in-flight cancellation; tasks API tests for both endpoints

**Result**

- 26 new tests; full suite at 138 passing.
- `attempt` is incremented and persisted with each RUNNING transition, so a
  crashed or failed run is never silently lost and a retry resumes from a
  known state; `run_task` is guarded against double-runs via a row lock.
- Retry preserves the prior transcript as durable history and appends the new
  attempt; it is bounded by `max_attempts` (default `task_max_attempts`).
- Cancellation is cooperative: the cancel endpoint persists `cancelled`
  immediately (crash-safe) and sets the in-process flag; the running agent
  stops at its next step boundary and emits a `cancelled` event. Automatic
  retries on transient errors are deferred (they conflict with the
  terminal-status SSE close) and remain future work.
- One migration: `0005` adds `attempt` / `max_attempts` to `tasks`.

---

## Step 10 — Multi-agent pipeline (Phase 8)

**Spec**

Let a task run a composed multi-agent pipeline — planner → coder → reviewer →
tester — instead of only the single coder loop. All stages share one task, one
workspace, and one incrementally persisted transcript; a non-PASS reviewer or
failing tester routes back to the coder for rework, bounded by a
`max_passes` limit so the pipeline always terminates. Cooperative cancellation
and the attempt/retry machinery must keep working unchanged for both agent
types.

**Deliverables**

- `app/agents/base.py` — `LoopAgent`, a shared single-node ReAct loop (the
  machinery extracted from the coder): LLM call → tool execution → result
  feedback, bounded by `max_steps`, with the `on_message`/`should_cancel`
  hooks. `run(goal, initial_messages)` seeds and streams the goal;
  `run_from(messages)` runs over an existing conversation without re-emitting
  it (how a composed pipeline reuses the loop per stage). `LoopResult`,
  `LoopState`, and structural `RunResult`/`AgentLike` protocols.
- `app/agents/coder.py` — `CoderAgent` is now a thin `LoopAgent` subclass
  (same public constructor); `CoderResult` aliases `LoopResult`;
  `format_tool_result` moved to the base and re-exported.
- `app/agents/pipeline.py` — `PipelineAgent`, a LangGraph graph over one
  shared transcript: `planner` produces the plan, `coder` implements it,
  `reviewer` judges it, `tester` verifies it. Each stage runs a fresh
  `LoopAgent` via `run_from` over the accumulated messages and returns only
  the delta. `_route_review`/`_route_test` send non-PASS verdicts back to
  `coder`, counting rework in `pass_count` bounded by `max_passes`;
  `parse_verdict` reads the `VERDICT: PASS|CHANGES_NEEDED|FAIL` first line
  (fail-safe). `PipelineResult` mirrors the loop result plus `passes`.
- `app/orchestrator/orchestrator.py` — `_build_agent` dispatches on
  `task.agent_type`: `coder` → `CoderAgent`, `pipeline` → `PipelineAgent`;
  unknown types fail the task with `ValueError: unsupported agent_type: …`.
  The executor/agent construction moved inside the run's try block so a
  failure surfaces as a FAILED task.
- `app/core/config.py` — `pipeline_max_passes` (default 2) bounds rework.
- `tests/unit/test_pipeline_agent.py` — verdict parsing, happy path (plan →
  code → review PASS → test PASS with summed tokens and per-stage transcript
  growth), reviewer/tester-triggered rework loops, rework bound
  (`max_passes`), zero-passes, `on_message` streaming, cooperative
  cancellation.
- `tests/integration/test_orchestrator.py` — a `pipeline` task persists the
  full 4-stage transcript with summed token accounting and events; an unknown
  `agent_type` fails the task with the captured error.
- `tests/integration/test_tasks_api.py` — `POST …/tasks` with
  `agent_type: "pipeline"` runs end to end through the API (completed,
  ​4-stage transcript, `20`/`8` tokens).

**Result**

- 12 new tests; full suite at 150 passing.
- A `pipeline` task runs planner → coder → reviewer → tester in a single
  run/attempt; the transcript grows monotonically across stages (each stage's
  LLM call sees the accumulated conversation) and is persisted exactly once
  per message through the shared `on_message` hook.
- Reviewer `CHANGES_NEEDED` and tester `FAIL` verdicts route back to the coder;
  rework is counted and bounded by `pipeline_max_passes`, after which the
  pipeline terminates with the latest verdict — guaranteed termination.
- Cancellation still works: `should_cancel` is checked at every stage's step
  boundaries and raises `TaskCancelled`; retry/attempt semantics are unchanged
  (a pipeline task retries as one task).
- Migration-independent: no schema changes were required; the existing
  `Task.agent_type` column now has two supported values.

---

## Final integrated result

With all ten steps in place, an end-to-end request flow is verified:

1. `POST /api/v1/sessions/{id}/tasks` creates a task (status `pending`,
   `attempt 0`) and runs it against the configured LLM in the background:
   `agent_type: "coder"` runs the single LangGraph coder loop, while
   `agent_type: "pipeline"` runs the planner → coder → reviewer → tester
   pipeline over the same transcript (rework bounded by `pipeline_max_passes`),
   executing tools in the sandbox and persisting the transcript incrementally.
2. `GET /api/v1/sessions/{id}/tasks/{task_id}/events` replays the current
   state and then streams each message and status transition live until the
   task terminates.
3. `POST /api/v1/tasks/{id}/cancel` cancels a pending or running task
   durably; the agent stops at its next step boundary and emits a `cancelled`
   event.
4. `POST /api/v1/tasks/{id}/retry` resets a terminal task (clearing the
   error, keeping the transcript) and re-runs it, bounded by
   `max_attempts`; `attempt` is durable across every run.
5. `GET /api/v1/tasks/{id}` returns the task with its full, ordered
   transcript, attempt counter, and token accounting.
6. Every step is covered by tests, lint, and strict typing; the work is
   committed and pushed to GitHub.

### What is intentionally out of scope (future phases)

- Authentication/authorization and user-facing session management
- Retrieval (AST indexing + embeddings), memory, evals, monitoring
