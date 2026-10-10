"""Intent-based agent selection.

Historically a task declared its ``agent_type`` explicitly (``coder``,
``pipeline``, or ``coordinator``). This module adds an ``auto`` mode: given a
natural-language goal it picks the most appropriate agent type using cheap,
deterministic keyword heuristics, so the orchestrator and CLI can route a goal
without the caller having to know the agent taxonomy.

The classifier is deliberately transparent and offline: it requires no LLM
call, always returns an answer, and exposes the matched signals in the
:class:`Intent` it returns. It is the deterministic counterpart to the
coordinator's LLM dispatch (which decides *specialist* selection); the
coordinator uses the hints here only as a fallback when its dispatch reply
cannot be parsed.

Rules (first match wins):

1. If the goal mentions any specialist domain (security, performance,
   testing), route to ``coordinator`` — it runs those read-only specialists
   and can also apply changes.
2. Else if the goal asks for changes (action verbs) and looks multi-step
   (connectors such as "and"/"then", or several action verbs), route to
   ``pipeline`` (planner → coder → reviewer → tester).
3. Otherwise route to ``coder`` (the sensible default single-agent loop).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "AGENT_TYPES",
    "Intent",
    "classify_intent",
    "resolve_agent_type",
]

#: The agent types the orchestrator can run directly. ``auto`` is resolved to
#: one of these by :func:`resolve_agent_type`.
AGENT_TYPES: tuple[str, ...] = ("coder", "pipeline", "coordinator")

#: Keyword sets that indicate a read-only specialist is worth running. Keys
#: mirror the coordinator's specialist registry; the values are lowercase
#: substrings matched against the goal.
_SPECIALIST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "security": (
        "security",
        "vulnerab",
        "exploit",
        "injection",
        "xss",
        "csrf",
        "sanitiz",
        "secret",
        "credential leak",
        "cve",
        "auth bypass",
    ),
    "performance": (
        "performance",
        "latency",
        "slow",
        "scalab",
        "throughput",
        "n+1",
        "bottleneck",
        "memory leak",
        "optimi",
        "profil",
    ),
    "testing": (
        "test",
        "coverage",
        "pytest",
        "unit test",
        "integration test",
        "regression",
        "flaky",
        "assert",
    ),
}

#: Registry order — used so specialist lists are deterministic.
_SPECIALIST_ORDER: tuple[str, ...] = ("security", "performance", "testing")

#: Verbs/nouns that indicate the goal expects the repository to be modified.
_ACTION_KEYWORDS: tuple[str, ...] = (
    "fix",
    "add",
    "implement",
    "refactor",
    "rename",
    "remove",
    "delete",
    "create",
    "write",
    "update",
    "change",
    "migrate",
    "upgrade",
    "downgrade",
    "build",
    "bug",
    "patch",
)

#: Connectors/signals that a goal spans multiple steps.
_COMPLEXITY_KEYWORDS: tuple[str, ...] = (
    " and ",
    " then ",
    " also ",
    " as well as ",
    " plus ",
    "across",
    "end-to-end",
    "e2e",
    "multiple",
    "whole",
    "entire",
)


@dataclass(frozen=True)
class Intent:
    """The outcome of classifying a goal.

    Attributes:
        agent_type: Recommended agent type (one of :data:`AGENT_TYPES`).
        specialists: Specialist names whose keywords matched, in registry
            order (empty when none matched).
        needs_changes: Whether the goal is expected to modify the repository.
        reason: Human-readable justification for the decision.
    """

    agent_type: str
    specialists: tuple[str, ...]
    needs_changes: bool
    reason: str


def classify_intent(goal: str) -> Intent:
    """Classify ``goal`` into an :class:`Intent` using deterministic heuristics.

    The classifier never raises and always returns a valid agent type, so it
    is safe to call on any user input.
    """
    text = goal.lower()

    specialists = tuple(
        name
        for name in _SPECIALIST_ORDER
        if any(_matches(text, keyword) for keyword in _SPECIALIST_KEYWORDS[name])
    )
    needs_changes = any(_matches(text, keyword) for keyword in _ACTION_KEYWORDS)

    if specialists:
        return Intent(
            agent_type="coordinator",
            specialists=specialists,
            needs_changes=needs_changes,
            reason=f"goal matches specialists: {', '.join(specialists)}",
        )

    if needs_changes and _looks_multi_step(text):
        return Intent(
            agent_type="pipeline",
            specialists=(),
            needs_changes=True,
            reason="goal requires multi-step changes",
        )

    if needs_changes:
        return Intent(
            agent_type="coder",
            specialists=(),
            needs_changes=True,
            reason="goal requires a focused change",
        )

    return Intent(
        agent_type="coder",
        specialists=(),
        needs_changes=False,
        reason="no change or specialist signal; defaulting to the coder agent",
    )


def resolve_agent_type(agent_type: str, goal: str) -> str:
    """Resolve ``agent_type`` against ``goal``.

    ``"auto"`` (case-insensitive) is classified via :func:`classify_intent`;
    every other value is returned unchanged so explicit choices are honoured
    (and unknown values still reach the orchestrator's existing validation).
    """
    if agent_type.strip().lower() == "auto":
        return classify_intent(goal).agent_type
    return agent_type


def _looks_multi_step(text: str) -> bool:
    """Return whether ``text`` reads like a multi-step change request."""
    if any(connector in text for connector in _COMPLEXITY_KEYWORDS):
        return True
    action_hits = sum(1 for keyword in _ACTION_KEYWORDS if _matches(text, keyword))
    if action_hits >= 2:
        return True
    # Multiple sentences / clauses also suggest more than a one-line edit.
    return len(re.findall(r"[.;\n]|,\s", text)) >= 2


def _matches(text: str, keyword: str) -> bool:
    """Return whether ``keyword`` appears in ``text`` as a whole word/prefix.

    Keywords that start with a word character are anchored to a word boundary
    so short stems (e.g. ``"test"``) do not match inside unrelated words
    (e.g. ``"latest"``); they still match longer forms (``"tests"``,
    ``"testing"``). Connectors with leading spaces are matched literally.
    """
    if keyword[:1].isalnum():
        return re.search(r"\b" + re.escape(keyword), text) is not None
    return keyword in text
