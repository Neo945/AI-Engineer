"""Unit tests for the observability layer.

Component tests (``InstrumentedLLM``, middleware, metric recorders) inject a
``Tracer``/``Meter`` built on in-memory exporters directly, so nothing touches
the network and no global provider is registered. ``init_telemetry`` is
exercised exactly once, at the end of this module, because the OpenTelemetry
API only permits registering a global provider once per process.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from tests.unit.fake_llm import FakeLLM

from app.core.config import Settings
from app.llm.protocol import LLMResponse, LLMUsage
from app.monitoring import instruments as inst
from app.monitoring.instruments import Instruments
from app.monitoring.middleware import ObservabilityMiddleware
from app.monitoring.telemetry import init_telemetry, shutdown_telemetry
from app.monitoring.tracing import InstrumentedLLM

Telemetry = tuple[Tracer, InMemorySpanExporter, InMemoryMetricReader]


def _settings(*, otel_enabled: bool = False) -> Settings:
    return Settings(_env_file=None, otel_enabled=otel_enabled)


def _metric_points(
    reader: InMemoryMetricReader, name: str
) -> list[tuple[int | float, dict[str, Any]]]:
    """Return ``(value, attributes)`` pairs for every data point of ``name``."""
    points: list[tuple[int | float, dict[str, Any]]] = []
    data = reader.get_metrics_data()
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value: int | float = point.value if hasattr(point, "value") else point.sum
                    points.append((value, dict(point.attributes)))
    return points


def _metric_total(reader: InMemoryMetricReader, name: str) -> int | float:
    """Sum every data point of ``name`` across all attribute sets."""
    return sum(value for value, _ in _metric_points(reader, name))


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    """In-memory tracer/meter, registered as the module-level instruments."""
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    inst.configure_instruments(Instruments(meter_provider.get_meter("test")))
    try:
        yield (tracer_provider.get_tracer("test"), span_exporter, metric_reader)
    finally:
        inst.configure_instruments(None)
        meter_provider.shutdown()
        tracer_provider.shutdown()


@pytest.mark.asyncio
async def test_complete_records_span_and_metrics(telemetry: Telemetry) -> None:
    tracer, exporter, reader = telemetry
    inner = FakeLLM(
        [
            LLMResponse(
                content="hi", usage=LLMUsage(input_tokens=10, output_tokens=3), model="fake-model"
            )
        ]
    )
    wrapped = InstrumentedLLM(inner, tracer=tracer)

    response = await wrapped.complete([], tools=[], max_tokens=16, temperature=0.0)

    assert response.content == "hi"
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["llm.complete"]
    span = spans[0]
    assert span.attributes["llm.provider"] == "fake"
    assert span.attributes["llm.model"] == "fake-model"
    assert span.attributes["llm.kind"] == "complete"
    assert _metric_total(reader, "llm.calls") == 1
    assert _metric_total(reader, "llm.tokens") == 13
    calls = _metric_points(reader, "llm.calls")
    assert calls[0][1]["outcome"] == "success"


@pytest.mark.asyncio
async def test_stream_records_usage_and_span(telemetry: Telemetry) -> None:
    tracer, exporter, reader = telemetry
    inner = FakeLLM(
        [
            LLMResponse(
                content="streamed",
                usage=LLMUsage(input_tokens=4, output_tokens=2),
                model="fake-model",
            )
        ]
    )
    wrapped = InstrumentedLLM(inner, tracer=tracer)

    events = [event async for event in wrapped.stream([], tools=[], max_tokens=16, temperature=0.0)]

    assert any(event.type == "usage" for event in events)
    assert [span.name for span in exporter.get_finished_spans()] == ["llm.stream"]
    assert _metric_total(reader, "llm.calls") == 1
    assert _metric_total(reader, "llm.tokens") == 6


@pytest.mark.asyncio
async def test_error_records_error_outcome_and_status(telemetry: Telemetry) -> None:
    tracer, exporter, reader = telemetry

    class _Boom(FakeLLM):
        async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
            raise RuntimeError("boom")

    wrapped = InstrumentedLLM(_Boom(), tracer=tracer)

    with pytest.raises(RuntimeError):
        await wrapped.complete([], tools=[], max_tokens=16, temperature=0.0)

    spans = exporter.get_finished_spans()
    assert spans[0].status.status_code == StatusCode.ERROR
    calls = _metric_points(reader, "llm.calls")
    assert calls[0][1]["outcome"] == "error"


@pytest.mark.asyncio
async def test_tool_and_task_metrics_via_configured_instruments() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    inst.configure_instruments(Instruments(provider.get_meter("test")))
    try:
        inst.record_tool_call(tool="file_read", outcome="success")
        inst.record_tool_call(tool="file_edit", outcome="error")
        inst.record_task_run(
            agent_type="coder", status="completed", duration_seconds=2.5, attempts=1
        )

        assert _metric_total(reader, "tool.calls") == 2
        assert _metric_total(reader, "task.runs") == 1
        tools = _metric_points(reader, "tool.calls")
        assert tools[1][1]["outcome"] == "error"
    finally:
        inst.configure_instruments(None)
        provider.shutdown()


@pytest.mark.asyncio
async def test_middleware_records_request(telemetry: Telemetry) -> None:
    tracer, exporter, reader = telemetry

    async def app(scope: object, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: object) -> None:
        pass

    middleware = ObservabilityMiddleware(app, tracer=tracer)
    scope = {"type": "http", "method": "GET", "path": "/health", "route": None}

    await middleware(scope, receive, send)

    assert [span.name for span in exporter.get_finished_spans()] == ["http.request"]
    assert _metric_total(reader, "http.server.requests") == 1
    request = _metric_points(reader, "http.server.requests")
    assert request[0][1]["http.response.status_code"] == 200
    assert request[0][1]["http.route"] == "/health"


@pytest.mark.asyncio
async def test_wrapper_and_middleware_work_without_telemetry() -> None:
    inner = FakeLLM([LLMResponse(content="ok", usage=LLMUsage(), model="fake-model")])
    wrapped = InstrumentedLLM(inner)

    response = await wrapped.complete([], tools=[], max_tokens=16, temperature=0.0)
    events = [event async for event in wrapped.stream([], tools=[], max_tokens=16, temperature=0.0)]

    assert response.content == "ok"
    assert any(event.type == "usage" for event in events)
    await wrapped.close()

    async def app(scope: object, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: object) -> None:
        pass

    await ObservabilityMiddleware(app)(
        {"type": "http", "method": "POST", "path": "/x", "route": None}, receive, send
    )


def test_init_telemetry_disabled_returns_false() -> None:
    assert (
        init_telemetry(
            _settings(otel_enabled=False),
            span_exporter=InMemorySpanExporter(),
            metric_reader=InMemoryMetricReader(),
        )
        is False
    )


def test_init_telemetry_enabled_registers_globals() -> None:
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    assert (
        init_telemetry(_settings(otel_enabled=True), span_exporter=exporter, metric_reader=reader)
        is True
    )

    span = trace.get_tracer("test").start_span("global")
    span.end()
    assert [span.name for span in exporter.get_finished_spans()] == ["global"]

    shutdown_telemetry()
    shutdown_telemetry()
