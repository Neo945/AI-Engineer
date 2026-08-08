"""Core infrastructure: configuration, logging, and dependency wiring.

This package is the composition root of the application. It must never
import from feature packages (gateway, orchestrator, agents, ...) to keep
the dependency graph pointing inward.
"""

from app.core.config import Settings, get_settings
from app.core.container import Container
from app.core.logging import configure_logging, get_logger

__all__ = [
    "Container",
    "Settings",
    "configure_logging",
    "get_logger",
    "get_settings",
]
