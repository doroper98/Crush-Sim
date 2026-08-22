"""FR-10 - legacy material harvester.

Extracts material *data* (and only data - spec §13.1) from the legacy
TypeScript material library and emits YAML cards, all tagged
``verified: false``.
"""

from __future__ import annotations

from .legacy_ts import (
    HarvestedMaterial,
    HarvestReport,
    harvest_file,
    parse_material_source,
    write_cards,
)

__all__ = [
    "HarvestedMaterial",
    "HarvestReport",
    "harvest_file",
    "parse_material_source",
    "write_cards",
]
