"""Gateway: the HTTP/WebSocket entry point.

Owns the FastAPI application factory, request-scoped dependencies, and the
public API surface. Boundary concerns (auth, rate limiting, streaming) will
live here in later phases.
"""
