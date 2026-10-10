"""Unit tests for intent-based agent selection."""

from __future__ import annotations

import pytest

from app.agents.intent import AGENT_TYPES, classify_intent, resolve_agent_type


def test_classify_security_goal_routes_to_coordinator() -> None:
    intent = classify_intent("Audit the login flow for injection vulnerabilities")
    assert intent.agent_type == "coordinator"
    assert "security" in intent.specialists
    assert intent.needs_changes is False


def test_classify_performance_goal_routes_to_coordinator() -> None:
    intent = classify_intent("Why is the API so slow? Look for latency bottlenecks")
    assert intent.agent_type == "coordinator"
    assert "performance" in intent.specialists


def test_classify_testing_keywords_route_to_coordinator() -> None:
    intent = classify_intent("Review test coverage for the payment module")
    assert intent.agent_type == "coordinator"
    assert "testing" in intent.specialists


def test_classify_specialist_goal_with_changes_sets_needs_changes() -> None:
    intent = classify_intent("Fix the security flaw in the parser")
    assert intent.agent_type == "coordinator"
    assert "security" in intent.specialists
    assert intent.needs_changes is True


def test_classify_multi_step_change_routes_to_pipeline() -> None:
    intent = classify_intent("add a health endpoint and then update the docs")
    assert intent.agent_type == "pipeline"
    assert intent.needs_changes is True
    assert intent.specialists == ()


def test_classify_focused_change_routes_to_coder() -> None:
    intent = classify_intent("fix the off-by-one in paginate()")
    assert intent.agent_type == "coder"
    assert intent.needs_changes is True


def test_classify_no_signal_defaults_to_coder() -> None:
    intent = classify_intent("hello there")
    assert intent.agent_type == "coder"
    assert intent.needs_changes is False
    assert intent.specialists == ()


def test_classify_always_returns_valid_agent_type() -> None:
    for goal in ("", "   ", "x", "fix fix fix and also fix then fix"):
        assert classify_intent(goal).agent_type in AGENT_TYPES


def test_classify_specialists_are_registry_order() -> None:
    intent = classify_intent("security and performance and testing review")
    assert intent.specialists == ("security", "performance", "testing")


def test_classify_ignores_keyword_substrings_inside_words() -> None:
    # "latest" contains "test" but is not a testing signal.
    intent = classify_intent("Update to the latest version")
    assert intent.specialists == ()
    assert intent.agent_type == "coder"


@pytest.mark.parametrize("value", ["coder", "pipeline", "coordinator"])
def test_resolve_agent_type_passes_through_explicit(value: str) -> None:
    assert resolve_agent_type(value, "fix the bug") == value


def test_resolve_agent_type_handles_auto_case_insensitively() -> None:
    assert resolve_agent_type("AUTO", "Review security") == "coordinator"
    assert resolve_agent_type(" auto ", "fix a typo") == "coder"


def test_resolve_agent_type_leaves_unknown_untouched() -> None:
    # Unknown values must reach the orchestrator's validation unchanged.
    assert resolve_agent_type("debugger", "fix the bug") == "debugger"
