"""Unit tests for LLM-drafted commit messages and PR descriptions."""

from __future__ import annotations

from tests.unit.fake_llm import FakeLLM

from app.git.commit import generate_commit_message
from app.git.pr import PRDescription, generate_pr_description, parse_pr_description, render_pr
from app.llm.protocol import LLMResponse

DIFF = (
    "diff --git a/app.py b/app.py\nindex 1111111..2222222 100644\n--- a/app.py\n"
    "+++ b/app.py\n@@ -1 +1 @@\n-print('hi')\n+print('hello')\n"
)


async def test_generate_commit_message_returns_model_content() -> None:
    llm = FakeLLM([LLMResponse(content="feat: greet the user", model="fake-model")])
    message = await generate_commit_message(llm, diff=DIFF)
    assert message == "feat: greet the user"
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["max_tokens"] == 512
    assert call["temperature"] == 0.0
    assert call["tools"] == []
    assert "DIFF" in call["messages"][0].content


async def test_generate_commit_message_trims_whitespace() -> None:
    llm = FakeLLM([LLMResponse(content="  fix: trim me\n\n", model="fake-model")])
    message = await generate_commit_message(llm, diff=DIFF)
    assert message == "fix: trim me"


async def test_parse_pr_description_from_fenced_json() -> None:
    text = (
        "```json\n"
        '{"title": "feat: add telemetry", "summary": "Adds spans.", '
        '"tests": "pytest green", "risks": ["new deps"], "migration": null}\n'
        "```"
    )
    description = parse_pr_description(text)
    assert description.title == "feat: add telemetry"
    assert description.summary == "Adds spans."
    assert description.tests == "pytest green"
    assert description.risks == ["new deps"]
    assert description.migration is None


async def test_parse_pr_description_from_bare_json() -> None:
    payload = '{"title": "fix: crash", "summary": "n/a", "tests": "Not run"}'
    description = parse_pr_description(payload)
    assert description.title == "fix: crash"
    assert description.tests == "Not run"


async def test_parse_pr_description_falls_back_to_first_line() -> None:
    description = parse_pr_description("fix the bug\n\nwe fixed the bug\n- detail one\n")
    assert description.title == "fix the bug"
    assert "we fixed the bug" in description.summary
    assert "detail one" in description.summary


async def test_title_is_normalized() -> None:
    description = parse_pr_description('{"title": "  long\\ntitle  ", "summary": "s"}')
    assert description.title == "long title"


async def test_render_pr_includes_populated_sections_only() -> None:
    description = PRDescription(
        title="feat: x",
        summary="Do the thing.",
        tests="unit tests pass",
        risks=["risk a"],
        migration="run migrate",
    )
    body = render_pr(description)
    assert "Do the thing." in body
    assert "## Testing" in body
    assert "## Risks" in body
    assert "- risk a" in body
    assert "## Migration notes" in body

    sparse = render_pr(PRDescription(title="feat: x", summary="  "))
    assert sparse == ""


async def test_generate_pr_description_parses_model_json() -> None:
    llm = FakeLLM(
        [
            LLMResponse(
                content=(
                    '{"title": "feat: add telemetry", "summary": "Adds OTel spans.", '
                    '"tests": "pytest green", "risks": [], "migration": null}'
                ),
                model="fake-model",
            )
        ]
    )
    description = await generate_pr_description(
        llm, diff=DIFF, commits=["abc123 feat: add telemetry"], base="main", branch="feature/x"
    )
    assert description.title == "feat: add telemetry"
    assert description.tests == "pytest green"
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_tokens"] == 2048
    prompt = llm.calls[0]["messages"][0].content
    assert "feature/x" in prompt
    assert "main" in prompt


async def test_generate_pr_description_falls_back_on_non_json() -> None:
    llm = FakeLLM([LLMResponse(content="A plain title\n\nSome prose.", model="fake-model")])
    description = await generate_pr_description(
        llm, diff=DIFF, commits=["abc123 thing"], base="main", branch="feature/x"
    )
    assert description.title == "A plain title"
    assert "Some prose." in description.summary
