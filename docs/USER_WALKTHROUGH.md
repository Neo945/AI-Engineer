# User Walkthrough — How the Project Works

This guide explains the coding-agent from the perspective of the people who
use it: first how to get it running, then a complete walkthrough of a real
task from request to final answer, with the internal machinery explained at
each stage.

---

## 1. Before you start

The project is a **modular monolith**: one backend that contains the HTTP
API (gateway), the agent logic (orchestrator + LangGraph coder), the tool
layer (sandboxed executor), and the persistence (PostgreSQL + Redis).

It is **not yet a finished product** — there is no frontend, no login, and
no API to create workspaces/sessions. Today you interact with the running
backend over HTTP once a workspace and a session exist in the database.
The walkthrough in section 3 is the complete, current experience; section 4
describes the end-state vision.

### What you need

- Python 3.13+
- Docker with Compose v2

---

## 2. Running the backend

```bash
# 1. Provision infrastructure (PostgreSQL 16 + pgvector, Redis 7)
make up

# 2. Create a venv and install dependencies
make venv dev-install

# 3. Apply database migrations
make migrate

# 4. (Optional) build the sandbox image used by terminal tools
make executor-image

# 5. Run the API
make run
```

The API listens on `http://localhost:8000`; interactive docs are at
`http://localhost:8000/docs`.

### Configure the LLM

The agent is useless without an LLM. Set these in `.env` (copy from
`.env.example`) or as environment variables:

```bash
LLM_PROVIDER=openai            # or "anthropic"
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...             # your provider key
# LLM_BASE_URL=http://localhost:8000/v1   # for vLLM/Ollama local backends
```

**Note:** the API starts fine without these — they are only needed when you
run a task. If they are missing when you do, you get a clear `503`.

### Create a workspace and a session

Until workspace/session management is exposed over HTTP, seed one row of
each directly:

```bash
# Open a SQL shell (or use psql)
docker compose exec postgres psql -U coding -d coding_agent

-- Create a user, a workspace, and a session:
INSERT INTO users (id, email, full_name, created_at, updated_at)
VALUES (gen_random_uuid(), 'me@example.com', 'Me', now(), now());

-- Pick up the ids you just created (replace with the actual values):
INSERT INTO workspaces (id, owner_id, name, repo_path, created_at, updated_at)
VALUES (gen_random_uuid(), '<user-id>', 'my-repo', '/tmp/my-repo', now(), now());

INSERT INTO sessions (id, workspace_id, user_id, title, status, created_at, updated_at)
VALUES (gen_random_uuid(), '<workspace-id>', '<user-id>', 'My session', 'idle', now(), now());
```

Take note of the **session id** — you will pass it to the task API. The
workspace's `repo_path` is the directory the agent is allowed to touch; it
must exist on the host.

---

## 3. The end-to-end experience (what actually happens today)

### 3.1 The user's goal

Imagine a developer with a broken repository at `/tmp/my-repo`. They want
the agent to fix a bug. They send one request:

```bash
curl -X POST http://localhost:8000/api/v1/sessions/<session-id>/tasks \
  -H 'Content-Type: application/json' \
  -d '{"goal": "The README says the server runs on port 5000 but it runs on 8000. Fix the mismatch."}'
```

The request **blocks until the agent finishes**, then returns the task:

```json
{
  "id": "b5f1...",
  "session_id": "7c2e...",
  "parent_task_id": null,
  "agent_type": "coder",
  "status": "completed",
  "goal": "The README says the server runs on port 5000 but it runs on 8000. Fix the mismatch.",
  "result": "Fixed README. The server binds 0.0.0.0:8000, so the docs now say port 8000.",
  "error": null,
  "input_tokens": 1250,
  "output_tokens": 310,
  "started_at": "2026-08-08T09:45:00+00:00",
  "finished_at": "2026-08-08T09:45:07+00:00",
  "created_at": "2026-08-08T09:45:00+00:00",
  "updated_at": "2026-08-08T09:45:07+00:00"
}
```

### 3.2 What happened inside — stage by stage

| Stage | Who does it | What happens |
|-------|-------------|--------------|
| 1. Task created | Gateway | A `Task` row is inserted in `PENDING` state, owned by the session. |
| 2. Task starts | Orchestrator | Status flips to `RUNNING`, `started_at` is set, the session's workspace is resolved. |
| 3. First LLM call | CoderAgent | The goal becomes the first `user` message; the system prompt plus the tool catalog (`file_read`, `file_write`, `terminal_run`, `git_status`, …) is sent to the LLM. |
| 4. Tool calls | CoderAgent | The LLM asks for tools. The agent executes them **inside a sandbox container** — resource-capped, non-root, no network — and only inside the workspace directory. |
| 5. Loop | CoderAgent | Each result is fed back as a `tool` message; the LLM keeps going until it gives a final answer or hits the `max_steps` bound (default 8). |
| 6. Persist | Orchestrator | Every message is written to the transcript with a stable `ordinal`, plus tool-call ids/arguments and token counts. |
| 7. Finish | Orchestrator | Status → `COMPLETED` (or `FAILED` with the error captured), `result`/token totals/`finished_at` written. |

The agent never touches your machine directly — `file_write`, `git_commit`,
`terminal_run`, etc. all execute in the disposable sandbox, so a bad command
can't harm the host.

### 3.3 Inspecting what the agent did

Ask for the full transcript of any task:

```bash
curl http://localhost:8000/api/v1/tasks/<task-id>
```

This returns the task fields plus `messages`, in order:

```json
{
  "id": "b5f1...",
  "status": "completed",
  "messages": [
    { "role": "user",      "content": "The README says the server runs on port 5000...", "ordinal": 0 },
    { "role": "assistant", "content": "", "tool_calls": [ { "id": "tc1", "name": "file_search", "arguments": {"pattern": "5000"} } ], "ordinal": 1 },
    { "role": "tool",      "content": "Found 3 matches in README.md:560, docs/server.md:12", "tool_call_id": "tc1", "ordinal": 2 },
    { "role": "assistant", "content": "", "tool_calls": [ { "id": "tc2", "name": "file_write", "arguments": {"path": "README.md", "content": "...# port 8000..."} } ], "ordinal": 3 },
    { "role": "tool",      "content": "README.md updated.", "tool_call_id": "tc2", "ordinal": 4 },
    { "role": "assistant", "content": "Fixed README. The server binds 0.0.0.0:8000, so the docs now say port 8000.", "ordinal": 5 }
  ]
}
```

This transcript is durable: it survives restarts and is the source of truth
for "what did the agent do".

### 3.4 Browsing history

List every task a session has run, oldest first:

```bash
curl "http://localhost:8000/api/v1/sessions/<session-id>/tasks?limit=50&offset=0"
```

### 3.5 Health checks

```bash
curl http://localhost:8000/api/v1/healthz   # {"status": "ok"} always
curl http://localhost:8000/api/v1/readyz    # reports database + redis reachability
```

---

## 4. Error cases the user will meet

| Situation | What the user sees |
|-----------|--------------------|
| LLM not configured (`LLM_API_KEY` unset, no local `base_url`) | `503` with `"LLM is not configured; set LLM_PROVIDER, LLM_API_KEY, and/or LLM_BASE_URL"` |
| Session id doesn't exist | `404` `"session not found"` |
| Task id doesn't exist | `404` `"task not found"` |
| Goal missing/empty | `422` validation error from FastAPI |
| Agent run crashes mid-way | Task is persisted as `FAILED` with `error` set, e.g. `RuntimeError: ...`; the transcript up to the failure may be empty |

A failed run does **not** raise an HTTP 500 — the task record carries the
failure, so a polling client gets a consistent story.

---

## 5. The end-state vision (roadmap)

Today is the "agent core". The full product will look like this from the
user's point of view:

1. **Sign in** to a web app and link a Git repository.
2. The backend **clones** the repo into a sandbox workspace and indexes it
   (AST + embeddings for semantic search).
3. You **describe a task in chat** — fix a bug, add a feature, refactor.
4. A **planner agent** breaks it into steps; **coder, reviewer, and tester
   agents** execute the steps: edit files, run commands and tests in the
   sandbox, review the diff, and fix regressions.
5. The whole run **streams live** (SSE/WebSocket) — you watch tool calls and
   file edits as they happen, and can **cancel** or request a retry.
6. Finished work is presented as a **summary + diff**, with per-task token
   cost; you decide whether to keep the changes and push.

Everything already built (tool executor, LLM abstraction, coder loop,
durable transcripts, lifecycle events) is the foundation that this vision
stands on.
