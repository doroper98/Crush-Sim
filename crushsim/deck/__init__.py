"""FR-04 - OpenRadioss deck generation.

See the module docstring of :mod:`crushsim.deck.writer` for the standing
warning about validating deck syntax against the pinned starter.
"""

from __future__ import annotations

from .writer import (
    DECK_VALIDATION_WARNING,
    DeckPart,
    DeckResult,
    DriveDefinition,
    RadiossDeckWriter,
    build_deck,
)

__all__ = [
    "DECK_VALIDATION_WARNING",
    "DeckPart",
    "DeckResult",
    "DriveDefinition",
    "RadiossDeckWriter",
    "build_deck",
]
