"""Unit tests for the architecture analysis (dependency graph + LLM report)."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.architecture import (
    ARCHITECTURE_PROMPT,
    build_architecture_seed,
    build_file_graph,
    cycles,
    dependencies,
    dependents,
    graph_summary,
    hub_files,
    layers,
    neighborhood,
    orphan_files,
    parse_architecture_report,
    render_architecture,
    render_graph_mermaid,
    render_graph_text,
    render_node_mermaid,
    render_node_text,
)
from app.cli.commands import cmd_arch, cmd_graph
from app.cli.context import CliContext, CliError, WorkspaceState
from app.cli.main import build_parser
from app.core.config import Settings
from app.llm.messages import ChatMessage
from app.llm.protocol import LLMResponse
from app.tools.schemas import ToolSpec


def _settings() -> Settings:
    return Settings(_env_file=None)


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _state(repo: Path) -> WorkspaceState:
    return WorkspaceState(
        repo_path=str(repo),
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )


def _scaffold(repo: Path) -> None:
    files = {
        "src/a.py": "import b\n",
        "src/b.py": "import a\n",
        "src/c.py": "import b\n",
        "src/d.py": "value = 1\n",
        "src/e.py": "import os\nimport requests\n",
        "src/util.py": "import math\n",
        "web/index.ts": 'import { helper } from "./dep";\nimport "./side";\n',
        "web/dep.ts": "export const helper = 1;\n",
        "web/side.ts": "console.log('loaded');\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path)
    _scaffold(repo)
    return repo


# --- Dependency graph ------------------------------------------------------


def test_build_file_graph_extracts_python_edges(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    assert set(graph.nodes) == {
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "src/d.py",
        "src/e.py",
        "src/util.py",
        "web/index.ts",
        "web/dep.ts",
        "web/side.ts",
    }
    assert ("src/a.py", "src/b.py") in {(edge.source, edge.target) for edge in graph.edges}
    assert ("src/b.py", "src/a.py") in {(edge.source, edge.target) for edge in graph.edges}
    assert dependencies(graph, "src/a.py") == ["src/b.py"]
    assert dependencies(graph, "src/c.py") == ["src/b.py"]
    assert dependents(graph, "src/b.py") == ["src/a.py", "src/c.py"]
    assert dependencies(graph, "src/d.py") == []
    assert dependencies(graph, "src/e.py") == []


def test_build_file_graph_resolves_typescript_imports(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    assert dependencies(graph, "web/index.ts") == ["web/dep.ts", "web/side.ts"]
    assert "web/side.ts" in graph.nodes


def test_cycles_and_hubs_and_orphans(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    assert cycles(graph) == [("src/a.py", "src/b.py")]
    assert ("src/b.py", 2) in hub_files(graph)
    orphans = orphan_files(graph)
    assert "src/d.py" in orphans
    assert "src/util.py" in orphans
    assert "src/a.py" not in orphans


def test_layers_assigns_every_node(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    depth = layers(graph)
    assert set(depth) == set(graph.nodes)
    assert depth["src/d.py"] == 1
    # Cycle participants share one layer, deeper than their dependents.
    assert depth["src/a.py"] == depth["src/b.py"] == 2
    assert depth["src/c.py"] < depth["src/b.py"]


def test_neighborhood_includes_dependencies_and_dependents(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    assert neighborhood(graph, "src/c.py") == {"src/c.py", "src/b.py"}
    assert "src/a.py" in neighborhood(graph, "src/c.py", depth=2)


def test_graph_summary_is_compact_and_readable(workspace: Path) -> None:
    summary = graph_summary(build_file_graph(workspace))
    assert "internal dependency edges" in summary
    assert "src/a.py -> src/b.py -> src/a.py" in summary
    assert "unresolved (external) imports" in summary


def test_unresolved_external_imports_are_collected(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    assert graph.unresolved["src/e.py"] == ("os", "requests")
    assert graph.unresolved["src/util.py"] == ("math",)
    assert "src/b.py" not in graph.unresolved


def test_extract_imports_uses_ast_and_skips_syntax_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "broken.py").write_text("def broken(:\n")
    graph = build_file_graph(repo)
    assert graph.nodes == ("broken.py",)
    assert graph.edges == ()


# --- Rendering -------------------------------------------------------------


def test_render_graph_text_lists_files_and_counts(workspace: Path) -> None:
    text = render_graph_text(build_file_graph(workspace))
    assert "files: 9, internal dependency edges: 5" in text
    assert "python=6, typescript=3" in text
    assert "  src/a.py" in text


def test_render_graph_mermaid_uses_positional_ids(workspace: Path) -> None:
    mermaid = render_graph_mermaid(build_file_graph(workspace))
    assert mermaid.startswith("flowchart TD")
    assert "n0[src/a.py]" in mermaid
    assert "n0 --> n1" in mermaid
    assert "-->" in mermaid


def test_render_graph_mermaid_includes_unresolved_stubs(workspace: Path) -> None:
    mermaid = render_graph_mermaid(
        build_file_graph(workspace), include_unresolved=True, max_nodes=6
    )
    assert "-.-> os" in mermaid
    assert "-.-> requests" in mermaid


def test_render_graph_mermaid_caps_nodes_and_drops_edges(workspace: Path) -> None:
    mermaid = render_graph_mermaid(build_file_graph(workspace), max_nodes=4)
    assert mermaid.count("n[") == 0
    # Only edges between rendered nodes appear; src/b.py is node n1 and kept.
    assert "n2 --> n1" in mermaid
    assert "n0 --> n1" in mermaid


def test_render_node_text_and_mermaid(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    text = render_node_text(graph, "src/a.py")
    assert "imports:\n  src/b.py" in text
    assert "imported by:\n  src/b.py" in text
    mermaid = render_node_mermaid(graph, "src/a.py")
    assert "src_a_py[src/a.py]" in mermaid
    assert "src_b_py --> src_a_py" in mermaid


def test_render_node_unknown_file_raises(workspace: Path) -> None:
    graph = build_file_graph(workspace)
    with pytest.raises(ValueError, match="unknown file"):
        render_node_text(graph, "missing.py")
    with pytest.raises(ValueError, match="unknown file"):
        render_node_mermaid(graph, "missing.py")


# --- Report ----------------------------------------------------------------


def test_parse_architecture_report_from_fenced_json() -> None:
    text = """\
Analysis:
```json
{"summary": "Modular monolith.", "components": [
  {"name": "core", "files": ["app/core/config.py"], "responsibility": "config"}],
 "layers": ["gateway", "core"],
 "key_files": [{"path": "app/core/config.py", "role": "hub"}],
 "recommendations": ["reduce hub coupling"],
 "mermaid": "flowchart TD\\n  A[gateway] --> B[core]\\n"}
```
"""
    report = parse_architecture_report(text)
    assert report.summary == "Modular monolith."
    assert report.components[0].name == "core"
    assert report.components[0].files == ["app/core/config.py"]
    assert report.layers == ["gateway", "core"]
    assert report.key_files[0].path == "app/core/config.py"
    assert report.recommendations == ["reduce hub coupling"]
    assert "A[gateway]" in report.mermaid


def test_parse_architecture_report_from_bare_json() -> None:
    text = '{"summary": "ok", "components": [{"name": "x", "files": ["x.py"]}]}'
    report = parse_architecture_report(text)
    assert report.summary == "ok"
    assert report.components[0].name == "x"


def test_parse_architecture_report_skips_malformed_entries() -> None:
    text = (
        '{"summary": "s", "components": [{"name": "good"}, "junk", 5], '
        '"key_files": [{"path": "ok.py"}, {"name": "no-path"}]}'
    )
    report = parse_architecture_report(text)
    assert len(report.components) == 1
    assert report.components[0].name == "good"
    assert [key.path for key in report.key_files] == ["ok.py"]


def test_parse_architecture_report_prose_fallback() -> None:
    report = parse_architecture_report("The system is a modular monolith.")
    assert report.summary == "The system is a modular monolith."
    assert report.components == []
    assert report.mermaid == ""


def test_parse_architecture_report_recovers_mermaid_from_prose() -> None:
    text = "No json.\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    report = parse_architecture_report(text)
    assert report.summary == "No json."
    assert "flowchart TD" in report.mermaid


def test_render_architecture_markdown_sections() -> None:
    report = parse_architecture_report(
        '{"summary": "s", "components": [{"name": "core", "files": ["a.py"], '
        '"responsibility": "config"}], "layers": ["gateway"], '
        '"key_files": [{"path": "a.py", "role": "hub"}], '
        '"recommendations": ["one", "two"]}'
    )
    body = render_architecture(report)
    assert "## Components" in body
    assert "## Layers" in body
    assert "## Key files" in body
    assert "## Recommendations" in body
    assert "1. one" in body
    assert "2. two" in body


def test_build_architecture_seed_embeds_the_graph_summary(workspace: Path) -> None:
    seed = build_architecture_seed(graph_summary(build_file_graph(workspace)))
    assert seed.startswith("Analyze the architecture")
    assert "internal dependency edges" in seed


def test_architecture_prompt_mentions_the_json_contract() -> None:
    assert "components" in ARCHITECTURE_PROMPT
    assert "key_files" in ARCHITECTURE_PROMPT
    assert "mermaid" in ARCHITECTURE_PROMPT


# --- CLI -------------------------------------------------------------------


def test_parser_graph_and_arch() -> None:
    parser = build_parser()
    graph = parser.parse_args(["graph", "app/x.py", "--mermaid", "--max-nodes", "10"])
    assert graph.node == "app/x.py"
    assert graph.mermaid is True
    assert graph.max_nodes == 10
    arch = parser.parse_args(["arch", "--mermaid"])
    assert arch.mermaid is True


async def test_cmd_graph_prints_text_report(workspace: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    code = await cmd_graph(ctx, repo=workspace, state=_state(workspace))
    assert code == 0
    out = buffer.getvalue()
    assert "files: 9, internal dependency edges: 5" in out
    assert "  src/a.py" in out


async def test_cmd_graph_mermaid_output(workspace: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    code = await cmd_graph(ctx, repo=workspace, state=_state(workspace), mermaid=True)
    assert code == 0
    assert buffer.getvalue().startswith("flowchart TD")


async def test_cmd_graph_focus_node(workspace: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    code = await cmd_graph(ctx, repo=workspace, state=_state(workspace), node="src/a.py")
    assert code == 0
    out = buffer.getvalue()
    assert "src/a.py" in out
    assert "src/b.py" in out


async def test_cmd_graph_unknown_node_is_friendly(workspace: Path) -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="unknown file in graph"):
        await cmd_graph(ctx, repo=workspace, state=_state(workspace), node="missing.py")


def _arch_answer() -> LLMResponse:
    content = (
        '{"summary": "A modular monolith.", "components": '
        '[{"name": "core", "files": ["src/b.py"], "responsibility": "hub"}], '
        '"layers": ["app", "core"], '
        '"recommendations": ["break the a/b cycle"]}'
    )
    return LLMResponse(content=content, model="fake")


async def test_cmd_arch_renders_report(workspace: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_arch_answer()])

    code = await cmd_arch(ctx, repo=workspace, state=_state(workspace), llm=llm)

    assert code == 0
    out = buffer.getvalue()
    assert "A modular monolith." in out
    assert "## Components" in out
    assert "## Layers" in out
    assert "## Recommendations" in out
    assert "break the a/b cycle" in out
    assert llm.calls[0]["system"] == ARCHITECTURE_PROMPT
    assert "internal dependency edges" in llm.calls[0]["messages"][0].content


async def test_cmd_arch_prose_reply_degrades_gracefully(workspace: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([LLMResponse(content="It is a well-layered monolith.", model="fake")])

    code = await cmd_arch(ctx, repo=workspace, state=_state(workspace), llm=llm)

    assert code == 0
    assert "well-layered monolith" in buffer.getvalue()


class _RaisingLLM(FakeLLM):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        raise RuntimeError("boom")


async def test_cmd_arch_llm_failure_is_friendly(workspace: Path) -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_arch(ctx, repo=workspace, state=_state(workspace), llm=_RaisingLLM())


async def test_cmd_arch_unconfigured_llm_is_friendly(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_arch(ctx, repo=workspace, state=_state(workspace))
