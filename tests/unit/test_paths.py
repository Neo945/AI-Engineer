"""Unit tests for workspace path confinement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.executor.paths import PathTraversalError, resolve_within


def test_resolves_nested_path(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    target = tmp_path / "a" / "b.txt"
    target.write_text("x")
    assert resolve_within(tmp_path, "a", "b.txt") == Path(os.path.realpath(target))


def test_resolves_dot_to_root(tmp_path: Path) -> None:
    assert resolve_within(tmp_path, ".") == Path(os.path.realpath(tmp_path))


def test_resolves_empty_to_root(tmp_path: Path) -> None:
    assert resolve_within(tmp_path) == Path(os.path.realpath(tmp_path))


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "..", "etc", "passwd")


def test_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "/etc/passwd")


def test_rejects_embedded_traversal(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "sub/../../etc/passwd")


def test_rejects_sibling_prefix_collision(tmp_path: Path) -> None:
    """A path like ``/root-evil`` must not be treated as inside ``/root``."""
    sibling = tmp_path.parent / (tmp_path.name + "-evil")
    sibling.mkdir(exist_ok=True)
    (sibling / "pwned.txt").write_text("no")
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "..", tmp_path.name + "-evil", "pwned.txt")


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secrets"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(secret)
    with pytest.raises(PathTraversalError):
        resolve_within(tmp_path, "link.txt")
