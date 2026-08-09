"""Aggregated tool specifications and argument models.

Importing ``ALL_SPECS`` and ``ARGUMENT_MODELS`` from this package gives the
executor (and later the LLM layer) a single registry of every tool without
coupling them to the individual spec modules.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.tools.schemas import ToolName, ToolSpec
from app.tools.specs.filesystem import MODELS as _FILESYSTEM_MODELS
from app.tools.specs.filesystem import SPECS as _FILESYSTEM_SPECS
from app.tools.specs.git import MODELS as _GIT_MODELS
from app.tools.specs.git import SPECS as _GIT_SPECS
from app.tools.specs.terminal import MODELS as _TERMINAL_MODELS
from app.tools.specs.terminal import SPECS as _TERMINAL_SPECS
from app.tools.specs.tests import MODELS as _TESTS_MODELS
from app.tools.specs.tests import SPECS as _TESTS_SPECS

ALL_SPECS: list[ToolSpec] = [
    *_FILESYSTEM_SPECS,
    *_TERMINAL_SPECS,
    *_GIT_SPECS,
    *_TESTS_SPECS,
]

ARGUMENT_MODELS: dict[ToolName, type[BaseModel]] = {
    **_FILESYSTEM_MODELS,
    **_TERMINAL_MODELS,
    **_GIT_MODELS,
    **_TESTS_MODELS,
}
