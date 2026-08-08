"""Structured logging built on structlog.

Logs are emitted as JSON lines in deployments and as readable key-value
output during local development. Contextual information can be attached to
the current logical unit of work with ``structlog.contextvars``.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    """Configure structlog and the stdlib logging hierarchy.

    Args:
        level: Root logging level (e.g. ``INFO``, ``DEBUG``).
        json_logs: Emit JSON lines when true, readable console output otherwise.
    """
    logging.basicConfig(level=level, format="%(message)s")
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*_PROCESSORS, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """Return a bound logger for ``name``.

    Args:
        name: Logger name, conventionally the module path.

    Returns:
        A bound structlog logger.
    """
    return structlog.get_logger(name)
