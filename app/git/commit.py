"""LLM-generated conventional commit messages."""

from __future__ import annotations

from app.llm.messages import ChatMessage, ChatRole
from app.llm.protocol import LLMProvider

__all__ = ["generate_commit_message"]

_MAX_DIFF_CHARS = 40_000


async def generate_commit_message(llm: LLMProvider, *, diff: str) -> str:
    """Return a conventional commit message (subject + body) for ``diff``."""
    truncated = diff if len(diff) <= _MAX_DIFF_CHARS else diff[:_MAX_DIFF_CHARS] + "\n…(truncated)"
    prompt = (
        "Write a conventional commit message for the following diff.\n"
        "Rules:\n"
        "- First line: a subject like 'fix: ...' or 'feat: ...' under 72 characters.\n"
        "- After a blank line, a short body explaining why, when it adds value.\n"
        "- Reply with only the commit message; no fences, no quotes.\n\n"
        f"DIFF:\n{truncated}"
    )
    response = await llm.complete(
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        tools=[],
        max_tokens=512,
        temperature=0.0,
    )
    return response.content.strip()
