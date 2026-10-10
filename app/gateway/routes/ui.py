# ruff: noqa: E501
"""Minimal web UI for the coding-agent gateway.

A dependency-free single-page app served at ``/ui``. It authenticates against
the JSON API (login/register), manages workspaces and sessions, and creates and
watches agent tasks: it shows each task's persisted transcript and polls until
the task reaches a terminal state. The HTML is embedded as a string so it needs
no static-file packaging; the API prefix is substituted at request time.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import get_settings

__all__ = ["render_ui", "router"]

router = APIRouter(tags=["ui"])

_UI_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>coding-agent</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #0d1117; color: #c9d1d9; }
  header { padding: 12px 16px; border-bottom: 1px solid #30363d;
           display: flex; gap: 12px; align-items: center; }
  header h1 { font-size: 15px; margin: 0; flex: 1; letter-spacing: .04em; }
  main { display: grid; grid-template-columns: 260px 260px 1fr;
         height: calc(100vh - 49px); }
  section { border-right: 1px solid #30363d; padding: 12px; overflow: auto; }
  section:last-child { border-right: 0; }
  #authPanel { grid-column: 1 / -1; max-width: 380px; margin: 60px auto 0;
               border: 1px solid #30363d; border-radius: 10px; height: fit-content; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
       color: #8b949e; margin: 0 0 8px; }
  input, textarea, button { font: inherit; background: #161b22; color: #c9d1d9;
       border: 1px solid #30363d; border-radius: 6px; padding: 6px 8px; width: 100%; }
  textarea { resize: vertical; min-height: 60px; }
  button { cursor: pointer; background: #21262d; width: auto; }
  button:hover { background: #30363d; }
  button.primary { background: #238636; border-color: #2ea043; color: #fff; }
  button.primary:hover { background: #2ea043; }
  .row { display: flex; gap: 6px; margin-bottom: 8px; }
  .item { padding: 6px 8px; border-radius: 6px; cursor: pointer; }
  .item:hover, .item.active { background: #161b22; }
  .muted { color: #8b949e; }
  .msg { border-left: 3px solid #30363d; padding: 6px 10px; margin: 8px 0;
         white-space: pre-wrap; overflow-wrap: anywhere; }
  .msg.user { border-color: #1f6feb; }
  .msg.assistant { border-color: #238636; }
  .msg.tool { border-color: #d29922; }
  .badge { font-size: 11px; padding: 2px 6px; border-radius: 999px;
           background: #21262d; vertical-align: middle; }
  .badge.completed { background: #1f6f2a; }
  .badge.failed { background: #7d1a1a; }
  .badge.running, .badge.pending { background: #6b4e16; }
  .grow { flex: 1; }
  .hidden { display: none; }
  #wsList, #sessionList, #taskList { margin-top: 8px; }
</style>
</head>
<body>
<header>
  <h1>coding-agent</h1>
  <span id="who" class="muted"></span>
  <button id="logout" class="hidden">log out</button>
</header>
<main>
  <section id="authPanel">
    <h2>sign in</h2>
    <div class="row"><input id="email" type="email" placeholder="you@example.com" autocomplete="username"></div>
    <div class="row"><input id="password" type="password" placeholder="password" autocomplete="current-password"></div>
    <div class="row">
      <button id="loginBtn" class="primary">log in</button>
      <button id="registerBtn">register</button>
    </div>
    <div id="authMsg" class="muted"></div>
  </section>

  <div id="workbench" class="hidden" style="display: contents">
    <section id="workspacePanel">
      <h2>workspaces</h2>
      <div class="row">
        <input id="wsName" placeholder="new workspace name">
        <button id="wsAdd">add</button>
      </div>
      <div id="wsList"></div>
    </section>

    <section id="sessionPanel">
      <h2>sessions</h2>
      <div class="row">
        <input id="sessionTitle" placeholder="new session title">
        <button id="sessionAdd">add</button>
      </div>
      <div id="sessionList"><div class="muted">select a workspace</div></div>
    </section>

    <section id="taskPanel">
      <h2>task</h2>
      <div class="row">
        <textarea id="taskGoal" placeholder="describe what the agent should do"></textarea>
      </div>
      <div class="row">
        <button id="taskAdd" class="primary">run task</button>
        <span id="taskMsg" class="muted"></span>
      </div>
      <div id="taskList"></div>
      <div id="detail"></div>
    </section>
  </div>
</main>
<script>
"use strict";
const API = "__API_PREFIX__";
const state = { token: localStorage.getItem("ca_token") || "", ws: null, session: null, task: null, poll: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, options) {
  options = options || {};
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (state.token) headers.Authorization = "Bearer " + state.token;
  const res = await fetch(API + path, Object.assign({}, options, { headers }));
  if (res.status === 401) { logout(); throw new Error("session expired, please sign in"); }
  if (!res.ok) {
    let detail = "";
    try { detail = JSON.stringify(await res.json()); } catch (e) { /* ignore */ }
    throw new Error(res.status + " " + detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function showWorkbench(visible) {
  $("workbench").classList.toggle("hidden", !visible);
  $("authPanel").classList.toggle("hidden", visible);
  $("logout").classList.toggle("hidden", !visible);
}

function logout() {
  state.token = ""; state.ws = state.session = state.task = null;
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  localStorage.removeItem("ca_token");
  $("who").textContent = "";
  showWorkbench(false);
}

async function bootstrap() {
  if (!state.token) { showWorkbench(false); return; }
  try {
    const me = await api("/auth/me");
    $("who").textContent = me.email;
    showWorkbench(true);
    await loadWorkspaces();
  } catch (e) { $("authMsg").textContent = e.message; }
}

async function auth(path) {
  $("authMsg").textContent = "";
  try {
    const body = JSON.stringify({ email: $("email").value, password: $("password").value });
    const data = await api(path, { method: "POST", body });
    state.token = data.access_token;
    localStorage.setItem("ca_token", state.token);
    $("password").value = "";
    $("who").textContent = data.user.email;
    showWorkbench(true);
    await loadWorkspaces();
  } catch (e) { $("authMsg").textContent = e.message; }
}

async function loadWorkspaces() {
  const items = await api("/workspaces");
  renderList($("wsList"), items, state.ws, (ws) => selectWorkspace(ws), (ws) => ws.name);
  if (items.length && !state.ws) selectWorkspace(items[0]);
}

function selectWorkspace(ws) {
  state.ws = ws; state.session = null; state.task = null;
  renderList($("wsList"), state._wsItems || [], ws, (w) => selectWorkspace(w), (w) => w.name);
  $("sessionList").innerHTML = '<div class="muted">loading...</div>';
  $("detail").innerHTML = ""; $("taskList").innerHTML = "";
  loadSessions();
}

async function loadSessions() {
  if (!state.ws) return;
  const items = await api("/workspaces/" + state.ws.id + "/sessions");
  state._sessionItems = items;
  renderList($("sessionList"), items, state.session, (s) => selectSession(s), (s) => s.title);
  if (!items.length) $("sessionList").innerHTML = '<div class="muted">no sessions yet</div>';
}

function selectSession(session) {
  state.session = session; state.task = null;
  renderList($("sessionList"), state._sessionItems || [], session, (s) => selectSession(s), (s) => s.title);
  $("detail").innerHTML = "";
  loadTasks();
}

async function loadTasks() {
  if (!state.session) return;
  const items = await api("/sessions/" + state.session.id + "/tasks");
  state._taskItems = items;
  renderList($("taskList"), items, state.task, (t) => selectTask(t), taskLabel);
  if (!items.length) $("taskList").innerHTML = '<div class="muted">no tasks yet</div>';
}

function taskLabel(t) {
  return '<span class="badge ' + esc(t.status) + '">' + esc(t.status) + '</span> ' + esc(t.goal.slice(0, 60));
}

async function createWorkspace() {
  const name = $("wsName").value.trim();
  if (!name || !state.token) return;
  await api("/workspaces", { method: "POST", body: JSON.stringify({ name }) });
  $("wsName").value = "";
  await loadWorkspaces();
}

async function createSession() {
  if (!state.ws) { $("taskMsg").textContent = "select a workspace first"; return; }
  const title = $("sessionTitle").value.trim() || "New session";
  const session = await api("/workspaces/" + state.ws.id + "/sessions", { method: "POST", body: JSON.stringify({ title }) });
  $("sessionTitle").value = "";
  await loadSessions();
  selectSession(session);
}

async function createTask() {
  if (!state.session) { $("taskMsg").textContent = "select or create a session first"; return; }
  const goal = $("taskGoal").value.trim();
  if (!goal) { $("taskMsg").textContent = "enter a goal"; return; }
  $("taskMsg").textContent = "";
  const task = await api("/sessions/" + state.session.id + "/tasks", { method: "POST", body: JSON.stringify({ goal, agent_type: "coder" }) });
  $("taskGoal").value = "";
  await loadTasks();
  selectTask(task);
}

function selectTask(task) {
  state.task = task;
  renderList($("taskList"), state._taskItems || [], task, (t) => selectTask(t), taskLabel);
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
  refreshTask();
  const terminal = ["completed", "failed", "cancelled"];
  if (!terminal.includes(task.status)) {
    state.poll = setInterval(refreshTask, 1500);
  }
}

async function refreshTask() {
  if (!state.task) return;
  try {
    const detail = await api("/tasks/" + state.task.id);
    state.task = detail;
    renderList($("taskList"), state._taskItems || [], detail, (t) => selectTask(t), taskLabel);
    renderDetail(detail);
    if (["completed", "failed", "cancelled"].includes(detail.status) && state.poll) {
      clearInterval(state.poll); state.poll = null;
    }
  } catch (e) { $("taskMsg").textContent = e.message; }
}

function renderDetail(task) {
  const parts = ['<h2>' + '<span class="badge ' + esc(task.status) + '">' + esc(task.status) + '</span> ' + esc(task.goal) + '</h2>'];
  if (task.error) parts.push('<div class="msg tool">' + esc(task.error) + "</div>");
  (task.messages || []).forEach((m) => {
    parts.push('<div class="msg ' + esc(m.role) + '"><strong>' + esc(m.role) + "</strong>\n" + esc(m.content) + "</div>");
  });
  if (!(task.messages || []).length) parts.push('<div class="muted">waiting for transcript...</div>');
  $("detail").innerHTML = parts.join("");
}

function renderList(container, items, selected, onSelect, label) {
  state._wsItems = container.id === "wsList" ? items : state._wsItems;
  container.innerHTML = "";
  items.forEach((item) => {
    const div = document.createElement("div");
    div.className = "item" + (selected && selected.id === item.id ? " active" : "");
    div.innerHTML = label(item);
    div.onclick = () => onSelect(item);
    container.appendChild(div);
  });
}

$("loginBtn").onclick = () => auth("/auth/login");
$("registerBtn").onclick = () => auth("/auth/register");
$("logout").onclick = logout;
$("wsAdd").onclick = createWorkspace;
$("sessionAdd").onclick = createSession;
$("taskAdd").onclick = createTask;
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") auth("/auth/login"); });
bootstrap();
</script>
</body>
</html>
"""


def render_ui(api_prefix: str) -> str:
    """Return the single-page UI HTML bound to ``api_prefix``.

    Args:
        api_prefix: Gateway API prefix (e.g. ``/api/v1``) the UI calls.
    """
    return _UI_TEMPLATE.replace("__API_PREFIX__", api_prefix)


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def get_ui() -> HTMLResponse:
    """Serve the single-page web UI."""
    return HTMLResponse(render_ui(get_settings().api_prefix))
