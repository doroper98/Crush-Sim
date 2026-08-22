"""Analytic reference solutions for the §8 validation benchmarks.

These are the *reference* values a simulation is compared against. They contain
no simulation physics of their own (ADR-01) - they are textbook closed-form
expressions used as acceptance criteria.

All quantities are in the working unit system: mm, N, MPa, mJ.

References
----------
B-1  J. M. Alexander, "An approximate analysis of the collapse of thin
     cylindrical shells under axial loading", Q. J. Mech. Appl. Math. 13
     (1960). Mean crush load of axisymmetric (concertina) folding.
B-2  Curved-beam / thin-ring theory for a circular ring loaded by two
     diametrically opposed point forces (Roark, Table 9.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ALEXANDER_K1: float = 6.0
"""Leading coefficient of the Alexander mean-crush-load expression."""

ALEXANDER_K2: float = 3.44
"""Constant term of the Alexander mean-crush-load expression."""

RING_DEFLECTION_COEFFICIENT: float = math.pi / 4.0 - 2.0 / math.pi
"""Ring deflection coefficient along the load line: pi/4 - 2/pi ~= 0.1488."""


def flow_stress(sigma_y: float, uts: float) -> float:
    """Mean flow stress [MPa] used by the Alexander expression.

    Args:
        sigma_y: Initial yield stress [MPa].
        uts: Ultimate tensile strength [MPa].

    Raises:
        ValueError: If either stress is non-positive or UTS < yield.
    """
    if sigma_y <= 0.0 or uts <= 0.0:
        raise ValueError("sigma_y and uts must both be > 0 MPa")
    if uts < sigma_y:
        raise ValueError("uts must be >= sigma_y")
    return 0.5 * (sigma_y + uts)


def alexander_mean_crush_load(
    sigma_0: float,
    thickness: float,
    diameter: float,
    *,
    k1: float = ALEXANDER_K1,
    k2: float = ALEXANDER_K2,
) -> float:
    """B-1 reference: mean axial crush load [N] of a thin circular tube.

    ``P_m = sigma_0 * t^2 * (k1 * sqrt(D / t) + k2)``

    Args:
        sigma_0: Mean flow stress [MPa].
        thickness: Wall thickness [mm].
        diameter: Tube diameter [mm].
        k1: Leading coefficient (Alexander: 6).
        k2: Constant term (Alexander: 3.44).

    Returns:
        Mean crush load [N].

    Raises:
        ValueError: If any dimension or the flow stress is non-positive.
    """
    if sigma_0 <= 0.0:
        raise ValueError("sigma_0 must be > 0 MPa")
    if thickness <= 0.0 or diameter <= 0.0:
        raise ValueError("thickness and diameter must both be > 0 mm")
    return sigma_0 * thickness**2 * (k1 * math.sqrt(diameter / thickness) + k2)


def alexander_fold_wavelength(thickness: float, diameter: float) -> float:
    """Half-wavelength [mm] of the concertina fold, ``sqrt(pi * R * t / 2)``-scaled.

    Useful as a meshing sanity check: several elements must span one fold.

    Raises:
        ValueError: If any dimension is non-positive.
    """
    if thickness <= 0.0 or diameter <= 0.0:
        raise ValueError("thickness and diameter must both be > 0 mm")
    return math.sqrt(math.pi * (0.5 * diameter) * thickness / 2.0)


def ring_second_moment(thickness: float, length: float) -> float:
    """Second moment of area [mm^4] of a ring wall cross-section, ``L t^3 / 12``.

    Raises:
        ValueError: If any dimension is non-positive.
    """
    if thickness <= 0.0 or length <= 0.0:
        raise ValueError("thickness and length must both be > 0 mm")
    return length * thickness**3 / 12.0


def ring_lateral_stiffness(
    E: float,
    thickness: float,
    radius: float,
    length: float,
    *,
    plane_strain: bool = False,
    nu: float = 0.3,
) -> float:
    """B-2 reference: elastic lateral stiffness [N/mm] of a circular ring.

    A thin ring of mean radius ``R`` compressed by two diametrically opposed
    point loads deflects along the load line by::

        delta = (pi/4 - 2/pi) * P * R^3 / (E * I)

    so the stiffness is ``k = P / delta = E * I / (0.1488 * R^3)``.

    Args:
        E: Young's modulus [MPa].
        thickness: Wall thickness [mm].
        radius: Mean ring radius [mm].
        length: Axial length of the ring slice [mm].
        plane_strain: Use ``E / (1 - nu^2)`` - appropriate for a long cylinder
            where axial strain is restrained.
        nu: Poisson ratio, used only when ``plane_strain`` is set.

    Returns:
        Lateral stiffness [N/mm].

    Raises:
        ValueError: If any input is non-positive or ``nu`` is out of range.
    """
    if E <= 0.0:
        raise ValueError("E must be > 0 MPa")
    if radius <= 0.0:
        raise ValueError("radius must be > 0 mm")
    modulus = E
    if plane_strain:
        if not 0.0 <= nu < 0.5:
            raise ValueError("nu must be in [0, 0.5)")
        modulus = E / (1.0 - nu**2)
    inertia = ring_second_moment(thickness, length)
    return modulus * inertia / (RING_DEFLECTION_COEFFICIENT * radius**3)


def ring_lateral_deflection(
    force: float,
    E: float,
    thickness: float,
    radius: float,
    length: float,
    **kwargs: object,
) -> float:
    """Deflection [mm] of the ring under ``force`` [N] - inverse of the stiffness.

    Raises:
        ValueError: If the force is negative or any dimension is invalid.
    """
    if force < 0.0:
        raise ValueError("force must be >= 0 N")
    stiffness = ring_lateral_stiffness(E, thickness, radius, length, **kwargs)  # type: ignore[arg-type]
    return force / stiffness


def relative_error(measured: float, reference: float) -> float:
    """Signed relative error ``(measured - reference) / reference``.

    Raises:
        ValueError: If the reference is zero.
    """
    if reference == 0.0:
        raise ValueError("reference must be non-zero")
    return (measured - reference) / reference


@dataclass(frozen=True, slots=True)
class BenchmarkVerdict:
    """Result of comparing a simulated value against an analytic reference."""

    benchmark: str
    measured: float
    reference: float
    tolerance: float
    unit: str = ""

    @property
    def error(self) -> float:
        """Signed relative error."""
        return relative_error(self.measured, self.reference)

    @property
    def passed(self) -> bool:
        """Whether |error| is inside the tolerance."""
        return abs(self.error) <= self.tolerance

    def describe(self) -> str:
        """One-line verdict for the report."""
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"[{self.benchmark}] measured {self.measured:.5g}{self.unit} vs reference "
            f"{self.reference:.5g}{self.unit} -> {self.error:+.1%} "
            f"(tolerance +/-{self.tolerance:.0%}) [{verdict}]"
        )
