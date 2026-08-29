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

ToolKind = Literal[
    "platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller", "bead_arbor", "step"
]


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


@dataclass(frozen=True, slots=True)
class VentSpec:
    """A stadium (pill) shaped scored vent on a prismatic can's cap.

    The score is the outline of the stadium: a band of ``band`` mm width
    centred on the outline is meshed with edges aligned to it and carries
    ``score_thickness`` instead of the cap thickness. When the score tears
    (failure strain + element deletion) the inner flap opens.

    ``membrane_thickness`` switches the vent to the production construction
    seen on real prismatic caps: the whole stadium is a separate thin foil
    part (hand-pressably thin aluminium, welded to the thick cap along the
    stadium outline as merged nodes) and the score pattern is engraved on
    the foil. ``pattern`` then picks where the score runs:

    - ``"perimeter"``: the stadium outline (legacy; the flap detaches and
      ejects when it tears all round).
    - ``"petal_x"``: an X of score lines crossing at the centre. The burst
      starts at the crossing, tears run outward along the arms, and the
      four petals fold back on the UNSCORED welded perimeter - the vent
      opens without throwing a fragment, which is what the crossed coined
      grooves on production caps are for.

    Attributes:
        length: Stadium overall length [mm] (along the cap's long axis).
        width: Stadium overall width [mm] (also the end-cap diameter).
        band: Score band width [mm].
        score_thickness: Residual thickness at the score [mm].
        membrane_thickness: Foil thickness [mm]; None keeps the legacy
            single-part scored-cap construction.
        pattern: Score layout - "perimeter" or "petal_x".
    """

    length: float
    width: float
    band: float = 0.8
    score_thickness: float = 0.05
    membrane_thickness: float | None = None
    pattern: str = "perimeter"
    arc_bulge: float = 0.30
    """petal_x arm curvature: control-point offset as a fraction of the chord
    length (0 = straight arms). Auto-shrunk until the arc fits the flap."""

    def __post_init__(self) -> None:
        if not 0.0 < self.width <= self.length:
            raise GeometryError(f"Vent needs 0 < width <= length, got {self.width}x{self.length}")
        if self.band <= 0.0 or self.score_thickness <= 0.0:
            raise GeometryError("Vent band and score_thickness must be > 0")
        if self.pattern not in ("perimeter", "petal_x"):
            raise GeometryError(f"Vent pattern must be 'perimeter' or 'petal_x', got {self.pattern!r}")
        if self.membrane_thickness is not None:
            if self.membrane_thickness <= 0.0:
                raise GeometryError("Vent membrane_thickness must be > 0")
            if self.score_thickness >= self.membrane_thickness:
                raise GeometryError(
                    "Vent score_thickness must be below membrane_thickness "
                    f"(got {self.score_thickness} >= {self.membrane_thickness})"
                )
        elif self.pattern != "perimeter":
            raise GeometryError("Vent pattern 'petal_x' needs membrane_thickness (foil vent)")

    def contains(self, x: float, y: float, grow: float = 0.0) -> bool:
        """Whether cap-local point (x, y) lies inside the stadium grown by ``grow``."""
        r = self.width / 2.0 + grow / 2.0
        c = max(self.length / 2.0 - self.width / 2.0, 0.0)
        if abs(x) <= c:
            return abs(y) <= r
        return math.hypot(abs(x) - c, y) <= r

    def petal_arms(self, samples: int = 15) -> list[list[tuple[float, float]]]:
        """The four score arms of the ``petal_x`` pattern as sampled arcs.

        Each arm runs from the crossing at the origin to a point short of the
        stadium boundary (85% of the straight half-length, 72% of the half
        width) so the tears stop before the welded perimeter and the petals
        keep their hinges. The arms bow away from the long axis (quadratic
        arc, ``arc_bulge`` of the chord length), reproducing the crossed
        coined grooves on production caps - two lens shapes meeting at the
        centre. The bulge auto-shrinks until the whole arc stays inside the
        inner stadium.
        """
        ax = max(self.length / 2.0 - self.width / 2.0, self.width / 4.0) * 0.85
        # Tip height: clear the score band's inner outline by ~a band width,
        # never below 30% of the half width on very narrow vents.
        ay = max(self.width / 2.0 - 1.5 * self.band, 0.3 * self.width / 2.0)
        arms: list[list[tuple[float, float]]] = []
        for sx in (-1.0, 1.0):
            for sy in (-1.0, 1.0):
                tip = (sx * ax, sy * ay)
                chord = math.hypot(*tip)
                bulge = max(self.arc_bulge, 0.0) * chord
                for _ in range(6):
                    # Shrink until the whole arc clears the score band's inner
                    # outline by about a band width - an arc grazing that
                    # outline pinches sliver elements between the two curves
                    # (measured: SICN 0.298 vs the 0.30 gate).
                    control = (tip[0] / 2.0, tip[1] / 2.0 + sy * bulge)
                    pts = []
                    ok = True
                    for i in range(samples):
                        t = i / (samples - 1)
                        px = (1 - t) ** 2 * 0.0 + 2 * (1 - t) * t * control[0] + t * t * tip[0]
                        py = (1 - t) ** 2 * 0.0 + 2 * (1 - t) * t * control[1] + t * t * tip[1]
                        pts.append((px, py))
                        if t > 0.0 and not self.contains(px, py, grow=-3.0 * self.band):
                            ok = False
                    if ok or bulge <= 1e-6:
                        break
                    bulge *= 0.6
                arms.append(pts)
        return arms

    def score_distance(self, x: float, y: float) -> float:
        """Distance of cap-local (x, y) to the nearest petal_x score arc."""
        best = math.inf
        for arm in self.petal_arms():
            for (x0, y0), (x1, y1) in zip(arm, arm[1:]):
                dx, dy = x1 - x0, y1 - y0
                length2 = dx * dx + dy * dy
                t = 0.0 if length2 == 0.0 else max(
                    0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length2)
                )
                best = min(best, math.hypot(x - (x0 + t * dx), y - (y0 + t * dy)))
        return best


@dataclass(frozen=True, slots=True)
class BoxCan:
    """A prismatic (box) cell can: mid-surface shell with a thickness parameter.

    Coordinate convention matches the cylindrical can: height along +Z,
    bottom face at Z = 0, footprint centred on the origin with ``width``
    along X and ``depth`` along Y. The welded cap is part of the same shell
    (a weld is exact as a merged-node connection).
    """

    width: float
    height: float
    depth: float
    thickness: float
    closed_bottom: bool = True
    closed_top: bool = True
    vent: VentSpec | None = None
    unit_system: str = UNIT_SYSTEM

    def __post_init__(self) -> None:
        for label, value in (("width", self.width), ("depth", self.depth), ("height", self.height), ("thickness", self.thickness)):
            if value <= 0.0:
                raise GeometryError(f"Box can {label} must be > 0 mm, got {value!r}")
        if self.thickness >= min(self.width, self.depth) / 4.0:
            raise GeometryError("Box can walls must be thin for a shell idealisation")
        if self.vent is not None:
            if not self.closed_top:
                raise GeometryError("A vent needs a closed top (the cap carries it)")
            if self.vent.length + 2 * self.vent.band >= self.width or (
                self.vent.width + 2 * self.vent.band >= self.depth
            ):
                raise GeometryError("Vent (plus score band) must fit inside the cap")

    @property
    def half_width_mid(self) -> float:
        """Mid-surface half width [mm] (X)."""
        return (self.width - self.thickness) / 2.0

    @property
    def half_depth_mid(self) -> float:
        """Mid-surface half depth [mm] (Y)."""
        return (self.depth - self.thickness) / 2.0

    def summary(self) -> dict[str, object]:
        """Flat dictionary for reports and run summaries."""
        return {
            "kind": "box_can",
            "width_mm": self.width,
            "depth_mm": self.depth,
            "height_mm": self.height,
            "thickness_mm": self.thickness,
            "closed_bottom": self.closed_bottom,
            "closed_top": self.closed_top,
            "vent": None
            if self.vent is None
            else {
                "length_mm": self.vent.length,
                "width_mm": self.vent.width,
                "band_mm": self.vent.band,
                "score_thickness_mm": self.vent.score_thickness,
                "membrane_thickness_mm": self.vent.membrane_thickness,
                "pattern": self.vent.pattern,
            },
            "unit_system": self.unit_system,
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

    if kind == "bead_arbor":
        # Internal support: always on the can axis, at the groove height.
        return ToolShape(
            kind=kind,
            origin=(0.0, 0.0, height_frac * can.height),
            direction=unit,
            size=tool_size,
            radius=0.0,
        )

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
