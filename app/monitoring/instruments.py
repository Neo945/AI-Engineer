"""Metric instruments and recording helpers.

Every recorder funnels through a module-level :class:`Instruments` instance.
By default it is bound to OpenTelemetry's no-op global meter, so recording is
free unless :func:`app.monitoring.telemetry.init_telemetry` configured a real
meter. Tests can swap the instance with :func:`configure_instruments` to point
at an in-memory meter.
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram, Meter

__all__ = [
    "Instruments",
    "configure_instruments",
    "get_instruments",
    "record_http_request",
    "record_llm_call",
    "record_task_run",
    "record_tool_call",
]

_METER_NAME = "coding-agent"


class Instruments:
    """The set of counters and histograms this application exports."""

    def __init__(self, meter: Meter) -> None:
        self.llm_calls: Counter = meter.create_counter(
            "llm.calls",
            unit="{calls}",
            description="LLM requests made, by kind and outcome.",
        )
        self.llm_tokens: Counter = meter.create_counter(
            "llm.tokens",
            unit="{tokens}",
            description="Tokens exchanged with the LLM, by direction.",
        )
        self.llm_duration: Histogram = meter.create_histogram(
            "llm.duration",
            unit="s",
            description="Duration of LLM requests.",
        )
        self.tool_calls: Counter = meter.create_counter(
            "tool.calls",
            unit="{calls}",
            description="Tool executions, by tool and outcome.",
        )
        self.task_runs: Counter = meter.create_counter(
            "task.runs",
            unit="{runs}",
            description="Task attempts, by agent type and final status.",
        )
        self.task_duration: Histogram = meter.create_histogram(
            "task.duration",
            unit="s",
            description="Duration of task runs from start to terminal state.",
        )
        self.requests: Counter = meter.create_counter(
            "http.server.requests",
            unit="{requests}",
            description="HTTP requests handled by the gateway.",
        )
        self.request_duration: Histogram = meter.create_histogram(
            "http.request.duration",
            unit="s",
            description="Duration of HTTP requests handled by the gateway.",
        )


_DEFAULT: Instruments | None = None


def get_instruments() -> Instruments:
    """Return the module-level instruments instance."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Instruments(metrics.get_meter(_METER_NAME))
    return _DEFAULT


def configure_instruments(instruments: Instruments | None) -> None:
    """Swap the module-level instruments instance (used by tests)."""
    global _DEFAULT
    _DEFAULT = instruments


def record_llm_call(
    *,
    kind: str,
    provider: str,
    model: str,
    outcome: str,
    duration_seconds: float,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record one LLM request: a call counter, a duration, and its tokens."""
    instruments = get_instruments()
    attributes = {
        "kind": kind,
        "provider": provider,
        "model": model,
        "outcome": outcome,
    }
    instruments.llm_calls.add(1, attributes)
    instruments.llm_duration.record(duration_seconds, attributes)
    if input_tokens:
        instruments.llm_tokens.add(
            input_tokens,
            {"provider": provider, "model": model, "direction": "input"},
        )
    if output_tokens:
        instruments.llm_tokens.add(
            output_tokens,
            {"provider": provider, "model": model, "direction": "output"},
        )


def record_tool_call(*, tool: str, outcome: str) -> None:
    """Record one tool execution."""
    instruments = get_instruments()
    instruments.tool_calls.add(1, {"tool": tool, "outcome": outcome})


def record_task_run(
    *,
    agent_type: str,
    status: str,
    duration_seconds: float,
    attempts: int,
) -> None:
    """Record one task attempt reaching a terminal state."""
    instruments = get_instruments()
    attributes: dict[str, str | int] = {
        "agent_type": agent_type,
        "status": status,
        "attempts": attempts,
    }
    instruments.task_runs.add(1, attributes)
    instruments.task_duration.record(duration_seconds, attributes)


def record_http_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    """Record one HTTP request handled by the gateway."""
    instruments = get_instruments()
    attributes: dict[str, str | int] = {
        "http.request.method": method,
        "http.route": route,
        "http.response.status_code": status_code,
    }
    instruments.requests.add(1, attributes)
    instruments.request_duration.record(duration_seconds, attributes)
