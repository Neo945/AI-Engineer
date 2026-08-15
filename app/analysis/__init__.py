"""Distributed-systems analysis for the coding agent.

``engineer analyze`` combines a deterministic concern scanner
(:mod:`app.analysis.scan`) with an LLM interpretation (:mod:`app.analysis.report`)
to assess a workspace's distributed-systems posture: sync/async HTTP,
retries, idempotency, concurrency, locking, caching, timeouts, circuit
breakers, and messaging.
"""

from app.analysis.report import (
    ANALYSIS_PROMPT,
    AnalysisReport,
    build_analysis_seed,
    parse_analysis_report,
    render_analysis,
)
from app.analysis.scan import (
    Concern,
    ConcernScan,
    DistributedScan,
    ScanHit,
    render_scan,
    scan_distributed_systems,
    scan_summary,
)

__all__ = [
    "ANALYSIS_PROMPT",
    "AnalysisReport",
    "Concern",
    "ConcernScan",
    "DistributedScan",
    "ScanHit",
    "build_analysis_seed",
    "parse_analysis_report",
    "render_analysis",
    "render_scan",
    "scan_distributed_systems",
    "scan_summary",
]
