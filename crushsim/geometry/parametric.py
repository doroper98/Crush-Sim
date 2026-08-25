"""Parametric can and reference-tool definitions (FR-02, Phase 1).

The Phase-1 proof of concept never touches CAD: the can is a code-generated
cylindrical **shell** (a surface, not a solid) with the wall thickness carried
as a shell property, exactly as required for thin-walled parts (spec §4 FR-02
"extract the outer skin only, thickness is a parameter").

Coordinate convention (spec §5.2): can axis is +Z, bottom face at Z = 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from ..errors import GeometryError
from ..units import UNIT_SYSTEM

ToolKind = Literal["platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller", "step"]


@dataclass(frozen=True, slots=True)
class CanShell:
    """A cylindrical shell can: mid-surface geometry plus a thickness parameter.

    Attributes:
        radius: Outer radius [mm].
        height: Height [mm], bottom face at Z = 0.
        thickness: Wall thickness [mm], applied by the shell property.
        closed_bottom: Whether a flat bottom disc is part of the shell.
        closed_top: Whether a flat top disc is part of the shell.
    """

    radius: float
    height: float
    thickness: float
    closed_bottom: bool = False
    closed_top: bool = False
    unit_system: str = UNIT_SYSTEM

    def __post_init__(self) -> None:
        if self.radius <= 0.0:
            raise GeometryError(f"Can radius must be > 0 mm, got {self.radius!r}")
        if self.height <= 0.0:
            raise GeometryError(f"Can height must be > 0 mm, got {self.height!r}")
        if self.thickness <= 0.0:
            raise GeometryError(f"Wall thickness must be > 0 mm, got {self.thickness!r}")
        if self.thickness >= self.radius:
            raise GeometryError(
                f"Wall thickness ({self.thickness} mm) must be far below the radius "
                f"({self.radius} mm) for a shell idealisation to be valid."
            )

    # -- derived quantities -------------------------------------------------

    @property
    def diameter(self) -> float:
        """Outer diameter [mm]."""
        return 2.0 * self.radius

    @property
    def mid_surface_radius(self) -> float:
        """Radius of the shell mid-surface [mm]."""
        return self.radius - 0.5 * self.thickness

    @property
    def wall_area(self) -> float:
        """Lateral (side wall) mid-surface area [mm^2]."""
        return 2.0 * math.pi * self.mid_surface_radius * self.height

    @property
    def surface_area(self) -> float:
        """Total meshed mid-surface area [mm^2], including closed end discs."""
        area = self.wall_area
        disc = math.pi * self.mid_surface_radius**2
        if self.closed_bottom:
            area += disc
        if self.closed_top:
            area += disc
        return area

    @property
    def cross_section_area(self) -> float:
        """Wall cross-section area [mm^2] = 2*pi*R_mid*t (axial load carrying)."""
        return 2.0 * math.pi * self.mid_surface_radius * self.thickness

    @property
    def slenderness(self) -> float:
        """Diameter-to-thickness ratio D/t [-]; beverage cans are O(600)."""
        return self.diameter / self.thickness

    def volume_of_material(self) -> float:
        """Volume of the shell material [mm^3] (thin-wall approximation)."""
        return self.surface_area * self.thickness

    def mass(self, density: float) -> float:
        """Mass [tonne] for a density in tonne/mm^3."""
        return self.volume_of_material() * float(density)

    def bounding_box(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Axis-aligned bounding box ``((xmin, ymin, zmin), (xmax, ymax, zmax))`` [mm]."""
        r = self.radius
        return ((-r, -r, 0.0), (r, r, self.height))

    def summary(self) -> dict[str, float | bool | str]:
        """Flat dictionary for reports and run summaries."""
        return {
            "kind": "parametric_can",
            "radius_mm": self.radius,
            "height_mm": self.height,
            "thickness_mm": self.thickness,
            "diameter_mm": self.diameter,
            "mid_surface_radius_mm": self.mid_surface_radius,
            "surface_area_mm2": self.surface_area,
            "cross_section_area_mm2": self.cross_section_area,
            "slenderness_D_over_t": self.slenderness,
            "closed_bottom": self.closed_bottom,
            "closed_top": self.closed_top,
            "unit_system": self.unit_system,
        }


@dataclass(frozen=True, slots=True)
class ToolShape:
    """Reference rigid tool (REF_TOOL) driven by an imposed displacement.

    Attributes:
        kind: Tool archetype (spec §5.3).
        origin: Tool reference point [mm].
        direction: Unit drive direction.
        size: Characteristic in-plane size [mm] (platen/jig plate edge length).
        radius: Indenter radius [mm], used by ``kind == 'indenter'``.
        step_path: Real-shape jig STEP file when ``kind == 'step'``.
    """

    kind: ToolKind
    origin: tuple[float, float, float]
    direction: tuple[float, float, float]
    size: float
    radius: float = 0.0
    step_path: str | None = None

    def __post_init__(self) -> None:
        norm = math.sqrt(sum(c * c for c in self.direction))
        if norm <= 0.0:
            raise GeometryError("Tool drive direction must be a non-zero vector")
        if self.size <= 0.0:
            raise GeometryError(f"Tool size must be > 0 mm, got {self.size!r}")

    @property
    def unit_direction(self) -> tuple[float, float, float]:
        """Normalised drive direction."""
        norm = math.sqrt(sum(c * c for c in self.direction))
        return (self.direction[0] / norm, self.direction[1] / norm, self.direction[2] / norm)

    def summary(self) -> dict[str, object]:
        """Flat dictionary for reports."""
        return {
            "kind": self.kind,
            "origin_mm": list(self.origin),
            "direction": list(self.unit_direction),
            "size_mm": self.size,
            "radius_mm": self.radius,
            "step_path": self.step_path,
        }


def make_can(
    radius: float,
    height: float,
    thickness: float,
    *,
    closed_bottom: bool = False,
    closed_top: bool = False,
) -> CanShell:
    """Build a parametric can shell definition.

    Args:
        radius: Outer radius [mm].
        height: Height [mm].
        thickness: Wall thickness [mm].
        closed_bottom: Include a bottom disc in the meshed surface.
        closed_top: Include a top disc in the meshed surface.

    Raises:
        GeometryError: If any dimension is non-positive or the shell
            idealisation is invalid.
    """
    return CanShell(
        radius=float(radius),
        height=float(height),
        thickness=float(thickness),
        closed_bottom=closed_bottom,
        closed_top=closed_top,
    )


def make_tool(
    can: CanShell,
    kind: ToolKind,
    direction: tuple[float, float, float],
    *,
    gap: float = 0.5,
    size: float | None = None,
    indenter_radius: float = 10.0,
    step_path: str | None = None,
    height_frac: float = 0.5,
) -> ToolShape:
    """Place a reference tool against the can, offset by ``gap`` along ``-direction``.

    The tool starts just clear of the can surface and is then driven into it, so
    the contact initiates cleanly instead of starting inter-penetrated.

    Args:
        can: The can the tool acts on.
        kind: Tool archetype.
        direction: Drive direction (need not be normalised).
        gap: Initial clearance between tool and can surface [mm].
        size: Characteristic tool size [mm]; defaults to 1.5x the can diameter.
        indenter_radius: Hemispherical indenter radius [mm] for LC-3; also the
            pipe radius for the horizontal ``cylinder`` roller.
        step_path: Real-shape jig STEP file for ``kind == 'step'``.
        height_frac: Axial position of a radial tool as a fraction of the can
            height (0 = base, 1 = rim). Ignored for axial drives. A beading
            roller sits near the top (e.g. 0.9); the default is mid height.

    Raises:
        GeometryError: If the direction is degenerate or the gap is negative.
    """
    if gap < 0.0:
        raise GeometryError(f"Tool gap must be >= 0 mm, got {gap!r}")
    norm = math.sqrt(sum(c * c for c in direction))
    if norm <= 0.0:
        raise GeometryError("Tool drive direction must be a non-zero vector")
    unit = (direction[0] / norm, direction[1] / norm, direction[2] / norm)
    tool_size = float(size) if size is not None else 1.5 * can.diameter

    axial = abs(unit[2]) > 0.9
    if axial:
        # Axial tool (LC-1): sits above the can top (or below the bottom).
        contact_z = can.height if unit[2] < 0.0 else 0.0
        origin = (0.0, 0.0, contact_z - unit[2] * gap)
    else:
        # Radial tool (LC-2/LC-3): sits off the side wall at mid height. Every
        # builder places the tool's nearest feature at the origin (the V-block
        # apex, the plate plane, the sphere's and the cylinder's front), so the
        # offset is the can surface plus the gap - adding the tool radius here
        # would double-count the builder's own setback and leave the tool
        # radius' worth of dead air in front of the can.
        offset = can.radius + gap
        origin = (-unit[0] * offset, -unit[1] * offset, height_frac * can.height)

    return ToolShape(
        kind=kind,
        origin=origin,
        direction=unit,
        size=tool_size,
        radius=indenter_radius if kind in ("indenter", "cylinder", "bead_roller") else 0.0,
        step_path=step_path,
    )
