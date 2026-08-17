# Project Gap Analysis

Audit of the `coding-agent` repository against the target specification for a
portfolio-grade AI Software Engineering Agent.

- **Date:** 2026-08-09
- **Branch:** `main` at `6548500` (Phase 8 complete)
- **Evidence base:** full source review of `app/`, `tests/`, `docs/`,
  `infra/`, `alembic/`; `pytest --collect-only` = 150 tests; `ruff`/`mypy`
  clean on 85 source files (~4,740 LOC source, ~3,550 LOC tests).

> This document describes *what exists today*, *what the target requires*,
> and the priority of each gap. It deliberately proposes **no source changes**
> — implementation order is in `TARGET_ARCHITECTURE.md` and must be approved
> before any P0 work begins.

---

## 1. Executive summary

The project already contains a **strong, well-tested agent runtime core**:

- Provider-agnostic LLM abstraction (`openai`, `anthropic`, local
  OpenAI-compatible backends) — spec §25 **done**.
- LangGraph agent loop + single `coder` agent and a composed
  planner→coder→reviewer→tester `pipeline` agent — spec §8/§10/§14 **mostly
  done**.
- Typed tool system (name/description/input schema/result/error/metadata)
  with 10 tools across filesystem, terminal, and git — spec §11 **mostly
  done**.
- Docker-sandboxed terminal execution (non-root, no network, read-only
  rootfs, resource caps, hard timeout) — spec §12 **done at the sandbox
  level**.
- Durable orchestration: per-message transcript persistence, SSE streaming,
  retry/cancel, token accounting — spec §8 **done**.

The project's **largest gaps are all above the runtime core**:

1. **No CLI** — the spec's primary interface (spec §5) does not exist; the
   only surface is a FastAPI HTTP API.
2. **No repository intelligence** — no repository discovery, ingestion,
   indexing, AST analysis, dependency graph, or semantic/symbol search
   (spec §6, §7). `app/retrieval/` is an empty stub; `CodeChunk` exists but
   is never populated.
3. **No planning/approval UX** — a plan is produced inline by the pipeline's
   planner stage but there is no structured plan artifact and no user
   approval gate before execution (spec §9).
4. **No test-and-repair loop** — a tester stage returns a verdict, but there
   is no automated run-tests → analyze-failure → fix → retry cycle with
   bounded attempts (spec §13).
5. **No memory** — `app/memory/` is an empty stub (spec §20).
6. **No human-facing modes** — no Staff Engineer audit, production-readiness
   audit, distributed-systems analysis, system-design mode, or legacy
   modernization (spec §15–§19).
7. **No observability layer** — structlog exists but no OTel/metrics/tracing
   (spec §23).
8. **No evaluation framework** — `app/evals/` is an empty stub (spec §24).
9. **Partial safety controls** — sandbox + path confinement + timeouts exist,
   but there are no destructive-command confirmations, allow/deny rules, or
   approval gates (spec §12, §30).
10. **No auth** — `users` table exists but there are no auth endpoints or
    ownership checks; workspaces/sessions have no management API.

---

## 2. Capability gap table

| Capability            | Current State | Target State | Priority |
| --------------------- | ------------- | ------------ | -------- |
| CLI                   | **Done.** `engineer` entry point with rich TTY UX, streaming, commands (init/index/status, task, review/audit, test, diff/commit/pr, memory, design, analyze, evals). | — | P0 |
| Repository indexing   | **Done.** `engineer init` / `engineer index`: file discovery, language detection, AST symbol extraction, embeddings, indexer service. | — | P0 |
| Agent orchestration   | **Done.** `app/orchestrator` (LangGraph), durable transcript, events, retry/cancel; `coder` + `pipeline` + `coordinator` agents. | — | P0 |
| Tool execution        | **Done.** 16 tools (fs 7, terminal 1, git 7, test 1) via `ToolExecutor` + Docker sandbox. | — | P0 |
| Memory                | **Done.** Session/project/decision memory, `engineer memory` / `memory list` / `memory clear`. | — | P1 |
| Architecture analysis | **Done.** `engineer graph` (dependency graph, hubs/cycles/orphans/layers), `engineer arch` (LLM analysis seeded by graph). | — | P1 |
| Production audit      | **Done.** `engineer audit` staff-engineer + production-readiness scoring with evidence. | — | P1 |
| Git integration       | **Done.** 7 git tools (status, diff, commit, log, branch, checkout, push); `engineer commit` with LLM message generation; `engineer pr` with LLM-generated title/summary/tests/risks/migration notes + `gh pr create`. | — | P1 |
| Multi-agent system    | **Done (P2).** Fixed planner→coder→reviewer→tester pipeline + `CoordinatorAgent` dispatching to parallel read-only security/perf specialists and an optional coder, then synthesizing. | Extend specialists (e.g. testing); parallel execution already in place. | P2 |
| Deployment            | **Partial.** Docker Compose for Postgres/Redis; API + executor Dockerfiles. | K8s manifests, health/autoscaling, CI/CD, observability stack. | P2 |

Supplementary rows (not in the required list but material gaps):

| Capability            | Current State | Target State | Priority |
| --------------------- | ------------- | ------------ | -------- |
| Context retrieval     | **Done.** `engineer index` + `ContextAssembler` with keyword search + symbol lookup; feeds ranked context window into the agent loop. | — | P0 |
| Planning + approval   | **Done.** Structured plan artifact (objective/assumptions/files/deps/risks/validation) + approval gate with `--yes` automation. | — | P0 |
| Test execution        | **Done.** `test_run` tool runs the suite in the sandbox, parses structured pass/fail output. | — | P0 |
| Test-and-repair loop  | **Done.** Test-and-repair graph node: analyze failure → fix → re-run, bounded by `test_max_repairs`. | — | P0 |
| Code review agent     | **Done.** Standalone `engineer review` with severity/file/line/problem/reason/fix across correctness, security, performance, maintainability, testing. | — | P1 |
| Safety controls       | **Done.** `edit_file` tool, command allow/deny rules, destructive-command confirmation. | — | P0 |
| Semantic/symbol search| **Done.** `file_search` with ripgrep-style keyword search + AST symbol extraction + dependency-graph context. | — | P0 |
| LLM provider breadth  | **Partial.** `openai`, `anthropic`, local OpenAI-compatible. | Add Gemini; keep env-driven config. | P1 |
| Observability         | **Done.** OTel traces/metrics (LLM calls, tokens, duration, tool calls, task runs, HTTP requests), `InstrumentedLLM` wrapper, HTTP middleware, token-level streaming to CLI via `_TokenSink`. | — | P1 |
| Evaluation framework  | **Done.** 6 benchmark tasks, headless `EvalRunner`, `ResultStore` (JSONL), `engineer eval list|run|results|compare` CLI. | Expand task suite, multi-model parallel runner, CI integration. | P1 |
| Auth / workspace mgmt | **Done.** Register/login/refresh/logout/me endpoints; user-scoped workspaces and sessions; ownership checks. | — | P1 |
| System design mode    | **Done.** `engineer design "…"` → architecture, APIs, data/event model, caching, failure handling, scaling, observability, Mermaid diagrams. | — | P1 |
| Distributed systems analysis | **Done.** `engineer analyze` scans for sync/async HTTP, retries, idempotency, concurrency, locking, caching, timeouts, circuit breakers, messaging. | — | P1 |
| Legacy modernization  | **Missing.** | `engineer modernize` → migration plan, risk assessment, rollback strategy; approval before execution. | P2 |
| MCP integration       | **Missing.** Tool layer is closed. | Design tool layer so MCP-compatible tools can be added (GitHub, Jira, DBs, cloud). | P2 |
| Web UI / extension    | **Missing.** | React frontend / VS Code extension consuming the same gateway. | P2 |
| Streaming tokens      | **Done.** `_TokenSink` + full `on_token` callback chain streams LLM output tokens live to Rich console; configurable via `cli_stream_tokens`. | — | P1 |

---

## 3. Detail by capability area

### 3.1 CLI (P0) — Missing

- No `engineer` entry point; `pyproject.toml` defines no console script.
- Only interaction today is the FastAPI gateway (`app/gateway/`) — a backend,
  not a developer UX.
- Target: `engineer` (session), `engineer init|index|status`,
  `engineer "task"`, `engineer review|audit|test`, `engineer diff|commit|pr`,
  `engineer memory`, `engineer design`, `engineer analyze`, `engineer
  modernize`.
- The CLI must be a **thin adapter over the same agent core** (gateway or
  in-process orchestrator), not a parallel implementation.

### 3.2 Repository intelligence + context engine (P0) — Missing

- `app/retrieval/__init__.py` — stub ("Tree-sitter AST parsing, symbol
  extraction, embeddings, pgvector semantic search, repository code graph").
- `app/database/models/code_chunk.py` — table exists (workspace_id, file_path,
  start_line, end_line, content, language, embedding Vector(1536), meta) but
  nothing writes it; no `CodeChunkRepository`, no indexer, no search service.
- No file discovery (the agent must use `file_list`/`file_search` by hand and
  read every file it cares about — no index to consult).
- No language detection, AST/symbol extraction, dependency graph, or hybrid
  search. Spec §6/§7 entirely open.
- Target stack: an indexer (walker → chunker → optional embeddings), a
  retrieval service over `code_chunks` + pgvector (semantic) + ripgrep-style
  keyword + symbol index, and a context assembler that picks relevant files
  for a goal. Embedding provider must be pluggable (spec's LLM-abstraction
  pattern reused).

### 3.3 Agent orchestration (P0) — Effectively done

- `app/orchestrator/orchestrator.py` — durable lifecycle, per-message
  persistence, event emission, retry/cancel, token accounting, agent-type
  dispatch (`coder`, `pipeline`, `coordinator`, unknown → failed task).
- `app/agents/base.py` — `LoopAgent`/`LoopResult`/`RunResult`/`AgentLike`,
  cooperative cancellation, `on_message` streaming hook, `tool_names`
  allowlist that rejects out-of-allowlist tool requests.
- `app/agents/pipeline.py` — planner→coder→reviewer→tester with rework
  routing bounded by `pipeline_max_passes`.
- `app/agents/coordinator.py` — `CoordinatorAgent` dispatches to parallel
  read-only security/perf specialists (confined to `READ_ONLY_TOOLS`) and an
  optional coder, then synthesizes one answer.
- Remaining P2: more specialists (e.g. testing), intent-based agent selection
  across all modes rather than only the fixed `agent_type` set.

### 3.4 Tool system (P0) — Done, with listed additions

- `app/tools/` — typed `ToolCall`/`ToolResult`/`ToolSpec`, `ToolRegistry`
  (register/unregister/execute/validate), JSON-Schema argument validation,
  `ALL_SPECS`/`ARGUMENT_MODELS` registry.
- `app/executor/executor.py` — 10 handlers: `file_read`, `file_write`,
  `file_list`, `file_search` (glob), `file_delete`, `file_move`,
  `terminal_run`, `git_status`, `git_diff`, `git_commit`.
- Missing vs spec §11: `edit_file` (diff-based), `search_code`/`find_symbol`/
  `find_references`/`find_dependencies`, `git_log`/`git_branch`/`git_checkout`/
  `git_push`, `docker_build`/`docker_run`/`docker_logs`/`docker_stop`.
- Note: `file_write` exists so code modification works, but without an
  `edit_file` the agent rewrites whole files — a risk to "minimize unrelated
  changes" (spec §10).

### 3.5 Terminal security (P0) — Partial

Strong: commands run only in the sandbox (non-root, no network, read-only
rootfs, tmpfs /tmp, memory/CPU caps, hard `timeout -s KILL`, container
destroy-on-timeout); host-side git is hardened (no hooks, `GIT_TERMINAL_PROMPT=0`).
Missing: no command allow/deny rules, no destructive-command confirmation
(`rm`, `git reset --hard`, `git push --force`, …), no per-command user
approval hook. Spec §12/§30 explicitly require these.

### 3.6 Test-and-repair loop (P0) — Missing

- The pipeline's tester stage asks the LLM for `VERDICT: PASS|FAIL` — a
  simulated verdict, not a real test run.
- No tool parses a test suite result into structured failures; no repair
  loop feeds failures back to a fixer with a bounded attempt count. Spec §13
  (one of the highest-value differentiators) is entirely open.

### 3.7 Code review (P1) — Partial

- `reviewer` pipeline stage produces PASS/CHANGES_NEEDED; the LLD documents
  a reviewer prompt. But there is no structured findings format
  (severity/file/line/problem/reason/suggested fix), no standalone review
  command, and no review of an existing branch (uncommitted work).

### 3.8 Memory (P1) — Done

- `app/memory/` — session, project, and decision memory with repository and
  service layers. `engineer memory`, `memory list`, `memory clear` commands.
  Orchestrator injects recalled memory into agent context window.

### 3.9 Git workflow (P1) — Done

- 7 git tools fully wired from `ToolName` enum through spec to executor:
  `git_status`, `git_diff`, `git_commit`, `git_log`, `git_branch`,
  `git_checkout`, `git_push`.
- `engineer commit` with `--generate` (LLM-drafts conventional-commit message
  from diff) and `--message` override.
- `engineer pr` with LLM-generated title/summary/tests/risks/migration notes,
  branch push, `.engineer/pr-<branch>.md` save, and `gh pr create` (graceful
  fallback when `gh` is not installed).

### 3.10 Observability (P1) — Done

- `app/monitoring/` — full OpenTelemetry instrumentation: `TracerProvider` +
  `MeterProvider` bootstrap, OTLP/HTTP export, `InstrumentedLLM` wrapper
  (spans + metrics per LLM call), `ObservabilityMiddleware` (HTTP request
  spans + metrics).
- Metrics: 4 counters + 4 histograms (LLM calls/tokens/duration, tool calls,
  task runs/duration, HTTP requests/duration).
- Token-level streaming: `_TokenSink` renders LLM output tokens live to the
  Rich console via the `on_token` callback chain.
- Config: `otel_enabled` (default off, opt-in), `otel_exporter_endpoint`,
  `otel_traces_enabled`, `otel_metrics_enabled`.

### 3.11 Evaluation framework (P1) — Done

- `app/evals/` — 6 benchmark tasks (fix-auth-bug, add-rest-endpoint,
  add-db-migration, fix-failing-test, optimize-query, find-security-issue),
  `EvalRunner` (scaffold → provision → orchestrator → verify), `ResultStore`
  (append-only JSONL), `summarize()` for model comparison.
- CLI: `engineer eval list|run|results|compare` with flags for workspace,
  keep, timeout, results path, model filter.
- Tests: unit tests for tasks/runner/results/CLI + integration test against
  real PostgreSQL.

### 3.12 Multi-agent (P2) — Done (Phase 18)

- Fixed pipeline (planner→coder→reviewer→tester) exists; the
  `CoordinatorAgent` adds intent-based dispatch to parallel read-only
  specialists (security, performance) confined to `READ_ONLY_TOOLS`, an
  optional sequential coder, and a synthesis step. Spec §22 largely closed;
  remaining: more specialist roles (e.g. testing) and on-demand mode
  selection.

### 3.13 Deployment (P2) — Partial

- `docker-compose.yml` (Postgres+Redis), `infra/Dockerfile` (API),
  `infra/executor/Dockerfile` (sandbox image), env-driven config, Alembic
  migrations. No K8s, no CI/CD pipeline, no autoscaling.

---

## 4. Architectural weaknesses & technical debt

| # | Weakness | Evidence | Impact | Recommended posture |
|---|----------|----------|--------|---------------------|
| 1 | **No interface independence yet** — everything hangs off FastAPI; no CLI/web/extension adapters, so "interface-independent core" is aspirational. | `app/gateway/` is the only entry point. | Spec §1/§5 unmet; blocks Web UI later. | Build CLI as a thin adapter over the orchestrator + a shared client library. |
| 2 | **Repository context is not available to agents** — agents re-derive the repo by hand every task. | `app/retrieval/` empty; `code_chunks` unused. | Wasted tokens; poor task-specific context; "generic chatbot" risk (spec §1). | P0 retrieval + context engine. |
| 3 | **`file_write` only, no `edit_file`** — whole-file rewrites risk unrelated churn. | `app/tools/specs/filesystem.py` has no edit tool. | Violates "minimize unnecessary changes" (spec §10). | Add diff-based `edit_file` (P0). |
| 4 | **Tester verdict is LLM-simulated, not real test execution.** | `app/agents/pipeline.py`; no test tool. | Test-and-repair loop (spec §13) impossible. | Add test tool + repair loop (P0). |
| 5 | **No approval gate / destructive-command confirmation.** | Orchestrator has no `require_approval`; sandbox runs any command. | Violates §9/§12/§30; risky for real repos. | Add confirmation/allow-deny layer (P0). |
| 6 | **Empty `memory/retrieval/evals/monitoring` packages** — names imply capabilities that do not exist. | Docstrings in `__init__.py` only. | Reputation/accuracy risk if presented as done. | Implement or clearly mark as roadmap stubs. |
| 7 | **`users`/`workspaces`/`sessions` tables but no ownership or auth** — multi-user data model with no protection. | Models exist; no auth routes; task endpoints are unauthenticated. | Security risk once deployed. | Phase 9 (auth + ownership). |
| 8 | **Token streaming defined but unused.** | `LLMStreamEvent` in `app/llm/protocol.py`. | CLI/UI can't show live output. | Wire `stream()` into the loop/SSE. |
| 9 | **Embedding provider not defined.** | No embedding abstraction; `CodeChunk.embedding` fixed at dim 1536. | Blocks retrieval. | Reuse the LLM-provider pattern for an `Embedder` protocol. |
| 10 | **`Settings` growth without groups.** | `app/core/config.py` is a flat pydantic-settings class. | Fine now; will bloat. | Acceptable; revisit when config is large. |

---

## 5. Security risks

1. **Unauthenticated API** — any host that can reach the gateway can run
   tasks against any workspace (see §4-7).
2. **No destructive-command confirmation** — the sandbox will happily run
   `rm -rf`, `git reset --hard`, etc. (mitigated only by being confined to the
   mounted workspace and no-network).
3. **`file_write` can overwrite any file in the workspace** without a diff or
   confirmation.
4. **Sandbox rootfs is read-only + no-network + non-root** — good baseline;
   remaining risk is the host git tools (confined via realpath, but write to
   the real checkout) and Docker-socket access for sandbox orchestration.
5. **No secrets handling** — `.env` holds `LLM_API_KEY`; no secret store, no
   key redaction in logs.

Mitigations already present: path-confined fs/git (`app/executor/paths.py`),
hardened git env, sandbox resource caps + timeouts, no-network containers.

---

## 6. Testing gaps

- **Strong today:** unit (agents, tools, LLM, config, paths, broker,
  cancellation), integration (repos, orchestrator, HTTP API + SSE), sandbox
  (real Docker). 150 tests; `mypy --strict` + `ruff` clean.
- **Missing:**
  - CLI tests (no CLI).
  - Repository indexing/retrieval tests (no indexer).
  - Test-and-repair loop tests (no loop).
  - Context-engine/retrieval-accuracy tests.
  - Evaluation-framework tests.
  - Auth/ownership tests (no auth).
  - Golden-path end-to-end test against a fixture repo (index → plan →
    edit → test → review).

---

## 7. Risks & constraints

| Risk | Severity | Mitigation |
|------|----------|------------|
| Scope creep — the spec is huge; building "everything" at once will stall | High | Strict P0-first execution; P0 alone makes the product genuinely useful; P1/P2 are additive. |
| Whole-file rewrites degrade the agent's output quality | Medium | `edit_file` + diff review before P1 modes depend on edits. |
| Real test execution needs sandbox reliability | Medium | Test tool reuses the proven sandbox; parse `pytest`/node output into structured results. |
| Embeddings depend on an external provider | Medium | Pluggable `Embedder` (local embedding model as default, OpenAI-compatible optional). |
| Auth touches every endpoint | Medium | Keep auth as its own work package with ownership checks; don't entangle it with P0. |
| "Fake AI" risk — reporting capabilities that do not exist | High | Gate every claim on tests; mark stubs as roadmap. |

---

## 8. Bottom line

The **agent runtime core is the project's moat and is largely done** (LLM
abstraction, LangGraph orchestration, sandboxed tools, durable persistence,
150 green tests). The product's missing surface is **everything the user sees
and everything that makes the agent *understand* a repository**: CLI,
repository intelligence + context engine, planning/approval, real test
execution + repair, and safety confirmations (P0); then review/audit modes,
memory, git/PR workflow, observability, evals, and auth (P1); then advanced
modes and interfaces (P2).

See `docs/TARGET_ARCHITECTURE.md` for the module mapping and the phased
implementation plan.
