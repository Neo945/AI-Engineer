# Knowledge Base — coding-agent development

This document consolidates the full development conversation for the
`coding-agent` project: what was built, phase by phase, with commits,
architecture, recurring patterns, verification workflow, and the current
state of the work. It is the single reference for resuming the session.

## 1. Project snapshot

| | |
|---|---|
| **Repo** | `/Users/shreeshsrivastava/Downloads/projects/AI Engineer/coding-agent` (note: `coding-agent`, not "codingagnet") |
| **Git branch** | `main`, **8 commits ahead of `origin/main`** (unpushed: Phases 14–17 + 3 commits) |
| **Python** | 3.13, `asyncio` (single event loop per CLI run) |
| **Dependencies** | FastAPI, SQLAlchemy 2 async + Alembic, PostgreSQL (pgvector), Redis, LangGraph, Pydantic v2, structlog, OpenTelemetry, rich (CLI), httpx |
| **Infra** | `docker compose` (PostgreSQL + Redis), `infra/executor/Dockerfile` sandbox image |
| **Status line** | Phase 17 (distributed-systems analysis) complete; all P0 and P1 items done |

The repo was previously at `~/Documents/Default Project` in an opencode
session; the real working directory is the Downloads repo. The opencode
working directory (`~/Documents/Default Project`) only ever held
`opencode.json` — all project work happens in the Downloads repo.

## 2. Architecture

A **modular monolith**: one deployable with strictly separated feature
packages, extractable into services later.

```
React frontend  ->  Gateway (auth, rate-limit, WS)  ->  Orchestrator (LangGraph)
                                                      | agents (planner/coder/reviewer/tester)
                                                      v tools -> Executor (sandboxed containers)
                                                      | retrieval (AST + embeddings)
                                                      | memory
                                                      v
                                          PostgreSQL (pgvector) / Redis
```

### Package / phase map

| Package | Purpose | Phase |
|---|---|---|
| `gateway` | FastAPI app factory, routes, request dependencies | 1–3 |
| `core` | settings, structured logging, DI container | 1–3 |
| `database` | async engine, sessions, ORM base, Alembic migrations | 1–3 |
| `tools` | typed tool contracts | 4 |
| `executor` | sandboxed tool execution, git, test parsing | 4 |
| `orchestrator` | agent execution graph (LangGraph) + event bus + retry/cancel | 5–7 |
| `agents` | shared loop + planner/coder/reviewer/tester pipeline | 5–8 |
| `llm` | provider abstraction + context management | 5 |
| `retrieval` | AST indexing, embeddings, semantic search | 9 |
| `memory` | conversation/repo/preferences/long-term | 10 |
| `evals` | headless SWE-bench-style task harness | 11 |
| `monitoring` | OTel traces + metrics, no-op by default | 12 |
| `git` | git tools, dirty-tree protection, PR/commit drafting | 13 |
| `audit` | staff-engineer production-readiness scoring | 14 |
| `architecture` | dependency graph + LLM architecture analysis | 15 |
| `design` | system-design mode with Mermaid diagrams | 16 |
| `analysis` | distributed-systems concern scan + LLM analysis | 17 |

## 3. Conversation timeline (oldest → newest)

Phases 1–7 are the foundation (gateway, core, database, tools, executor,
orchestrator, agents, llm) and predate the visible git log.

| Commit | Phase | What was done |
|---|---|---|
| `6548500` | 8 | Multi-agent pipeline: planner → coder → reviewer → tester in one LangGraph graph, rejection routes back to coder (bounded by `pipeline_max_passes`) |
| `8ab47b1` | P0-A | Developer CLI over the shared agent core (`engineer`) |
| `8039291` | P0-B | Repository indexing: discover, chunk, and search source files |
| `6c85161` | P0-C | Context retrieval: ranked search and task context seeding |
| `34e78fd` `702217b` `617d348` `a010a96` | P0-D | Task plan artifact + approval columns (migration 0006), `PlannerAgent` + structured `TaskPlan`, orchestrator `plan_task`/`approve_task`/`reject_task` + run gate, `engineer run -y` auto-approve + plan panel + plan/approve/reject/run gateway endpoints |
| `cc0982b` | P0-E | Sandbox command safety policy: deny/confirm tiers for destructive commands |
| `e8c644f` | P0-E | `file_edit` tool: surgical unified-diff edits with exact-match application |
| `9101ef0` | P0-F | `test_run` tool: structured pytest/jest output parsing with failure reports |
| `c3d23c1` | P0-F | Test-and-repair loop: `RepairAgent` drives tester stage, bounded fix→re-run iterations |
| `9841806` | — | CLI `test`/`review` commands: sandboxed test runs with auto-detect, `--fix` repair loop, structured review with verdict exit codes |
| `3d5d7fd` | — | Streaming token output: `LiveLLM.stream()` + `on_token` callbacks, `LoopAgent` fallback, CLI token sink |
| `0e9a6fb` | — | Reviewer verdict robustness: clear first-line verdict prompt, re-ask when omitted, lenient `parse_verdict` |
| `84c2860` | — | Friendly LLM errors in review/test `--fix` + repair-loop streaming coverage |
| `4d9a558` | 9 | **Auth + resource ownership**: register/login/me + password change, user-scoped workspace & session CRUD, ownership checks on task endpoints (stdlib PBKDF2 + HMAC-SHA256 tokens) |
| `d3a6334` | 10–11 | **Durable memory, headless evals, structured review findings**: `MemoryService` recall/remember/clear over pgvector `memory_entries` with orchestrator injection and `engineer memory add/list/recall/clear`; eval harness running SWE-bench-style tasks into JSONL results with `engineer eval list/run/results/compare`; structured review findings parsed from the VERDICT line + JSON findings block into severity-ordered tables |
| `097d078` | 11 | Fix: surface eval errors, close LLM/sandbox resources, stabilize flaky token test |
| `052b8e7` | 12 | **OpenTelemetry traces and metrics** (LLM calls, tool executions, task runs, HTTP requests); no-op by default; `OTEL_ENABLED` + `OTEL_EXPORTER_ENDPOINT` |
| `1abfac3` | 13 | **Git workflow**: `git_log`/`git_branch`/`git_checkout`/`git_push` tools; pre-modification dirty-tree protection; `engineer commit --generate`; `engineer pr` (title/body, push, `gh`) |
| `c246d5e` | 14 | **Staff-engineer audit** (`engineer audit`): production-readiness scores (correctness/security/performance/maintainability/testability/observability/deployment) out of 100 with cited evidence; PASS/CHANGES_NEEDED verdict derived from scores |
| `5fe4991` | 15 | **Dependency graph + LLM architecture analysis**: `app/architecture/` (`deps`, `render`, `report`); fixed 3 `deps.py` bugs (DependencyEdge `order=True`, TS `./x` resolution, Python absolute-import tree search); `engineer graph` (text/`--mermaid`/`--include-unresolved`/`--max-nodes`/focus node/`--depth`) + `engineer arch` |
| `1748c76` | 16 | **System-design mode** (`engineer design "<goal>"`): `app/design/` (`DesignReport`: summary, assumptions, architecture, components, API, data model, events, caching, failure handling, scaling, observability, Mermaid, risks); repo-independent (works outside a bound workspace) |
| `bad4421` | 17 | **Distributed-systems analysis** (`engineer analyze`): `app/analysis/` (`scan.py` deterministic concern scanner for sync/async HTTP, retries, idempotency, concurrency, locking, caching, timeouts, circuit breakers, messaging; `report.py` LLM interpretation into `AnalysisReport` with `ReviewFinding` findings + recommendations); `--scan-only` skips the LLM |

**Git state note:** `origin/main` is at `4d9a558` (auth). Commits
`d3a6334` → `bad4421` (8 commits) are local-only as of this KB.

## 4. Recurring patterns and conventions

These hold across every recent phase and should be followed when continuing:

### The LLM-driven mode pattern
Each mode (audit/arch/design/analyze) follows the same shape:
1. A **deterministic artifact** grounds the model (working-tree diff for
   audit, dependency-graph summary for arch, scan evidence for analyze,
   nothing for design).
2. A module-level `*_PROMPT` system prompt (staff persona + JSON contract
   in a fenced code block).
3. `build_*_seed(...)` builds the user message.
4. `parse_*_report(...)` **degrades gracefully**: fenced ` ```json ` block
   preferred, then bare JSON; prose fallback still yields a summary; Mermaid
   fenced blocks recovered; malformed entries are skipped, never fatal.
5. `render_*` returns Markdown for `ctx.console.print(...)`.
6. A thin `cmd_*` in `app/cli/commands.py` + subparser + `_cmd_*` handler
   in `app/cli/main.py`.

### Findings are shared
`app/review/findings.py` owns `ReviewFinding` (severity critical…nit, file,
line, problem, reason, fix) plus `parse_findings`, `sort_findings`,
`format_findings`, `extract_verdict`. Audit and analyze reuse it so findings
rendering is uniform across modes.

### Workspace binding rules
- **Repo-dependent modes** (review, audit, graph, arch, analyze, run, test,
  commit, pr, memory): require `engineer init` first (`load_state(repo)`),
  resolve via `find_repo_root(Path.cwd())`.
- **Repo-independent modes** (eval, design): `arun` special-cases them to
  `repo = Path.cwd()`, `state = None`.
- `engineer analyze` is repo-dependent (scans the workspace); `--scan-only`
  needs no LLM.

### LLM plumbing
- `build_llm_client(settings)` via `app/llm/factory`; commands take an
  optional `llm: LLMProvider` param so tests inject `FakeLLM`.
- `llm_owned` pattern: if the caller passed no LLM, the command owns it and
  closes it in `finally`.
- `_build_llm(ctx)` raises `CliError` with `_llm_failure_hint` /
  `LLM_UNCONFIGURED_HINT` for unconfigured/missing providers; `arun`
  renders `CliError` as `error: <msg>` → exit 1.

### Tooling / verification (always run before committing)
```
make lint        # ruff check + format check over app+tests
make typecheck   # mypy (strict) over app
make test-unit   # pytest -m "not integration" (no infra)
make test        # full suite (needs make up + executor image)
```
Unit tests must pass with a mock LLM (`tests/unit/fake_llm.py`), no infra.
End-to-end smoke tests use `run_cli([...])` in a temp git repo after
`engineer init` with `PYTHONPATH` + `.venv/bin/python`.

### Commit style
`feat: <summary> (Phase N)` with a body summarizing what was built, key
decisions, and verification. Phase commits have been made incrementally
(one phase per commit).

## 5. CLI reference (`engineer`)

```
engineer "task"                       # plan+approve+run an agent task
engineer run -y "task"                # auto-approve a planned run
engineer init                         # bind the current repo (creates .engineer/state.json)
engineer status                       # bound workspace, branch, recent activity
engineer diff                         # working-tree diff
engineer commit -m msg | --generate   # commit (LLM-drafted message)
engineer pr [--draft --title]         # PR description + push + open via gh
engineer test [--command] [--framework] [--fix] [--repairs]
engineer review [--ref]               # PASS/CHANGES_NEEDED verdict + findings
engineer audit [--ref] [--max-steps]  # production-readiness scores + verdict
engineer graph [node] [--mermaid] [--include-unresolved] [--max-nodes] [--depth]
engineer arch [--mermaid]
engineer design "<goal>"              # repo-independent
engineer analyze [--scan-only]
engineer memory add|list|recall|clear [--kind]
engineer eval list|run|results|compare [--model]
```

## 6. Known issues / caveats

- `make lint` format-check is **red on pre-existing files**: `app/agents/pipeline.py`
  and `tests/unit/test_security.py` have formatting drift untouched by
  recent phases (verified via `git stash`). `ruff check` and `mypy` are
  clean.
- Local LLM config in `.env`/`.env.example` points at the opencode zen
  endpoint (model `big-pickle`). Tests never hit the network (`FakeLLM`).
- The `async_http` scan pattern in `app/analysis/scan.py` requires an
  HTTP-ish client identifier (client/session/http/connector/api/svc/
  service/sdk/gateway) to avoid flagging `container.delete(...)` etc.

## 7. Roadmap

All P0 and P1 items are complete. Remaining:
- **P1**: Auth UI polish is optional; everything else done.
- **P2**: coordinator + parallel agents (item 16); legacy modernization /
  security / perf analysis modes (item 17); MCP tool integration (item 18);
  deployment CI/CD + K8s (item 19); Web UI / VS Code extension (item 20).

Suggested next step from this session: **Phase 18 — coordinator + parallel
agents** (P2 item 16), or auth UI. P2 items are additive and lower priority;
confirm scope before starting.

## 8. Working agreements for this session

- Work only in `/Users/shreeshsrivastava/Downloads/projects/AI Engineer/coding-agent`.
- Verify with `make lint`/`make typecheck`/`make test-unit`; expect the two
  pre-existing format failures noted in §6.
- Commit per phase with `feat: <summary> (Phase N)`; do not push unless asked.
- Ask before touching infra (docker), migrations, or dependency files.
