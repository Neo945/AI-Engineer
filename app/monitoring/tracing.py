"""Tracing instrumentation: spans and metrics around LLM requests.

:class:`InstrumentedLLM` decorates any :class:`LLMProvider` so that every
``complete``/``stream`` call produces a span and the token/outcome metrics,
without the provider or its callers knowing anything about OpenTelemetry. It
is the composition-root seam the container and CLI use to observe LLM
traffic.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from app.llm.messages import ChatMessage
from app.llm.protocol import LLMProvider, LLMResponse, LLMStreamEvent
from app.monitoring import instruments as inst
from app.tools.schemas import ToolSpec

__all__ = ["InstrumentedLLM"]

_TRACER_NAME = "coding-agent"


class InstrumentedLLM(LLMProvider):
    """Wrap an :class:`LLMProvider` with spans and metric recording.

    Attributes:
        name: Provider identifier reported by the wrapper.
        model: The wrapped provider's model, captured at construction.
    """

    name = "instrumented"

    def __init__(self, provider: LLMProvider, *, tracer: trace.Tracer | None = None) -> None:
        self._provider = provider
        self.model = provider.model
        self._tracer = tracer or trace.get_tracer(_TRACER_NAME)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        started = time.perf_counter()
        attributes = {
            "llm.provider": self._provider.name,
            "llm.model": self.model,
            "llm.kind": "complete",
        }
        with self._tracer.start_as_current_span("llm.complete", attributes=attributes) as span:
            try:
                response = await self._provider.complete(
                    messages,
                    tools=tools,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                inst.record_llm_call(
                    kind="complete",
                    provider=self._provider.name,
                    model=self.model,
                    outcome="error",
                    duration_seconds=time.perf_counter() - started,
                    input_tokens=0,
                    output_tokens=0,
                )
                raise
        inst.record_llm_call(
            kind="complete",
            provider=self._provider.name,
            model=self.model,
            outcome="success",
            duration_seconds=time.perf_counter() - started,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec],
        system: str | None = None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[LLMStreamEvent]:
        started = time.perf_counter()
        attributes = {
            "llm.provider": self._provider.name,
            "llm.model": self.model,
            "llm.kind": "stream",
        }
        span = self._tracer.start_span("llm.stream", attributes=attributes)
        input_tokens = 0
        output_tokens = 0
        served_model = self.model
        with trace.use_span(span, end_on_exit=False):
            try:
                async for event in self._provider.stream(
                    messages,
                    tools=tools,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ):
                    if event.type == "usage" and event.usage is not None:
                        input_tokens = event.usage.input_tokens
                        output_tokens = event.usage.output_tokens
                        served_model = event.model or served_model
                    yield event
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                inst.record_llm_call(
                    kind="stream",
                    provider=self._provider.name,
                    model=served_model,
                    outcome="error",
                    duration_seconds=time.perf_counter() - started,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                raise
            finally:
                span.set_attribute("llm.tokens.input", input_tokens)
                span.set_attribute("llm.tokens.output", output_tokens)
                span.end()
        inst.record_llm_call(
            kind="stream",
            provider=self._provider.name,
            model=served_model,
            outcome="success",
            duration_seconds=time.perf_counter() - started,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def close(self) -> None:
        """Release the wrapped provider's resources."""
        await self._provider.close()
