"""Single source of truth for the unit system and every numeric limit (ADR-04, spec §5.1/§7).

Unit system: **mm - s - tonne - N - MPa**.

============  ================  =========================
Quantity      Unit              Example (mild steel)
============  ================  =========================
length        mm                115.0
time          s                 1.0e-3
mass          tonne             1.0e-6
density       tonne/mm^3        7.85e-9
force         N                 1.0
stress        MPa (N/mm^2)      210000.0
energy        mJ (N*mm)         1.0
velocity      mm/s              5000.0 (= 5 m/s)
gravity       mm/s^2            9810.0
============  ================  =========================

Spec §13.3: *all* numeric limits live here. Never hardcode a gate threshold in
another module - import the constant instead.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Unit system identity
# ---------------------------------------------------------------------------

UNIT_SYSTEM: Final[str] = "mm-s-tonne-N-MPa"
"""Canonical unit-system tag, echoed into decks, run summaries and reports."""

UNIT_LENGTH: Final[str] = "mm"
UNIT_TIME: Final[str] = "s"
UNIT_MASS: Final[str] = "tonne"
UNIT_FORCE: Final[str] = "N"
UNIT_STRESS: Final[str] = "MPa"
UNIT_ENERGY: Final[str] = "mJ"
UNIT_DENSITY: Final[str] = "tonne/mm^3"

GRAVITY_MM_S2: Final[float] = 9810.0
"""Standard gravity expressed in the working unit system (mm/s^2)."""

# ---------------------------------------------------------------------------
# Conversion factors (multiply the source value by the factor)
# ---------------------------------------------------------------------------

KG_PER_M3_TO_TONNE_PER_MM3: Final[float] = 1.0e-12
"""kg/m^3 -> tonne/mm^3 (legacy material DB density conversion, FR-10)."""

G_PER_CM3_TO_TONNE_PER_MM3: Final[float] = 1.0e-9
"""g/cm^3 -> tonne/mm^3."""

M_TO_MM: Final[float] = 1.0e3
MM_TO_M: Final[float] = 1.0e-3
M_PER_S_TO_MM_PER_S: Final[float] = 1.0e3
"""m/s -> mm/s (drive velocity ramps are specified in m/s, spec §4 FR-04)."""

GPA_TO_MPA: Final[float] = 1.0e3
KN_TO_N: Final[float] = 1.0e3
JOULE_TO_MJ: Final[float] = 1.0e3
"""J (N*m) -> mJ (N*mm), the energy unit implied by mm-N."""


def kg_per_m3_to_tonne_per_mm3(rho_kg_m3: float) -> float:
    """Convert a density from kg/m^3 to tonne/mm^3 (x1e-12)."""
    return float(rho_kg_m3) * KG_PER_M3_TO_TONNE_PER_MM3


def tonne_per_mm3_to_kg_per_m3(rho_tonne_mm3: float) -> float:
    """Inverse of :func:`kg_per_m3_to_tonne_per_mm3`."""
    return float(rho_tonne_mm3) / KG_PER_M3_TO_TONNE_PER_MM3


def g_per_cm3_to_tonne_per_mm3(rho_g_cm3: float) -> float:
    """Convert a density from g/cm^3 to tonne/mm^3 (x1e-9)."""
    return float(rho_g_cm3) * G_PER_CM3_TO_TONNE_PER_MM3


def m_per_s_to_mm_per_s(v_m_s: float) -> float:
    """Convert a velocity from m/s to mm/s."""
    return float(v_m_s) * M_PER_S_TO_MM_PER_S


def mm_per_s_to_m_per_s(v_mm_s: float) -> float:
    """Convert a velocity from mm/s to m/s."""
    return float(v_mm_s) / M_PER_S_TO_MM_PER_S


def gpa_to_mpa(e_gpa: float) -> float:
    """Convert a modulus from GPa to MPa."""
    return float(e_gpa) * GPA_TO_MPA


def joule_to_mj(energy_j: float) -> float:
    """Convert energy from J (N*m) to mJ (N*mm), the mm-N system energy unit."""
    return float(energy_j) * JOULE_TO_MJ


def mm_to_m(length_mm: float) -> float:
    """Convert a length from mm to m."""
    return float(length_mm) * MM_TO_M


def m_to_mm(length_m: float) -> float:
    """Convert a length from m to mm."""
    return float(length_m) * M_TO_MM


# ---------------------------------------------------------------------------
# Mesh quality gate limits (spec §7, ADR-06)
# ---------------------------------------------------------------------------

MESH_MIN_SICN: Final[float] = 0.3
"""Minimum admissible SICN (signed inverse condition number) of any element."""

MESH_MAX_ASPECT_RATIO: Final[float] = 5.0
"""Maximum admissible element aspect ratio."""

MESH_MIN_EDGE_LENGTH_MM: Final[float] = 0.3
"""Minimum admissible element edge length [mm]; drives the explicit timestep."""

MESH_MAX_TRIANGLE_FRACTION: Final[float] = 0.15
"""Maximum admissible fraction of triangles in a quad-dominant shell mesh."""

MESH_MAX_NON_MANIFOLD_EDGES: Final[int] = 0
"""Free-edge anomaly gate: an edge shared by more than two shells is a defect.

Genuinely open rims (an open-ended can) produce legitimate free edges, so the
gate targets non-manifold edges; the plain free-edge count is reported for
inspection.
"""

SHELL_MASS_ERROR_MAX: Final[float] = 0.10
"""Largest |shell mass - solid mass| / solid mass a shell idealisation may carry.

A shell stands in for a solid wall, so its area times its thickness has to
reproduce the solid's volume. This is the one measurement that decides whether
an imported solid became a valid shell, and it needs no human to look at the
geometry: measured across every STEP in the repository, the parts that
idealise cleanly land inside +-10 % (cans: -6.6 to -9.1 %) and the ones that
must not be shells land far outside it (a 17.6 mm terminal block: +683 %).

Diagnostics like thickness uniformity do NOT separate the two - a passing part
measured 43 % uniform and a failing one 90 % - so they are reported, not gated.
"""

SHELL_WELD_SEAM_FRACTION_MIN: Final[float] = 0.5
"""Fraction of the shorter part's boundary that a weld must actually share.

A weld is shared nodes, but "more than zero shared nodes" is not a weld: two
parts grazing at a corner pass that test while the foil is still free to lift
off. The seam has to be a real length of the boundary."""


# ---------------------------------------------------------------------------
# Meshing targets (spec §4 FR-03)
# ---------------------------------------------------------------------------

MESH_TARGET_SIZE_DEFAULT_MM: Final[float] = 1.2
"""Default target shell element size [mm]."""

MESH_TARGET_SIZE_MIN_MM: Final[float] = 0.8
"""Lower bound of the recommended target-size band [mm]."""

MESH_TARGET_SIZE_MAX_MM: Final[float] = 1.5
"""Upper bound of the recommended target-size band [mm]."""

MESH_REMESH_MAX_ATTEMPTS: Final[int] = 3
"""Automatic remesh attempts before the pipeline aborts (spec §4 FR-03)."""

MESH_REMESH_SHRINK_FACTOR: Final[float] = 0.75
"""Target size multiplier applied on each automatic remesh attempt."""

# ---------------------------------------------------------------------------
# Solution quality gate limits (spec §7, ADR-06)
# ---------------------------------------------------------------------------

ENERGY_ERROR_MAX: Final[float] = 0.05
"""Maximum |energy error| as a fraction (5%)."""

HOURGLASS_TO_INTERNAL_MAX: Final[float] = 0.10
"""Maximum hourglass-energy / internal-energy ratio (10%)."""

KINETIC_TO_INTERNAL_MAX: Final[float] = 0.05
"""Maximum kinetic-energy / internal-energy ratio for quasi-static runs (5%)."""

ADDED_MASS_MAX: Final[float] = 0.02
"""Maximum mass-scaling added mass as a fraction of physical mass (2%)."""

TIMESTEP_COLLAPSE_FACTOR: Final[float] = 0.01
"""Timestep-collapse detector: a drop below this fraction of the initial
timestep signals a degenerate element or a contact instability (spec §4 FR-05).
"""

# ---------------------------------------------------------------------------
# Physics / deck defaults (spec §4 FR-04, §5.3)
# ---------------------------------------------------------------------------

CONTACT_FRICTION_DEFAULT: Final[float] = 0.15
"""Coulomb friction of the global /INTER/TYPE7 contact."""

SHELL_ISHELL_FORMULATION: Final[int] = 24
"""/PROP/SHELL Ishell formulation (QEPH)."""

SHELL_INTEGRATION_POINTS: Final[int] = 5
"""Through-thickness integration points of the can shell."""

SHELL_SHEAR_FACTOR: Final[float] = 0.8333
"""Transverse shear factor for the shell property."""

DRIVE_VELOCITY_MIN_M_S: Final[float] = 1.0
"""Lower bound of the admissible quasi-static drive velocity band [m/s]."""

DRIVE_VELOCITY_MAX_M_S: Final[float] = 5.0
"""Upper bound of the admissible quasi-static drive velocity band [m/s]."""

DRIVE_VELOCITY_DEFAULT_M_S: Final[float] = 2.0
"""Default quasi-static drive velocity [m/s]."""

# ---------------------------------------------------------------------------
# Material-law conventions (spec §1.1, FR-10)
# ---------------------------------------------------------------------------

HARDENING_REFERENCE_STRAIN: Final[float] = 0.3
"""Reference plastic strain used to back out K from UTS: K = (UTS - sy) / eps_ref^n."""

# ---------------------------------------------------------------------------
# Benchmark tolerances (spec §8)
# ---------------------------------------------------------------------------

BENCH_B1_TOLERANCE: Final[float] = 0.25
"""B-1 axial crush vs. Alexander mean crush load: +/-25%."""

BENCH_B2_TOLERANCE: Final[float] = 0.10
"""B-2 ring lateral stiffness vs. curved-beam theory: +/-10%."""

BENCH_B3_TOLERANCE: Final[float] = 0.05
"""B-3 mesh convergence: peak load difference 1.0mm <-> 0.5mm <= 5%."""


def within(value: float, limit: float, *, mode: str = "max") -> bool:
    """Return whether ``value`` satisfies ``limit``.

    Args:
        value: Measured quantity.
        limit: Threshold constant from this module.
        mode: ``"max"`` for ``value <= limit``, ``"min"`` for ``value >= limit``.

    Raises:
        ValueError: If ``mode`` is neither ``"max"`` nor ``"min"``.
    """
    if mode == "max":
        return float(value) <= float(limit)
    if mode == "min":
        return float(value) >= float(limit)
    raise ValueError(f"mode must be 'max' or 'min', got {mode!r}")
