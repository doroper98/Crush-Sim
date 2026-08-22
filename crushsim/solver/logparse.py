"""OpenRadioss log parsing (FR-05).

The engine prints one cycle line per printout interval::

    CYCLE    TIME    TIME-STEP  ELEMENT   ERROR  I-ENERGY  K-ENERGY T  ...  MAS.ERR
        0  0.000E+00 0.1000E-06 SHELL 12   0.0%  0.000E+00 0.000E+00       0.000E+00

This module turns that text into structured data. It is deliberately tolerant:
column layouts differ slightly between releases, so cycle lines are matched by
shape (leading integer plus a run of numeric fields), and the percentage columns
are located by their trailing ``%``. Anything that cannot be parsed is reported,
never silently dropped (spec §13.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from ..units import TIMESTEP_COLLAPSE_FACTOR

_NUM: Final[str] = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][-+]?\d+)?"

_CYCLE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^\s*(?P<cycle>\d+)\s+(?P<time>{_NUM})\s+(?P<dt>{_NUM})\b(?P<rest>.*)$"
)
_PERCENT_RE: Final[re.Pattern[str]] = re.compile(rf"({_NUM})\s*%")
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(_NUM)

_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "ERROR TERMINATION",
    "** ERROR",
    "*** ERROR",
    "ERROR ID",
)
_WARNING_MARKERS: Final[tuple[str, ...]] = ("** WARNING", "*** WARNING", "WARNING ID")
_NEGATIVE_VOLUME_MARKERS: Final[tuple[str, ...]] = (
    "NEGATIVE VOLUME",
    "NEGATIVE JACOBIAN",
    "ZERO OR NEGATIVE VOLUME",
)
_NORMAL_TERMINATION_MARKERS: Final[tuple[str, ...]] = ("NORMAL TERMINATION",)


def _to_float(text: str) -> float:
    """Parse a Fortran-style real (``1.0D-3`` as well as ``1.0e-3``)."""
    return float(text.replace("D", "E").replace("d", "e"))


@dataclass(slots=True)
class CycleRecord:
    """One printed solver cycle."""

    cycle: int
    time: float
    timestep: float
    energy_error: float | None = None
    """Energy error as a fraction (the log prints a percentage)."""
    mass_error: float | None = None
    """Added-mass error as a fraction."""
    internal_energy: float | None = None
    kinetic_energy: float | None = None


@dataclass(slots=True)
class LogSummary:
    """Structured view of a starter or engine log."""

    cycles: list[CycleRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    negative_volume: bool = False
    normal_termination: bool = False

    @property
    def last_cycle(self) -> CycleRecord | None:
        """The most recent cycle record, if any."""
        return self.cycles[-1] if self.cycles else None

    @property
    def final_time(self) -> float | None:
        """Simulation time of the last cycle [s]."""
        return self.cycles[-1].time if self.cycles else None

    @property
    def min_timestep(self) -> float | None:
        """Smallest timestep seen [s]."""
        values = [c.timestep for c in self.cycles if c.timestep > 0.0]
        return min(values) if values else None

    @property
    def max_energy_error(self) -> float | None:
        """Largest |energy error| seen, as a fraction."""
        values = [abs(c.energy_error) for c in self.cycles if c.energy_error is not None]
        return max(values) if values else None

    @property
    def final_energy_error(self) -> float | None:
        """Energy error of the last cycle, as a fraction."""
        for record in reversed(self.cycles):
            if record.energy_error is not None:
                return record.energy_error
        return None

    @property
    def max_mass_error(self) -> float | None:
        """Largest added-mass error seen, as a fraction."""
        values = [abs(c.mass_error) for c in self.cycles if c.mass_error is not None]
        return max(values) if values else None

    @property
    def timestep_collapse(self) -> bool:
        """Whether the timestep collapsed relative to its initial value.

        The threshold is :data:`crushsim.units.TIMESTEP_COLLAPSE_FACTOR`.
        """
        if len(self.cycles) < 2:
            return False
        first = next((c.timestep for c in self.cycles if c.timestep > 0.0), None)
        smallest = self.min_timestep
        if first is None or smallest is None:
            return False
        return smallest < TIMESTEP_COLLAPSE_FACTOR * first

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for ``run_summary.json``."""
        return {
            "cycles_parsed": len(self.cycles),
            "final_time_s": self.final_time,
            "min_timestep_s": self.min_timestep,
            "timestep_collapse": self.timestep_collapse,
            "max_energy_error": self.max_energy_error,
            "final_energy_error": self.final_energy_error,
            "max_mass_error": self.max_mass_error,
            "negative_volume": self.negative_volume,
            "normal_termination": self.normal_termination,
            "errors": list(self.errors),
            "warnings": list(self.warnings[:50]),
            "warning_count": len(self.warnings),
        }


def parse_log(text: str) -> LogSummary:
    """Parse an OpenRadioss starter/engine log into a :class:`LogSummary`.

    Args:
        text: Full log text (or the tail of it).

    Returns:
        Structured cycle records, warnings and termination status.
    """
    summary = LogSummary()
    for raw in text.splitlines():
        line = raw.rstrip()
        upper = line.upper()

        if any(marker in upper for marker in _NEGATIVE_VOLUME_MARKERS):
            summary.negative_volume = True
        if any(marker in upper for marker in _NORMAL_TERMINATION_MARKERS):
            summary.normal_termination = True
        if any(marker in upper for marker in _ERROR_MARKERS):
            summary.errors.append(line.strip())
            continue
        if any(marker in upper for marker in _WARNING_MARKERS):
            summary.warnings.append(line.strip())
            continue

        match = _CYCLE_RE.match(line)
        if match is None:
            continue
        try:
            cycle = int(match.group("cycle"))
            time = _to_float(match.group("time"))
            dt = _to_float(match.group("dt"))
        except ValueError:  # pragma: no cover - guarded by the regex
            continue
        rest = match.group("rest")
        percents = [_to_float(m) / 100.0 for m in _PERCENT_RE.findall(rest)]
        # Strip the percentage tokens before harvesting the energy columns.
        remainder = _PERCENT_RE.sub(" ", rest)
        numbers = [_to_float(m) for m in _NUMBER_RE.findall(remainder)]
        summary.cycles.append(
            CycleRecord(
                cycle=cycle,
                time=time,
                timestep=dt,
                energy_error=percents[0] if percents else None,
                mass_error=percents[-1] if len(percents) > 1 else None,
                internal_energy=numbers[0] if numbers else None,
                kinetic_energy=numbers[1] if len(numbers) > 1 else None,
            )
        )
    return summary


def tail(text: str, lines: int = 60) -> str:
    """Return the last ``lines`` lines of ``text`` for error messages."""
    parts = text.splitlines()
    return "\n".join(parts[-lines:])
