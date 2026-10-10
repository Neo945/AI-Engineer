"""Unit tests for the legacy-modernization mode (engineer modernize)."""

from __future__ import annotations

from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.cli.commands import cmd_modernize
from app.cli.context import CliContext, CliError
from app.cli.main import build_parser
from app.core.config import Settings
from app.llm.messages import ChatMessage
from app.llm.protocol import LLMResponse
from app.modernization import (
    MODERNIZATION_PROMPT,
    build_modernization_seed,
    parse_modernization_report,
    render_modernization,
)
from app.tools.schemas import ToolSpec


def _settings() -> Settings:
    return Settings(_env_file=None)


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


def _repo(tmp_path: Path) -> Path:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "app.py").write_text("import pkg.core\n", encoding="utf-8")
    return tmp_path


def _modernize_answer() -> LLMResponse:
    content = (
        '{"summary": "Strangle the sync HTTP layer behind a port.", '
        '"current_state": "Handlers call requests directly.", '
        '"target_state": "Async httpx client behind an interface.", '
        '"assumptions": ["tests cover the happy path"], '
        '"migration_steps": [{"title": "Extract an HTTP port", '
        '"description": "Introduce a transport interface.", '
        '"risk": "callers may bypass the port", "effort": "2-3 days", '
        '"validation": "unit tests pass against the fake"}], '
        '"risks": [{"severity": "high", "file": "pkg/app.py", '
        '"problem": "sync calls block the event loop", '
        '"reason": "throughput collapse under load", '
        '"fix": "switch to httpx.AsyncClient first"}], '
        '"rollback_strategy": ["keep the old client behind a feature flag"], '
        '"validation": ["run the full suite against the new transport"], '
        '"follow_ups": ["add contract tests for the port"]}'
    )
    return LLMResponse(content=content, model="fake")


async def test_cmd_modernize_renders_report(tmp_path: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_modernize_answer()])

    code = await cmd_modernize(
        ctx, repo=_repo(tmp_path), state=None, target="httpx + asyncio", llm=llm
    )

    assert code == 0
    out = buffer.getvalue()
    assert "Strangle the sync HTTP layer" in out
    assert "## Current state" in out
    assert "## Target state" in out
    assert "## Migration plan" in out
    assert "**Extract an HTTP port**" in out
    assert "## Risk assessment" in out
    assert "**HIGH** (pkg/app.py)" in out
    assert "## Rollback strategy" in out
    assert "## Validation" in out
    assert "## Follow-ups" in out
    assert llm.calls[0]["system"] == MODERNIZATION_PROMPT
    assert "httpx + asyncio" in llm.calls[0]["messages"][0].content


async def test_cmd_modernize_prose_reply_degrades_gracefully(tmp_path: Path) -> None:
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([LLMResponse(content="Modernize the HTTP layer first.", model="fake")])

    code = await cmd_modernize(ctx, repo=_repo(tmp_path), state=None, llm=llm)

    assert code == 0
    assert "Modernize the HTTP layer first." in buffer.getvalue()


def test_parse_modernization_report_from_fenced_json() -> None:
    text = (
        "```json\n"
        '{"summary": "s", "target_state": "t", '
        '"migration_steps": [{"title": "step one", "effort": "1 day"}], '
        '"risks": [{"severity": "medium", "file": "x.py", "problem": "p"}], '
        '"rollback_strategy": ["flag off"]}\n'
        "```"
    )
    report = parse_modernization_report(text)
    assert report.summary == "s"
    assert report.target_state == "t"
    assert report.migration_steps[0].title == "step one"
    assert report.risks[0].severity.value == "medium"
    assert report.risks[0].file == "x.py"
    assert report.rollback_strategy == ["flag off"]


def test_parse_modernization_report_from_bare_json() -> None:
    text = '{"summary": "ok", "assumptions": ["a"], "follow_ups": ["b"]}'
    report = parse_modernization_report(text)
    assert report.summary == "ok"
    assert report.assumptions == ["a"]
    assert report.follow_ups == ["b"]


def test_parse_modernization_report_skips_malformed_entries() -> None:
    text = (
        '{"summary": "s", '
        '"migration_steps": [{"title": "good"}, "junk", 5, {"effort": "no title"}], '
        '"risks": [{"severity": "high", "file": "x", "problem": "p"}, "junk"], '
        '"assumptions": [1, "real", null]}'
    )
    report = parse_modernization_report(text)
    assert len(report.migration_steps) == 1
    assert report.migration_steps[0].title == "good"
    assert len(report.risks) == 1
    assert report.risks[0].problem == "p"
    assert report.assumptions == ["real"]


def test_parse_modernization_report_prose_fallback() -> None:
    report = parse_modernization_report("Just do the migration.\nSecond line.")
    assert report.summary == "Just do the migration."


def test_render_modernization_includes_sections() -> None:
    report = parse_modernization_report(_modernize_answer().content)
    body = render_modernization(report)
    for heading in (
        "## Current state",
        "## Target state",
        "## Migration plan",
        "## Risk assessment",
        "## Rollback strategy",
        "## Validation",
        "## Assumptions",
        "## Follow-ups",
    ):
        assert heading in body


def test_render_modernization_omits_empty_sections() -> None:
    report = parse_modernization_report('{"summary": "only a summary"}')
    body = render_modernization(report)
    assert body == "only a summary"
    assert "## Migration plan" not in body


def test_render_modernization_orders_risks_by_severity() -> None:
    report = parse_modernization_report(
        '{"summary": "s", "risks": ['
        '{"severity": "low", "file": "b.py", "problem": "minor"}, '
        '{"severity": "critical", "file": "a.py", "problem": "severe"}]}'
    )
    body = render_modernization(report)
    assert body.index("severe") < body.index("minor")


def test_build_modernization_seed_embeds_graph_and_target() -> None:
    seed = build_modernization_seed("pkg/app.py -> pkg/core.py", target="httpx")
    assert seed.startswith("Plan the modernization")
    assert "pkg/app.py -> pkg/core.py" in seed
    assert "Target stack: httpx" in seed


def test_build_modernization_seed_without_target() -> None:
    seed = build_modernization_seed("graph")
    assert "Target stack" not in seed


def test_modernization_prompt_mentions_the_json_contract() -> None:
    assert "migration_steps" in MODERNIZATION_PROMPT
    assert "rollback_strategy" in MODERNIZATION_PROMPT
    assert "risks" in MODERNIZATION_PROMPT


def test_parser_modernize_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["modernize"])
    assert args.command == "modernize"
    assert args.target is None


def test_parser_modernize_target() -> None:
    parser = build_parser()
    args = parser.parse_args(["modernize", "--target", "Python 3.13"])
    assert args.target == "Python 3.13"


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


async def test_cmd_modernize_llm_failure_is_friendly(tmp_path: Path) -> None:
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_modernize(ctx, repo=_repo(tmp_path), state=None, llm=_RaisingLLM())


async def test_cmd_modernize_unconfigured_llm_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_modernize(ctx, repo=_repo(tmp_path), state=None)
