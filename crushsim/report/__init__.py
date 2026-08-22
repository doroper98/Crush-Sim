"""FR-08 - automated HTML report generation."""

from __future__ import annotations

from .builder import TEMPLATE_DIR, TEMPLATE_NAME, ReportContext, build_context, render_report

__all__ = [
    "TEMPLATE_DIR",
    "TEMPLATE_NAME",
    "ReportContext",
    "build_context",
    "render_report",
]
