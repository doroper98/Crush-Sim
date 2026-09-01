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


def f20narrow(value: float) -> str:
    """A real in a 20-character field whose digits stay in the last 10 columns.

    The pinned starter reads the /SHELL and /SH3N trailing thickness from the
    LAST TEN columns of the field, not from all twenty: a value whose text is
    eleven characters or longer spills its leading digits out of that window
    and the remainder parses as garbage. Measured on a gauged can wall,
    ``0.0856419204`` (twelve characters) read back as 856,419,204 mm - one
    element outweighed the whole cell by four orders of magnitude, and the
    kinetic energy this fed created a numerical explosion within 100 cycles.
    Ten significant characters keep six significant figures on any thickness
    this pipeline meshes, which is far inside gauge accuracy.
    """
    text = f"{float(value):.9g}"
    precision = 9
    while len(text) > 10 and precision > 1:
        precision -= 1
        text = f"{float(value):.{precision}g}"
    return f"{text:>{REAL_WIDTH}}"
