"""Quality-gate evaluation (spec §7, ADR-06).

Every threshold is imported from :mod:`crushsim.units`; nothing numeric is
hardcoded here. A failed gate stops the pipeline - artefacts that fail never
advance to the next stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..units import (
    ADDED_MASS_MAX,
    ENERGY_ERROR_MAX,
    HOURGLASS_TO_INTERNAL_MAX,
    KINETIC_TO_INTERNAL_MAX,
    MESH_MAX_ASPECT_RATIO,
    MESH_MAX_NON_MANIFOLD_EDGES,
    MESH_MAX_TRIANGLE_FRACTION,
    MESH_MIN_EDGE_LENGTH_MM,
    MESH_MIN_SICN,
    within,
)

Mode = Literal["max", "min"]


@dataclass(frozen=True, slots=True)
class GateMetric:
    """One measured quantity judged against one limit."""

    name: str
    value: float
    limit: float
    mode: Mode
    unit: str = ""
    recommendation: str = ""

    @property
    def passed(self) -> bool:
        """Whether the measured value satisfies the limit."""
        return within(self.value, self.limit, mode=self.mode)

    @property
    def comparator(self) -> str:
        """Human-readable comparison symbol (``<=`` or ``>=``)."""
        return "<=" if self.mode == "max" else ">="

    def describe(self) -> str:
        """One-line verdict, e.g. ``min_sicn = 0.412 >= 0.300 [PASS]``."""
        verdict = "PASS" if self.passed else "FAIL"
        unit = f" {self.unit}" if self.unit else ""
        return (
            f"{self.name} = {self.value:.4g}{unit} {self.comparator} "
            f"{self.limit:.4g}{unit} [{verdict}]"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "name": self.name,
            "value": self.value,
            "limit": self.limit,
            "mode": self.mode,
            "unit": self.unit,
            "passed": self.passed,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    """Verdict of a whole gate (mesh gate or solution gate)."""

    name: str
    metrics: list[GateMetric] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True only when every metric passes (ADR-06)."""
        return all(m.passed for m in self.metrics)

    @property
    def failures(self) -> list[GateMetric]:
        """The metrics that failed."""
        return [m for m in self.metrics if not m.passed]

    def recommendations(self) -> list[str]:
        """Recommended corrective actions for the failed metrics."""
        return [m.recommendation for m in self.failures if m.recommendation]

    def describe(self) -> str:
        """Multi-line human-readable verdict."""
        head = f"[{self.name}] {'PASS' if self.passed else 'FAIL'}"
        lines = [head] + [f"  {m.describe()}" for m in self.metrics]
        for rec in self.recommendations():
            lines.append(f"  -> {rec}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "name": self.name,
            "passed": self.passed,
            "metrics": [m.to_dict() for m in self.metrics],
            "info": dict(self.info),
            "recommendations": self.recommendations(),
        }


def evaluate_mesh_gate(
    *,
    min_sicn: float,
    max_aspect_ratio: float,
    min_edge_length: float,
    triangle_fraction: float,
    non_manifold_edges: int = 0,
    min_edge_limit: float | None = None,
    info: dict[str, Any] | None = None,
) -> GateResult:
    """Judge mesh statistics against the spec §7 mesh gate.

    Args:
        min_sicn: Minimum SICN over all shell elements.
        max_aspect_ratio: Maximum element aspect ratio.
        min_edge_length: Shortest element edge [mm].
        triangle_fraction: Triangles / all shell elements, in [0, 1].
        non_manifold_edges: Edges shared by more than two shells.
        min_edge_limit: Override for the minimum-edge floor [mm]. That floor
            is a COST guard ("edges below it collapse the explicit timestep"),
            not a quality one, so a deliberately refined sub-millimetre
            feature - a scored vent, say - may lower it knowingly and pay the
            timestep. Every other metric keeps its full-strength limit.
        info: Extra context (element counts, target size, ...) for the report.

    Returns:
        A :class:`GateResult` named ``mesh``.
    """
    metrics = [
        GateMetric(
            name="min_sicn",
            value=float(min_sicn),
            limit=MESH_MIN_SICN,
            mode="min",
            recommendation=(
                "Reduce the target element size or enable curvature refinement; "
                "inspect the worst elements listed in the mesh report."
            ),
        ),
        GateMetric(
            name="max_aspect_ratio",
            value=float(max_aspect_ratio),
            limit=MESH_MAX_ASPECT_RATIO,
            mode="max",
            recommendation="Lower the target size or add curvature-based refinement on fillets.",
        ),
        GateMetric(
            name="min_edge_length",
            value=float(min_edge_length),
            limit=float(min_edge_limit) if min_edge_limit else MESH_MIN_EDGE_LENGTH_MM,
            mode="min",
            unit="mm",
            recommendation=(
                "Raise the minimum mesh size: edges below the limit collapse the "
                "explicit timestep and force excessive mass scaling."
            ),
        ),
        GateMetric(
            name="triangle_fraction",
            value=float(triangle_fraction),
            limit=MESH_MAX_TRIANGLE_FRACTION,
            mode="max",
            recommendation=(
                "Improve recombination (quad-dominant): adjust the target size or use "
                "a structured/transfinite mesh on the cylindrical wall."
            ),
        ),
        GateMetric(
            name="non_manifold_edges",
            value=float(non_manifold_edges),
            limit=float(MESH_MAX_NON_MANIFOLD_EDGES),
            mode="max",
            recommendation=(
                "Fix the surface topology before meshing (fragment/heal the imported "
                "geometry); non-manifold edges break shell contact."
            ),
        ),
    ]
    return GateResult(name="mesh", metrics=metrics, info=dict(info or {}))


def evaluate_solution_gate(
    *,
    energy_error: float,
    hourglass_ratio: float,
    kinetic_ratio: float,
    added_mass_ratio: float,
    info: dict[str, Any] | None = None,
) -> GateResult:
    """Judge solver energy statistics against the spec §7 solution gate.

    Args:
        energy_error: |energy error| as a fraction (0.03 == 3%).
        hourglass_ratio: Hourglass energy / internal energy.
        kinetic_ratio: Kinetic energy / internal energy.
        added_mass_ratio: Mass-scaling added mass / physical mass.
        info: Extra context for the report.

    Returns:
        A :class:`GateResult` named ``solution``.
    """
    metrics = [
        GateMetric(
            name="energy_error",
            value=abs(float(energy_error)),
            limit=ENERGY_ERROR_MAX,
            mode="max",
            recommendation=(
                "Reduce the timestep scale factor or check for contact penetration; "
                "an energy error above the limit invalidates the result."
            ),
        ),
        GateMetric(
            name="hourglass_over_internal",
            value=float(hourglass_ratio),
            limit=HOURGLASS_TO_INTERNAL_MAX,
            mode="max",
            recommendation="Keep the fully integrated shell formulation (Ishell=24) and refine the mesh.",
        ),
        GateMetric(
            name="kinetic_over_internal",
            value=float(kinetic_ratio),
            limit=KINETIC_TO_INTERNAL_MAX,
            mode="max",
            recommendation="Lower the drive velocity or lengthen the ramp to stay quasi-static.",
        ),
        GateMetric(
            name="added_mass_ratio",
            value=float(added_mass_ratio),
            limit=ADDED_MASS_MAX,
            mode="max",
            recommendation="Raise the minimum element edge length instead of mass-scaling harder.",
        ),
    ]
    return GateResult(name="solution", metrics=metrics, info=dict(info or {}))
