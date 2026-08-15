"""Unit tests for the distributed-systems analysis (engineer analyze)."""

from __future__ import annotations

from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from tests.unit.fake_llm import FakeLLM

from app.analysis import (
    ANALYSIS_PROMPT,
    build_analysis_seed,
    parse_analysis_report,
    render_analysis,
    render_scan,
    scan_distributed_systems,
    scan_summary,
)
from app.cli.commands import cmd_analyze
from app.cli.context import CliContext, CliError
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


def _scaffold(root: Path) -> None:
    files = {
        "client.py": (
            "@retry(max_attempts=5, backoff=2)\n"
            "def fetch(url):\n"
            "    return requests.get(url, timeout=5)\n"
        ),
        "async_client.py": (
            "async def call():\n"
            "    async with aiohttp.ClientSession() as session:\n"
            "        return await session.get('http://svc/x')\n"
        ),
        "worker.py": (
            "import asyncio\n"
            "async def fan_out():\n"
            "    await asyncio.gather(*tasks)\n"
            "    await asyncio.wait_for(coro, timeout=1)\n"
            "    async with asyncio.Lock():\n"
            "        pass\n"
        ),
        "publisher.py": (
            "def send_event():\n"
            "    broker.publish('order.created', payload)\n"
            "    cache.set('slug', url, ttl=3600)\n"
        ),
        "db.py": "idempotency_key = request.headers.get('Idempotency-Key')\n",
        "resilience.py": "from tenacity import retry\n@retry\n@fallback\n",
        "plain.py": "def hello():\n    return 'ok'\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_scan_distributed_systems_flags_concerns(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scan = scan_distributed_systems(tmp_path)

    assert scan.files_scanned == 7
    hits = {concern.concern: concern.total for concern in scan.concerns}

    assert hits["sync_http"] == 1
    assert hits["async_http"] == 1
    assert hits["retries"] == 3
    assert hits["idempotency"] == 1
    assert hits["concurrency"] == 1
    assert hits["locking"] == 1
    assert hits["caching"] == 1
    assert hits["timeouts"] == 2
    assert hits["circuit_breaker"] == 1
    assert hits["messaging"] == 1


def test_scan_distributed_systems_hits_cite_file_and_line(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scan = scan_distributed_systems(tmp_path)

    sync_hits = scan.hits("sync_http")
    assert len(sync_hits) == 1
    hit = sync_hits[0]
    assert hit.file == "client.py"
    assert hit.line == 3
    assert "requests.get" in hit.evidence


def test_scan_distributed_systems_empty_repo_reports_no_concerns(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    scan = scan_distributed_systems(tmp_path)

    assert scan.files_scanned == 1
    assert all(concern_scan.total == 0 for concern_scan in scan.concerns)


def test_scan_summary_embeds_counts_and_evidence(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scan = scan_distributed_systems(tmp_path)

    summary = scan_summary(scan)
    assert "scanned 7 source files" in summary
    assert "sync_http: 1" in summary
    assert "[messaging] publisher.py:2" in summary


def test_render_scan_lists_hits_and_empty_concerns(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scan = scan_distributed_systems(tmp_path)

    body = render_scan(scan)
    assert "## sync_http (1 hits)" in body
    assert "client.py:3" in body
    assert "## messaging (1 hits)" in body
    assert "publisher.py:2" in body


def test_render_scan_empty_repo_lists_no_hits(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    scan = scan_distributed_systems(tmp_path)

    body = render_scan(scan)
    assert "concerns with no hits" in body
    assert "no distributed-systems concerns detected." in body


def _analysis_answer() -> LLMResponse:
    content = (
        '{"summary": "The client retries aggressively but lacks backoff and '
        'idempotency.", '
        '"findings": [{"severity": "high", "file": "client.py", "line": 1, '
        '"problem": "fixed retry budget without exponential backoff", '
        '"reason": "thundering herd on outages", '
        '"fix": "use exponential backoff with jitter"}], '
        '"recommendations": ["adopt an idempotency key on POSTs", '
        '"wrap downstream calls in a circuit breaker"]}'
    )
    return LLMResponse(content=content, model="fake")


async def test_cmd_analyze_renders_scan_and_report(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([_analysis_answer()])

    code = await cmd_analyze(ctx, repo=tmp_path, state=None, llm=llm)

    assert code == 0
    out = buffer.getvalue()
    assert "distributed-systems scan of" in out
    assert "## sync_http" in out
    assert "aggressively but lacks backoff" in out
    assert "## Findings" in out
    assert "HIGH client.py:1" in out
    assert "## Recommendations" in out
    assert llm.calls[0]["system"] == ANALYSIS_PROMPT
    assert "sync_http: 1" in llm.calls[0]["messages"][0].content


async def test_cmd_analyze_scan_only_skips_llm(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([])

    code = await cmd_analyze(ctx, repo=tmp_path, state=None, scan_only=True, llm=llm)

    assert code == 0
    assert llm.calls == []
    out = buffer.getvalue()
    assert "## sync_http" in out
    assert "## Findings" not in out


async def test_cmd_analyze_prose_reply_degrades_gracefully(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    console, buffer = _console()
    ctx = CliContext(console=console, settings=_settings())
    llm = FakeLLM([LLMResponse(content="No major distributed-systems risks found.", model="fake")])

    code = await cmd_analyze(ctx, repo=tmp_path, state=None, llm=llm)

    assert code == 0
    assert "No major distributed-systems risks found." in buffer.getvalue()


def test_parse_analysis_report_from_fenced_json() -> None:
    text = (
        "```json\n"
        '{"summary": "s", "findings": [{"severity": "high", "file": "a.py", '
        '"line": 2, "problem": "p", "fix": "f"}], '
        '"recommendations": ["r1", "r2"]}\n'
        "```"
    )
    report = parse_analysis_report(text)
    assert report.summary == "s"
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.severity.value == "high"
    assert finding.file == "a.py"
    assert finding.line == 2
    assert finding.fix == "f"
    assert report.recommendations == ["r1", "r2"]


def test_parse_analysis_report_from_bare_json() -> None:
    text = '{"summary": "ok", "findings": [{"severity": "low", "file": "b.py", "problem": "q"}]}'
    report = parse_analysis_report(text)
    assert report.summary == "ok"
    assert report.findings[0].file == "b.py"


def test_parse_analysis_report_skips_malformed_findings() -> None:
    text = (
        '{"summary": "s", "findings": [{"severity": "high", "file": "a.py", '
        '"problem": "good"}, "junk", 5, {"file": "no-severity"}], '
        '"recommendations": [1, "keep", null]}'
    )
    report = parse_analysis_report(text)
    assert len(report.findings) == 1
    assert report.findings[0].problem == "good"
    assert report.recommendations == ["keep"]


def test_parse_analysis_report_prose_fallback_recovers_findings() -> None:
    text = 'Several risks.\n[{"severity": "medium", "file": "c.py", "problem": "no timeout"}]'
    report = parse_analysis_report(text)
    assert report.summary == "Several risks."
    assert len(report.findings) == 1
    assert report.findings[0].problem == "no timeout"


def test_render_analysis_includes_findings_and_recommendations() -> None:
    report = parse_analysis_report(_analysis_answer().content)
    body = render_analysis(report)
    assert "## Findings" in body
    assert "HIGH client.py:1" in body
    assert "## Recommendations" in body
    assert "adopt an idempotency key" in body


def test_render_analysis_omits_empty_sections() -> None:
    report = parse_analysis_report('{"summary": "fine"}')
    body = render_analysis(report)
    assert body == "fine"
    assert "## Findings" not in body
    assert "## Recommendations" not in body


def test_build_analysis_seed_embeds_scan_evidence(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    scan = scan_distributed_systems(tmp_path)
    seed = build_analysis_seed(scan_summary(scan))
    assert seed.startswith("Analyze the distributed-systems posture")
    assert "scanned 7 source files" in seed
    assert "[sync_http] client.py:3" in seed


def test_analysis_prompt_mentions_the_json_contract() -> None:
    assert "findings" in ANALYSIS_PROMPT
    assert "recommendations" in ANALYSIS_PROMPT
    assert "severity" in ANALYSIS_PROMPT


def test_parser_analyze_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["analyze", "--scan-only"])
    assert args.command == "analyze"
    assert args.scan_only is True


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


async def test_cmd_analyze_llm_failure_is_friendly(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="the model request failed"):
        await cmd_analyze(ctx, repo=tmp_path, state=None, llm=_RaisingLLM())


async def test_cmd_analyze_unconfigured_llm_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_settings: Settings) -> LLMResponse:
        raise RuntimeError("no provider")

    monkeypatch.setattr("app.cli.commands.build_llm_client", _boom)
    _scaffold(tmp_path)
    console, _ = _console()
    ctx = CliContext(console=console, settings=_settings())

    with pytest.raises(CliError, match="LLM is not configured"):
        await cmd_analyze(ctx, repo=tmp_path, state=None)
