"""Legacy-modernization mode: ``engineer modernize`` plans a migration.

Seeded by the workspace's deterministic dependency graph, the LLM produces a
staged migration plan, a risk assessment, and a rollback strategy. The command
is plan-only: it never executes changes, so the plan can be reviewed and
approved before any work begins.
"""

from __future__ import annotations

from app.modernization.report import (
    MODERNIZATION_PROMPT,
    MigrationStep,
    ModernizationReport,
    build_modernization_seed,
    parse_modernization_report,
    render_modernization,
)

__all__ = [
    "MODERNIZATION_PROMPT",
    "MigrationStep",
    "ModernizationReport",
    "build_modernization_seed",
    "parse_modernization_report",
    "render_modernization",
]
