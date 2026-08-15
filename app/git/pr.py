"""Structured PR description generation.

``generate_pr_description`` asks the LLM to turn a committed diff into a
pull-request description (title, summary, tests, risks, migration notes) and
falls back to a best-effort description built from the commit list when the
model does not reply with the structured format, so the CLI never crashes on
a malformed response.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider

__all__ = [
    "PRDescription",
    "generate_pr_description",
    "parse_pr_description",
    "render_pr",
]

_MAX_DIFF_CHARS = 60_000


class PRDescription(BaseModel):
    """A pull-request title and body generated from a diff."""

    title: str = Field(min_length=1)
    summary: str = ""
    tests: str = ""
    risks: list[str] = Field(default_factory=list)
    migration: str | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        value = value.strip().replace("\n", " ")
        return value[:200] if len(value) > 200 else value


def render_pr(description: PRDescription) -> str:
    """Render a PR description as a Markdown body."""
    sections = [description.summary.strip()] if description.summary.strip() else []
    if description.tests.strip():
        sections.append(f"## Testing\n\n{description.tests.strip()}")
    if description.risks:
        items = "\n".join(f"- {risk.strip()}" for risk in description.risks if risk.strip())
        sections.append(f"## Risks\n\n{items}")
    if description.migration and description.migration.strip():
        sections.append(f"## Migration notes\n\n{description.migration.strip()}")
    return "\n\n".join(sections)


def parse_pr_description(text: str) -> PRDescription:
    """Parse a PR description from ``text``.

    Tries a fenced or bare JSON object first; falls back to treating the
    first non-empty line as the title and the rest as the summary.
    """
    payload = _extract_json_object(text)
    if payload is not None:
        try:
            return PRDescription.model_validate(payload)
        except Exception:
            pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0] if lines else "changes"
    return PRDescription(title=title, summary="\n".join(lines[1:]))


async def generate_pr_description(
    llm: LLMProvider,
    *,
    diff: str,
    commits: list[str],
    base: str,
    branch: str,
) -> PRDescription:
    """Generate a structured PR description for ``diff`` using ``llm``.

    Falls back to a commit-derived description when the model output cannot
    be parsed into :class:`PRDescription`.
    """
    prompt = _build_prompt(diff=diff, commits=commits, base=base, branch=branch)
    response = await llm.complete(
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        tools=[],
        max_tokens=2048,
        temperature=0.0,
    )
    return parse_pr_description(response.content)


def _build_prompt(*, diff: str, commits: list[str], base: str, branch: str) -> str:
    commit_lines = "\n".join(commits) if commits else "(no commits listed)"
    truncated = diff if len(diff) <= _MAX_DIFF_CHARS else diff[:_MAX_DIFF_CHARS] + "\n…(truncated)"
    return (
        f"You are preparing a pull request from branch '{branch}' into '{base}'.\n\n"
        "Commits in this PR:\n"
        f"{commit_lines}\n\n"
        "Full diff:\n"
        f"{truncated}\n\n"
        "Write a pull-request description as a single JSON object with exactly "
        "these fields:\n"
        '- "title": a concise conventional-commit style subject line.\n'
        '- "summary": 2-4 sentences on what changed and why.\n'
        '- "tests": how the change was verified, or "Not run".\n'
        '- "risks": an array of short risk strings (may be empty).\n'
        '- "migration": migration or breaking-change notes, or null.\n\n'
        "Reply with only the JSON object."
    )


def _extract_json_object(text: str) -> dict[str, object] | None:
    """Return a JSON object embedded in ``text``, or None."""
    import json
    import re

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
