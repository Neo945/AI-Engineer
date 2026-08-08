"""Path confinement for host-side workspace tools.

Every filesystem tool resolves its paths against a workspace root before any
host I/O happens. Resolution goes through ``os.path.realpath``, so symlinks
planted inside the workspace that point outside it are rejected rather than
followed — a sandbox process writing ``.evil -> /etc`` cannot trick a read
tool into leaking host files.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathTraversalError(ValueError):
    """Raised when a requested path escapes the workspace root."""


def resolve_within(root: Path | str, *parts: str | os.PathLike[str]) -> Path:
    """Resolve ``parts`` relative to ``root`` and enforce containment.

    Args:
        root: The workspace root every path must stay inside.
        parts: Path segments, joined to ``root``.

    Returns:
        The real (symlink-resolved) absolute path.

    Raises:
        PathTraversalError: If the resolved path is not inside ``root``.
    """
    root_real = os.path.realpath(root)
    request = "/".join(os.fspath(part) for part in parts) or "."
    joined = os.path.join(root_real, *(os.fspath(part) for part in parts))
    candidate = os.path.realpath(joined)

    if root_real == os.sep:
        return Path(candidate)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise PathTraversalError(f"path {request!r} escapes workspace root {root_real!r}")
    return Path(candidate)
