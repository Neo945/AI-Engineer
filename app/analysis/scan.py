"""Deterministic distributed-systems concern scanner.

Walks a workspace's source files (reusing the retrieval discovery) and
flags lines matching curated patterns for distributed-systems concerns:
sync/async HTTP, retries, idempotency, concurrency, locking, caching,
timeouts, circuit breakers, and messaging. The result is evidence the LLM
can interpret (`engineer analyze`) or a standalone report (`--scan-only`).

The scan is deliberately conservative: patterns are chosen to have few
false positives (specific APIs, decorators, and key words), and every hit
carries the source file, line, and the matched line as evidence so nothing
is claimed without a citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.retrieval.discovery import discover_source_files

#: Max evidence lines kept per concern, to bound output on large repos.
_MAX_HITS_PER_CONCERN = 40

#: Max evidence characters kept per hit.
_MAX_EVIDENCE = 120


@dataclass(frozen=True, order=True)
class ScanHit:
    """One matched line.

    Attributes:
        file: Root-relative path of the file.
        line: 1-based line number of the match.
        evidence: The matched line, trimmed.
    """

    file: str
    line: int
    evidence: str


@dataclass(frozen=True)
class Concern:
    """A scanned distributed-systems concern.

    Attributes:
        name: Short identifier (e.g. ``retries``).
        description: What the scan looks for.
        pattern: Regex matched against each source line (case-insensitive).
    """

    name: str
    description: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class ConcernScan:
    """The scan result for a single concern.

    Attributes:
        concern: The concern's short name.
        description: What the concern covers.
        total: Total matched lines across the workspace (before capping).
        hits: Up to :data:`_MAX_HITS_PER_CONCERN` evidence hits, sorted.
    """

    concern: str
    description: str
    total: int
    hits: tuple[ScanHit, ...]


@dataclass(frozen=True)
class DistributedScan:
    """The full workspace scan.

    Attributes:
        root: The scanned directory.
        files_scanned: Source files whose contents were read.
        concerns: Scans for every concern, including empty ones.
    """

    root: str
    files_scanned: int
    concerns: tuple[ConcernScan, ...]

    def hits(self, concern: str) -> tuple[ScanHit, ...]:
        """The hits for ``concern``, or ``()`` when it has none."""
        for scan in self.concerns:
            if scan.concern == concern:
                return scan.hits
        return ()


_CONCERNS: tuple[Concern, ...] = (
    Concern(
        "sync_http",
        "blocking HTTP calls (requests/urllib/http.client) on the request path",
        re.compile(
            r"\b(?:requests|urllib(?:\.request)?|http\.client)\s*\.\s*(?:get|post|put|delete|patch|request)\b"
        ),
    ),
    Concern(
        "async_http",
        "async HTTP calls (await client/session.get/post/...) — good, map them for timeouts",
        re.compile(
            r"\bawait\s+\w*?(?:client|session|http|connector|api|svc|service|sdk|gateway)"
            r"\w*\.(?:get|post|put|delete|patch|request)\s*\(",
            re.IGNORECASE,
        ),
    ),
    Concern(
        "retries",
        "retry/backoff logic (tenacity, @retry, max_retries, attempts)",
        re.compile(
            r"(?:@retry|\bbackoff\b|\btenacity\b|\bmax_retries\b|\bmax_attempts\b|\battempts\s*[=:])"
        ),
    ),
    Concern(
        "idempotency",
        "idempotency handling (idempotency keys, client request ids)",
        re.compile(
            r"\bidempoten[ct]y\b|\bidempotency[_-]?key\b|\bclient[_-]request[_-]id\b",
            re.IGNORECASE,
        ),
    ),
    Concern(
        "concurrency",
        "concurrent execution surfaces (gather/create_task/threads/pools)",
        re.compile(
            r"\basyncio\.(?:gather|create_task|TaskGroup)\b|\bthreading\.Thread\b|\b(?:Thread|Process)PoolExecutor\b"
        ),
    ),
    Concern(
        "locking",
        "locking/semaphore primitives (locks, mutexes, semaphores, acquire)",
        re.compile(
            r"\basyncio\.Lock\b|\bRLock\b|\bthreading\.Lock\b|\b(?:asyncio\.)?Semaphore\b|\b\.acquire\s*\("
        ),
    ),
    Concern(
        "caching",
        "caching (redis/memcached/cachetools, TTL, lru_cache)",
        re.compile(
            r"\b(?:redis|memcached|cachetools)\b|\bfunctools\.lru_cache\b|"
            r"\b(?:cache|ttl)\s*[=:]",
            re.IGNORECASE,
        ),
    ),
    Concern(
        "timeouts",
        "timeout policy (timeout=, asyncio.timeout, wait_for)",
        re.compile(r"\btimeout\s*[=:]|\basyncio\.(?:timeout|wait_for)\b|\bwith_timeout\b"),
    ),
    Concern(
        "circuit_breaker",
        "circuit breakers and resilience fallbacks (breaker, fallback)",
        re.compile(
            r"\bcircuit[_ ]?break\b|\bbreaker\b|\bresilience4j\b|\bhystrix\b|\bfallback\b",
            re.IGNORECASE,
        ),
    ),
    Concern(
        "messaging",
        "message/event plumbing (publish/emit/subscribe/consume, brokers, queues)",
        re.compile(
            r"\.(?:publish|emit|subscribe|consume|enqueue)\s*\(|"
            r"\b(?:kafka|rabbitmq|pulsar|nats|broker|task_queue)\b",
            re.IGNORECASE,
        ),
    ),
)


def scan_distributed_systems(root: str | Path) -> DistributedScan:
    """Scan the workspace at ``root`` for distributed-systems concerns.

    Args:
        root: The workspace checkout to scan.
    """
    root_path = Path(root).resolve()
    files = discover_source_files(root_path)
    matches: dict[str, list[ScanHit]] = {concern.name: [] for concern in _CONCERNS}
    totals: dict[str, int] = {concern.name: 0 for concern in _CONCERNS}
    files_scanned = 0
    for path in files:
        rel = path.relative_to(root_path).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files_scanned += 1
        for line_index, line in enumerate(source.splitlines()):
            for concern in _CONCERNS:
                if concern.pattern.search(line):
                    totals[concern.name] += 1
                    if len(matches[concern.name]) < _MAX_HITS_PER_CONCERN:
                        matches[concern.name].append(
                            ScanHit(
                                file=rel,
                                line=line_index + 1,
                                evidence=line.strip()[:_MAX_EVIDENCE],
                            )
                        )
    scans = tuple(
        ConcernScan(
            concern=concern.name,
            description=concern.description,
            total=totals[concern.name],
            hits=tuple(sorted(matches[concern.name])),
        )
        for concern in _CONCERNS
    )
    return DistributedScan(
        root=str(root_path),
        files_scanned=files_scanned,
        concerns=scans,
    )


def scan_summary(scan: DistributedScan) -> str:
    """A compact text summary of the scan, used to seed the LLM analysis."""
    lines = [
        f"scanned {scan.files_scanned} source files in {scan.root}",
        "concerns and hit counts:",
    ]
    for concern_scan in scan.concerns:
        lines.append(f"  {concern_scan.concern}: {concern_scan.total}")
    lines.append("sample evidence (concern file:line — matched line):")
    for concern_scan in scan.concerns:
        if concern_scan.total == 0:
            continue
        for hit in concern_scan.hits[:10]:
            lines.append(f"  [{concern_scan.concern}] {hit.file}:{hit.line} — {hit.evidence}")
    return "\n".join(lines)


def render_scan(scan: DistributedScan) -> str:
    """Render the scan as a human-readable report for the CLI."""
    lines = [
        f"distributed-systems scan of {scan.root} ({scan.files_scanned} source files)",
        "",
    ]
    for concern_scan in scan.concerns:
        if concern_scan.total == 0:
            continue
        line = f"## {concern_scan.concern} ({concern_scan.total} hits) — {concern_scan.description}"
        lines.append(line)
        for hit in concern_scan.hits:
            lines.append(f"  {hit.file}:{hit.line}  {hit.evidence}")
        if concern_scan.total > len(concern_scan.hits):
            lines.append(f"  ... and {concern_scan.total - len(concern_scan.hits)} more")
        lines.append("")
    empty = [concern_scan.concern for concern_scan in scan.concerns if concern_scan.total == 0]
    if empty:
        lines.append("concerns with no hits: " + ", ".join(empty))
    if not any(concern_scan.total for concern_scan in scan.concerns):
        lines.append("no distributed-systems concerns detected.")
    return "\n".join(lines)
