"""FR-02 - geometry generation and STEP pre-processing.

Phase 1 works from a code-generated parametric can (:mod:`parametric`).
Phase 2 adds STEP inspection/alignment (:mod:`step_inspect`), which needs the
optional ``cad`` extra (OCP).
"""

from __future__ import annotations

from .parametric import CanShell, ToolShape, make_can, make_tool
from .step_inspect import StepSummary, inspect_step

__all__ = [
    "CanShell",
    "StepSummary",
    "ToolShape",
    "inspect_step",
    "make_can",
    "make_tool",
]
