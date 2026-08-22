"""FR-01 - CATPart to STEP batch conversion (Windows + CATIA V5 COM only)."""

from __future__ import annotations

from .catia import ConversionRecord, batch_convert, catia_available, write_conversion_log

__all__ = [
    "ConversionRecord",
    "batch_convert",
    "catia_available",
    "write_conversion_log",
]
