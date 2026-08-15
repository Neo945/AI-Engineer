"""OpenTelemetry bootstrap: traces and metrics with no-op fallback.

Call :func:`init_telemetry` once at process start (gateway lifespan or CLI
entry) with the resolved settings. When ``settings.otel_enabled`` is false —
the default — nothing is configured and every OpenTelemetry API call resolves
to a no-op provider, so the instrumentation sprinkled through the codebase is
always safe and costs nothing.

When enabled, spans and metrics are exported over OTLP/HTTP to
``settings.otel_exporter_endpoint``. The exporter objects are injectable for
tests (in-memory exporters avoid touching the network).
"""

from __future__ import annotations

from typing import cast

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from app.core.config import Settings

__all__ = ["init_telemetry", "shutdown_telemetry"]

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def _build_resource(settings: Settings) -> Resource:
    """Build the resource describing this service instance."""
    return Resource(
        attributes={
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.app_env,
        }
    )


def init_telemetry(
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    span_processor: SpanProcessor | None = None,
    metric_reader: MetricReader | None = None,
) -> bool:
    """Configure global OpenTelemetry providers from ``settings``.

    Args:
        settings: Resolved application settings.
        span_exporter: Optional span exporter; when given (and no
            ``span_processor``) spans are exported synchronously, which is
            what tests use with an in-memory exporter. Defaults to an OTLP
            exporter when traces are enabled.
        span_processor: Optional span processor overriding the default choice
            of a synchronous processor for injected exporters and a batched
            one for OTLP.
        metric_reader: Optional metric reader; defaults to a periodic OTLP
            reader when metrics are enabled.

    Returns:
        True when telemetry was configured (``settings.otel_enabled``), False
        otherwise. Note the OpenTelemetry API only allows registering a global
        provider once per process, so this should be called at most once.
    """
    global _tracer_provider, _meter_provider
    if not settings.otel_enabled:
        return False

    resource = _build_resource(settings)

    if settings.otel_traces_enabled:
        if span_processor is not None:
            processor = span_processor
        elif span_exporter is not None:
            processor = SimpleSpanProcessor(span_exporter)
        else:
            processor = BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint)
            )
        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(processor)
        trace.set_tracer_provider(_tracer_provider)

    if settings.otel_metrics_enabled:
        if metric_reader is None:
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otel_exporter_endpoint)
            )
        _meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(_meter_provider)
    return True


def shutdown_telemetry() -> None:
    """Flush and stop all exporters, then reset to no-op providers."""
    global _tracer_provider, _meter_provider
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
        trace.set_tracer_provider(cast("trace.TracerProvider", None))
    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None
        metrics.set_meter_provider(cast("metrics.MeterProvider", None))
