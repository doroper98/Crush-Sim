"""FR-05 - OpenRadioss execution wrapper and log parsing."""

from __future__ import annotations

from .logparse import CycleRecord, LogSummary, parse_log, tail
from .runner import (
    RunResult,
    StageResult,
    resolve_executable,
    run_solver,
    solver_status,
    write_run_summary,
)

__all__ = [
    "CycleRecord",
    "LogSummary",
    "RunResult",
    "StageResult",
    "parse_log",
    "resolve_executable",
    "run_solver",
    "solver_status",
    "tail",
    "write_run_summary",
]
