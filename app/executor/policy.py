"""Command safety policy for sandboxed terminal runs.

Terminal commands execute inside the sandbox, which is already resource-capped
and non-root, but a misbehaving agent could still wipe a checkout or rewrite
history. The policy classifies every ``terminal_run`` command into one of
three tiers:

* :data:`CommandTier.DENY` — blocked outright; the result explains why and
  points at a safer built-in tool (``file_delete``, ``file_edit``, ...).
* :data:`CommandTier.CONFIRM` — destructive but occasionally legitimate; the
  caller must set ``confirm=True`` on the tool call to acknowledge it.
* :data:`CommandTier.ALLOW` — everything else.

Built-in patterns cover the common foot-guns; deployments can extend either
tier through ``command_deny_extra`` / ``command_confirm_extra`` settings.
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.core.config import Settings

_DENY_PATTERNS: tuple[str, ...] = (
    # Deleting files/trees — use file_delete, which is workspace-confined.
    r"\brm\b",
    r"\brmdir\b",
    r"\bunlink\b",
    r"\brm\s+-rf\b",
    # Rewriting/discarding git history or working tree.
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-f",
    r"\bgit\s+checkout\s+--(?:\s|$)",
    r"\bgit\s+stash\s+drop\b",
    r"\bgit\s+push\b.*--force\b",
    r"\bgit\s+push\b.*-f\b",
    # Raw block-device and partition operations.
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bmkswap\b",
    r"\bswapoff\b",
    r"\bfdisk\b",
    r"\bparted\b",
    r"\bmount\b",
    r"\bumount\b",
    # System control.
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bhalt\b",
    r"\bkill\b",
    r"\bkillall\b",
    r"\bpkill\b",
    r"\bsudo\b",
    r"\bsu\b",
    # Ownership/permission/metadata mutation on files.
    r"\bchmod\b",
    r"\bchown\b",
    r"\bchgrp\b",
    r"\bchattr\b",
    r"\buseradd\b",
    r"\buserdel\b",
    r"\bpasswd\b",
    r"\binsmod\b",
    r"\brmmod\b",
    # Piping remote content into a shell — arbitrary code execution.
    r"\b(?:curl|wget)\b.*\|\s*(?:sh|bash|zsh)\b",
    # Writing directly to device nodes.
    r">\s*/dev/\S+",
    r">\s*/proc/\S+",
)

_CONFIRM_PATTERNS: tuple[str, ...] = (
    # Publishing or rewriting git state.
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+(?:--soft|--mixed)\b",
    r"\bgit\s+revert\b",
    r"\bgit\s+commit\s+--amend\b",
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+tag\s+-d\b",
    r"\bgit\s+remote\b",
    # Database destruction.
    r"\bdropdb\b",
    r"\bdrop\s+(?:table|database|view|index|schema)\b",
    r"\bsqlite3\b.*\bDROP\b",
    r"\bredis-cli\b.*\b(?:FLUSH(?:ALL)?|DEL)\b",
    r"\bmongo\b.*\b(?:drop|deleteMany)\b",
    # Uninstalling packages.
    r"\bpip\s+uninstall\b",
    r"\bnpm\s+uninstall\b",
    r"\byarn\s+remove\b",
    r"\bcargo\s+clean\b",
    # Docker resource destruction.
    r"\bdocker\b.*\b(?:rmi|rm|prune|volume\s+rm)\b",
)


class CommandTier(StrEnum):
    """Classification of a command against the safety policy."""

    DENY = "deny"
    CONFIRM = "confirm"
    ALLOW = "allow"


class CommandPolicy:
    """Match terminal commands against deny/confirm rules.

    Attributes:
        enabled: When false every command is :data:`CommandTier.ALLOW`.
        deny: Compiled deny patterns (blocked outright).
        confirm: Compiled confirm patterns (require ``confirm=True``).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        deny: list[str] | None = None,
        confirm: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.deny = tuple(re.compile(p, re.IGNORECASE) for p in (deny or _DENY_PATTERNS))
        self.confirm = tuple(re.compile(p, re.IGNORECASE) for p in (confirm or _CONFIRM_PATTERNS))

    @classmethod
    def from_settings(cls, settings: Settings) -> CommandPolicy:
        """Build a policy from application settings.

        Built-in patterns are always active unless the policy is disabled;
        ``command_deny_extra`` and ``command_confirm_extra`` append additional
        regex patterns to the respective tiers.
        """
        return cls(
            enabled=settings.command_policy_enabled,
            deny=[*_DENY_PATTERNS, *settings.command_deny_extra],
            confirm=[*_CONFIRM_PATTERNS, *settings.command_confirm_extra],
        )

    def classify(self, command: str) -> CommandTier:
        """Classify a shell command string.

        ``DENY`` always wins over ``CONFIRM`` so an extended deny pattern
        cannot be downgraded by a built-in confirm rule.
        """
        if not self.enabled:
            return CommandTier.ALLOW
        if any(pattern.search(command) for pattern in self.deny):
            return CommandTier.DENY
        if any(pattern.search(command) for pattern in self.confirm):
            return CommandTier.CONFIRM
        return CommandTier.ALLOW


_DENY_HINT = (
    "blocked by the command safety policy. Prefer the built-in workspace "
    "tools instead (file_read/file_write/file_edit/file_delete/file_move, "
    "git_status/git_diff/git_commit)."
)
_CONFIRM_HINT = (
    "this command is destructive and requires explicit confirmation. "
    "Re-issue it with confirm=true only if that is truly intended."
)


def policy_message(tier: CommandTier, command: str) -> str:
    """Human-readable reason a command was classified ``tier``."""
    if tier is CommandTier.DENY:
        return f"command {command!r} {_DENY_HINT}"
    return f"command {command!r} {_CONFIRM_HINT}"
