"""Structured plan artifact: model, tolerant parser, and formatter.

The planner's free-text answer is parsed into a :class:`TaskPlan` with the
sections the roadmap asks for (objective, assumptions, files, dependencies,
risks, validation, steps). Parsing is deliberately tolerant: the model may
render sections with ``## Objective`` headings or ``Objective:`` prefixes,
lists as bullets or numbered items, and any unrecognized section is kept in
``objective`` text or dropped rather than failing the parse.
"""

from __future__ import annotations

import re

#: Section headings the parser recognizes (case-insensitive), grouped by the
#: field they populate. Unrecognized headings are ignored.
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "objective": ("objective", "overview", "goal"),
    "assumptions": ("assumptions", "assumption"),
    "files": ("files", "files-to-change", "files to change", "changes"),
    "dependencies": ("dependencies", "dependency", "deps"),
    "risks": ("risks", "risk"),
    "validation": ("validation", "validation plan", "tests", "testing", "how to verify"),
    "steps": ("steps", "implementation steps", "implementation plan", "plan"),
}

_HEADING_RE = re.compile(r"^\s*#{1,6}\s*([^#].*?)\s*:?\s*$")
_PREFIX_RE = re.compile(r"^\s*([^:]{2,40}):\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)]|\[[ xX]\])\s+(.*)$")

#: Destructive command tokens that flag a plan for approval even when it
#: lists no files to change (e.g. a cleanup that only runs shell commands).
_DESTRUCTIVE_TOKENS = (
    "rm -rf",
    "rm -r",
    "force push",
    "--force",
    "reset --hard",
    "drop table",
    "drop database",
    "truncate table",
    "git clean -f",
)


class TaskPlan:
    """A structured plan artifact produced by the planner stage.

    Attributes:
        objective: What the task accomplishes, in one or two sentences.
        assumptions: Facts the plan relies on.
        files: Files the plan will create or modify (write targets).
        dependencies: External packages, services, or prior work needed.
        risks: Likely failure modes and mitigations.
        validation: How the result will be verified.
        steps: Ordered, actionable implementation steps.
    """

    __slots__ = (
        "assumptions",
        "dependencies",
        "files",
        "objective",
        "risks",
        "steps",
        "validation",
    )

    def __init__(
        self,
        *,
        objective: str = "",
        assumptions: list[str] | None = None,
        files: list[str] | None = None,
        dependencies: list[str] | None = None,
        risks: list[str] | None = None,
        validation: list[str] | None = None,
        steps: list[str] | None = None,
    ) -> None:
        self.objective = objective
        self.assumptions = assumptions or []
        self.files = files or []
        self.dependencies = dependencies or []
        self.risks = risks or []
        self.validation = validation or []
        self.steps = steps or []

    @property
    def needs_approval(self) -> bool:
        """Whether executing this plan requires human sign-off.

        Writing files is the primary trigger; destructive shell operations
        listed in the steps or risks also require approval.
        """
        if self.files:
            return True
        scan = " ".join([*self.steps, *self.risks, self.objective]).lower()
        return any(token in scan for token in _DESTRUCTIVE_TOKENS)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TaskPlan):
            return NotImplemented
        return (
            self.objective == other.objective
            and self.assumptions == other.assumptions
            and self.files == other.files
            and self.dependencies == other.dependencies
            and self.risks == other.risks
            and self.validation == other.validation
            and self.steps == other.steps
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to the JSON artifact stored on the task."""
        return {
            "objective": self.objective,
            "assumptions": self.assumptions,
            "files": self.files,
            "dependencies": self.dependencies,
            "risks": self.risks,
            "validation": self.validation,
            "steps": self.steps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TaskPlan:
        """Rebuild a plan from its serialized form (see :meth:`to_dict`)."""
        return cls(
            objective=str(data.get("objective", "")),
            assumptions=_string_list(data.get("assumptions")),
            files=_string_list(data.get("files")),
            dependencies=_string_list(data.get("dependencies")),
            risks=_string_list(data.get("risks")),
            validation=_string_list(data.get("validation")),
            steps=_string_list(data.get("steps")),
        )


def parse_plan(text: str) -> TaskPlan:
    """Parse a planner answer into a :class:`TaskPlan`.

    Sections are detected by heading lines (``## Objective`` or
    ``Objective: ...``) and collected in order. List sections accept bullets,
    numbered items, and bare lines; the objective takes the section's plain
    text. A heading line that is not a recognized section ends the current
    section and drops the unknown content. A plan with no recognizable
    sections keeps the whole text as its objective.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        section = _match_section(line)
        if section is not None:
            key, inline = section
            current = key
            sections.setdefault(key, [])
            if inline:
                sections[key].append(inline)
            continue
        if _looks_like_heading(line):
            current = None
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            sections[current].append(bullet.group(1).strip())
        else:
            sections[current].append(line)

    def _field(key: str) -> list[str]:
        found: list[str] = []
        for alias in _SECTION_ALIASES[key]:
            found.extend(sections.get(alias, []))
        return found

    objective_lines = _field("objective")
    objective = "\n".join(objective_lines) if objective_lines else text.strip()
    return TaskPlan(
        objective=objective,
        assumptions=_field("assumptions"),
        files=_field("files"),
        dependencies=_field("dependencies"),
        risks=_field("risks"),
        validation=_field("validation"),
        steps=_field("steps"),
    )


def format_plan(plan: TaskPlan) -> str:
    """Render a plan as readable text for the CLI and agent transcript.

    The output uses ``Section:`` headings so it round-trips through
    :func:`parse_plan`.
    """
    blocks: list[str] = []
    if plan.objective:
        blocks.append(f"Objective:\n  {plan.objective}")
    for label, items in (
        ("Assumptions", plan.assumptions),
        ("Files", plan.files),
        ("Dependencies", plan.dependencies),
        ("Risks", plan.risks),
        ("Validation", plan.validation),
        ("Steps", plan.steps),
    ):
        if not items:
            continue
        body = "\n".join(f"  - {item}" for item in items)
        blocks.append(f"{label}:\n{body}")
    return "\n\n".join(blocks) if blocks else plan.objective


def _match_section(line: str) -> tuple[str, str | None] | None:
    """Return ``(section_key, inline_content)`` for a recognized heading line."""
    match = _HEADING_RE.match(line)
    if match:
        key = _canonical(match.group(1).strip())
        if key is not None:
            return key, None
        return None
    match = _PREFIX_RE.match(line)
    if match and _looks_like_key(match.group(1)):
        key = _canonical(match.group(1))
        if key is not None:
            inline = match.group(2).strip()
            return key, inline or None
        return None
    return None


def _looks_like_heading(line: str) -> bool:
    """Whether a line reads as a heading, recognized or not."""
    if _HEADING_RE.match(line):
        return True
    match = _PREFIX_RE.match(line)
    return bool(match and _looks_like_key(match.group(1)))


def _looks_like_key(text: str) -> bool:
    """Reject prose lines (``Note: ...``) as headings."""
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z _-]*", text))


def _string_list(value: object) -> list[str]:
    """Coerce a parsed ``to_dict`` value back into a list of strings."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _canonical(heading: str) -> str | None:
    normalized = " ".join(heading.lower().split())
    for key, aliases in _SECTION_ALIASES.items():
        if normalized in aliases:
            return key
    return None
