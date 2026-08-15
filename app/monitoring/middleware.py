"""ASGI middleware adding a server span and request metrics per request.

The middleware is safe to install unconditionally: when telemetry is disabled
the tracer and meter are no-ops, so the overhead is one dict look-up per
request.
"""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.monitoring import instruments as inst

__all__ = ["ObservabilityMiddleware"]

_TRACER_NAME = "coding-agent"


class ObservabilityMiddleware:
    """Create a server span and request metrics for every HTTP request."""

    def __init__(self, app: ASGIApp, *, tracer: Tracer | None = None) -> None:
        self._app = app
        self._tracer = tracer or trace.get_tracer(_TRACER_NAME)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "")
        route = self._route_path(scope)
        started = time.perf_counter()
        status_code = 500

        async def _send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        span = self._tracer.start_span(
            "http.request",
            attributes={"http.request.method": method, "http.route": route},
        )
        try:
            await self._app(scope, receive, _send)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            duration = time.perf_counter() - started
            span.set_attribute("http.response.status_code", status_code)
            span.end()
            inst.record_http_request(
                method=method,
                route=route,
                status_code=status_code,
                duration_seconds=duration,
            )

    @staticmethod
    def _route_path(scope: Scope) -> str:
        """Return the matched route template if available, else the raw path."""
        route: Any = scope.get("route")
        path = getattr(route, "path", None)
        if path is not None:
            return str(path)
        return scope.get("path", "")
