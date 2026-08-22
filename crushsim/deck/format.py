"""Fixed-width field helpers for the OpenRadioss block format.

OpenRadioss input is a *block format*: a keyword line (``/KEYWORD/id``) followed
by data lines whose fields occupy fixed columns - 10 characters for integers and
20 characters for reals in the standard (I10, F20.0) layout.
"""

from __future__ import annotations

from collections.abc import Iterable

INT_WIDTH: int = 10
"""Column width of an integer field."""

REAL_WIDTH: int = 20
"""Column width of a real field."""

RULER: str = (
    "#---1----|----2----|----3----|----4----|----5----|"
    "----6----|----7----|----8----|----9----|---10----|"
)
"""Column ruler comment, conventional in Radioss decks."""


def i10(value: int | float) -> str:
    """Format an integer into a 10-character right-aligned field."""
    return f"{int(value):>{INT_WIDTH}d}"


def f20(value: float) -> str:
    """Format a real into a 20-character right-aligned field.

    Uses a general format with enough significant digits to round-trip single
    precision without ever exceeding the column width.
    """
    text = f"{float(value):.9g}"
    if len(text) > REAL_WIDTH:  # pragma: no cover - defensive
        text = f"{float(value):.6g}"
    return f"{text:>{REAL_WIDTH}}"


def s10(value: str) -> str:
    """Format a short string into a 10-character right-aligned field."""
    return f"{value:>{INT_WIDTH}}"


def reals(values: Iterable[float]) -> str:
    """Format a sequence of reals as consecutive 20-character fields."""
    return "".join(f20(v) for v in values)


def title(text: str, *, width: int = 100) -> str:
    """Truncate a title line to the Radioss title-field width."""
    return text[:width]
