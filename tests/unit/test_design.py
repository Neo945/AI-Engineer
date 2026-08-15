"""Unit tests for the system-design mode (engineer design)."""

from __future__ import annotations

from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.cli.commands import cmd_design
from app.cli.context import CliContext, CliError
from app.cli.main import build_parser
from app.core.config import Settings
from app.design import (
    DESIGN_PROMPT,
    build_design_seed,
    parse_design_report,
    render_design,
)
from app.llm.messages import ChatMessage
from app.llm.protocol import LLMResponse
from app.tools.schemas import ToolSpec


def _settings() -> Settings:
    return Settings(_env_file=None)


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


def _design_answer() -> LLMResponse:
    content = (
        '{"summary": "A stateless URL shortener on Postgres + Redis.", '
        '"assumptions": ["100 rps read-heavy"], '
        '"architecture": "client -> api -> Postgres, Redis for hot slugs", '
        '"components": [{"name": "api", "responsibility": "routing", '
        '"details": "FastAPI"}], '
        '"api": [{"method": "POST", "path": "/shorten", '
        '"request": "{\\"url\\": \\"...\\"}", "response": "201 slug", '
        '"notes": "idempotent by url"}], '
        '"data_model": [{"entity": "links", "fields": "slug PK, url", '
        '"notes": "unique on url"}], '
        '"events": ["link.created"], '
        '"caching": ["hot slugs, TTL 24h"], '
        '"failure_handling": ["retry with backoff"], '
        '"scaling": ["read replicas"], '
        '"observability": ["RED metrics"], '
        '"mermaid": ["flowchart TD\\n  A[client] --> B[api]\\n"], '
        '"risks": ["hot key skew"]}'
    )
    return LLMResponse(content=content, model="fake")


async def test_cmd_design_renders_report() -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_design_answer()])

    code = await cmd_design(ctx, repo=Path.cwd(), state=None, goal="a url shortener", llm=llm)

    assert code == 0
    out = buffer.getvalue()
    assert "stateless URL shortener" in out
    assert "## Architecture" in out
    assert "## API" in out
    assert "**POST /shorten**" in out
    assert "## Data model" in out
    assert "## Caching" in out
    assert "## Failure handling" in out
    assert "## Scaling" in out
    assert "## Observability" in out
    assert "## Assumptions" in out
    assert "## Risks" in out
    assert "## Diagrams" in out
    assert "```mermaid" in out
    assert llm.calls[0]["system"] == DESIGN_PROMPT
    assert "a url shortener" in llm.calls[0]["messages"][0].content


async def test_cmd_design_prose_reply_degrades_gracefully() -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([LLMResponse(content="Stateless API over Postgres.", model="fake")])

    code = await cmd_design(ctx, repo=Path.cwd(), state=None, goal="x", llm=llm)

    assert code == 0
    assert "Stateless API over Postgres." in buffer.getvalue()


def test_parse_design_report_from_fenced_json() -> None:
    text = (
        "```json\n"
        '{"summary": "s", "components": [{"name": "api", "responsibility": "r"}], '
        '"api": [{"method": "POST", "path": "/shorten"}], '
        '"data_model": [{"entity": "links", "fields": "slug PK"}], '
        '"mermaid": ["flowchart TD\\n  A --> B\\n"], "risks": ["hot keys"]}\n'
        "```"
    )
    report = parse_design_report(text)
    assert report.summary == "s"
    assert report.components[0].name == "api"
    assert report.api[0].method == "POST"
    assert report.api[0].path == "/shorten"
    assert report.data_model[0].entity == "links"
    assert report.mermaid == ["flowchart TD\n  A --> B"]
    assert report.risks == ["hot keys"]


def test_parse_design_report_from_bare_json() -> None:
    text = '{"summary": "ok", "events": ["link.created"], "scaling": ["replicas"]}'
    report = parse_design_report(text)
    assert report.summary == "ok"
    assert report.events == ["link.created"]
    assert report.scaling == ["replicas"]


def test_parse_design_report_skips_malformed_entries() -> None:
    text = (
        '{"summary": "s", "components": [{"name": "good"}, "junk", 5], '
        '"api": [{"method": "GET", "path": "/x"}, {"path": "/no-method"}], '
        '"events": [1, "link.created", null]}'
    )
    report = parse_design_report(text)
    assert len(report.components) == 1
    assert report.components[0].name == "good"
    assert len(report.api) == 1
    assert report.api[0].path == "/x"
    assert report.events == ["link.created"]


def test_parse_design_report_prose_fallback_recovers_mermaid() -> None:
    text = "A simple system.\n```mermaid\nsequenceDiagram\n  A->>B: ping\n```\n"
    report = parse_design_report(text)
    assert report.summary == "A simple system."
    assert len(report.mermaid) == 1
    assert "sequenceDiagram" in report.mermaid[0]


def test_render_design_includes_sections_and_diagrams() -> None:
    report = parse_design_report(_design_answer().content)
    body = render_design(report)
    for heading in (
        "## Architecture",
        "## Components",
        "## API",
        "## Data model",
        "## Events",
        "## Caching",
        "## Failure handling",
        "## Scaling",
        "## Observability",
        "## Assumptions",
        "## Risks",
        "## Diagrams",
    ):
        assert heading in body
    assert "```mermaid" in body


def test_render_design_omits_empty_sections() -> None:
    report = parse_design_report('{"summary": "only a summary"}')
    body = render_design(report)
    assert body == "only a summary"
    assert "## API" not in body


def test_build_design_seed_embeds_the_goal() -> None:
    seed = build_design_seed("a URL shortener")
    assert seed.startswith("Design a system")
    assert "a URL shortener" in seed


def test_design_prompt_mentions_the_json_contract() -> None:
    assert "components" in DESIGN_PROMPT
    assert "mermaid" in DESIGN_PROMPT
    assert "failure_handling" in DESIGN_PROMPT


def test_parser_design_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["design", "a", "url", "shortener"])
    assert args.command == "design"
    assert args.goal == ["a", "url", "shortener"]


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


async def test_cmd_design_llm_failure_is_friendly() -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_design(ctx, repo=Path.cwd(), state=None, goal="x", llm=_RaisingLLM())


async def test_cmd_design_unconfigured_llm_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_design(ctx, repo=Path.cwd(), state=None, goal="x")
