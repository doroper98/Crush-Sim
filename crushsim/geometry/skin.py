"""FR-02 - shell idealisation of imported STEP solids.

A STEP solid of a can is a *wall*: its boundary has two sides, an outer skin
and a lining facing the cavity. Meshing the boundary as-is therefore produces
two shells where the part has one, and the model carries twice the area. On
``examples/step/Honda_Can.stp`` that is 15 711 elements instead of 7 016, a
45 % mass error, and a 58 % membrane-stiffness error (EXP-004). The bending
stiffness happened to come out right only because ``2 x 0.30**3`` is within
1 % of ``0.38**3`` for the thickness that case happened to set - change the
thickness and that coincidence goes away.

This module picks the single surface that stands in for the wall, so the
downstream mesher sees one skin and :data:`crushsim.units.SHELL_INTEGRATION_POINTS`
integration points carry the thickness (see ``docs/HANDOVER.md`` §2).

Two shapes need two rules, and which one applies is decided by measurement,
not by configuration:

``hollow``
    A thin wall around a cavity - a can. Keep the outer skin, then offset it
    inwards by half the wall thickness to land on the mid-surface.

``plate``
    Solid through its thickness - a cap, a vent foil. There is no cavity, so
    every face is "outer" and keeping them all rebuilds the two-sided problem.
    Keep the dominant face and offset it by half the thickness.

Nothing here is silent: a solid whose classification is ambiguous raises
rather than quietly contributing half a shell (spec §13.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from ..errors import GeometryError

SKIN_PROBE_OFFSET_MM: Final[float] = 0.02
"""How far off a face the cavity ray starts [mm].

Large enough to clear the face's own tolerance, small enough to stay inside
the thinnest wall this pipeline meshes (the 0.1 mm vent foil, halved). A ray
started exactly on the face re-hits it immediately and every face reads as a
cavity lining.
"""

CAVITY_RAY_REACH_FACTOR: Final[float] = 2.0
"""Ray length as a multiple of the solid's bounding-box diagonal.

The ray only has to be long enough to cross the cavity and reach the far
wall; anything beyond the part's own extent cannot add a hit.
"""

HOLLOW_AREA_SHARE_MAX: Final[float] = 0.85
"""Outer-skin share of total area below which a solid counts as hollow.

A hollow wall splits its boundary roughly in half (measured: 0.54 on the
Honda can, 0.53 on the validation assembly's can body). A plate has no cavity
at all and lands at 0.96-1.00; the only faces it loses are through-hole walls,
which a ray does cross. The gap between the two populations is wide, so the
threshold sits in the middle of it rather than at either edge.
"""

SolidKind = Literal["hollow", "plate"]


@dataclass(slots=True)
class SkinResult:
    """What :func:`extract_shell_skins` decided about one solid."""

    index: int
    kind: SolidKind
    volume_mm3: float
    outer_area_mm2: float
    cavity_area_mm2: float
    kept_faces: int
    dropped_faces: int
    wall_thickness_mm: float
    """Wall thickness gauged by ray across the wall, not inferred from areas.

    An area ratio cannot supply this: deriving the thickness from the areas and
    then checking mass against those same areas is circular and always reports
    zero error. Measured on the Honda can, the area route gives 0.412 mm
    against a gauged - and cross-checked - 0.380 mm.
    """
    brep_path: Path | None = None
    """Per-solid BREP, when ``per_solid`` was requested."""
    face_gauges: dict[int, float] = field(default_factory=dict)
    """Gauged thickness per kept face, keyed by its index in ``kept`` order.

    A single number per part is a fiction for anything real: measured on the
    validation assembly only 79 % of the can's area, 55 % of the cap's and
    44 % of the vent's sits within 5 % of the part median. The vent's low
    decile *is* the score residual (0.100 mm against a 0.300 mm median), so a
    part-level thickness erases the very feature the analysis is about. This
    is what per-element thickness is built from.
    """
    uniformity: float = 1.0
    """Fraction of area within +-5 % of the median gauge.

    The number to look at before trusting a single thickness. Low means the
    part has pockets, pads or a score - places where the load path was
    deliberately thinned - and those need per-element thickness.
    """

    @property
    def outer_share(self) -> float:
        """Outer area as a fraction of the whole boundary."""
        total = self.outer_area_mm2 + self.cavity_area_mm2
        return self.outer_area_mm2 / total if total > 0 else 1.0

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "index": self.index,
            "kind": self.kind,
            "volume_mm3": self.volume_mm3,
            "outer_area_mm2": self.outer_area_mm2,
            "cavity_area_mm2": self.cavity_area_mm2,
            "outer_share": self.outer_share,
            "kept_faces": self.kept_faces,
            "dropped_faces": self.dropped_faces,
            "wall_thickness_mm": self.wall_thickness_mm,
        }


def _require_occ() -> dict[str, Any]:
    """Import the OCP names this module needs, or say what to install."""
    try:
        from OCP.BRep import BRep_Builder  # noqa: PLC0415
        from OCP.BRepAdaptor import BRepAdaptor_Surface  # noqa: PLC0415
        from OCP.BRepBndLib import BRepBndLib  # noqa: PLC0415
        from OCP.BRepGProp import BRepGProp  # noqa: PLC0415
        from OCP.BRepLProp import BRepLProp_SLProps  # noqa: PLC0415
        from OCP.BRepTools import BRepTools  # noqa: PLC0415
        from OCP.Bnd import Bnd_Box  # noqa: PLC0415
        from OCP.GProp import GProp_GProps  # noqa: PLC0415
        from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector  # noqa: PLC0415
        from OCP.STEPControl import STEPControl_Reader  # noqa: PLC0415
        from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID  # noqa: PLC0415
        from OCP.TopExp import TopExp_Explorer  # noqa: PLC0415
        from OCP.TopoDS import TopoDS, TopoDS_Compound  # noqa: PLC0415
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - OCP is the cad extra
        from ..errors import OptionalDependencyError  # noqa: PLC0415

        raise OptionalDependencyError("OCP", "cad", purpose="STEP shell idealisation") from exc
    return locals()


def extract_shell_skins(
    step_path: str | Path,
    out_path: str | Path,
    *,
    probe_offset_mm: float = SKIN_PROBE_OFFSET_MM,
    per_solid: bool = False,
) -> list[SkinResult]:
    """Write a BREP holding one shell surface per solid in ``step_path``.

    Args:
        step_path: STEP file, single part or assembly.
        out_path: BREP file receiving the surviving faces; feed it to the
            normal STEP mesher, which imports BREP as readily as STEP.
        probe_offset_mm: Ray start offset; see :data:`SKIN_PROBE_OFFSET_MM`.
        per_solid: Also write ``<out_path stem>.<n>.brep`` per solid and record
            the path on each result. An assembly has to be meshed as one model
            so the parts share nodes where they touch, but the mesher still has
            to know which elements belong to which part - importing the solids
            one at a time is what keeps that mapping unambiguous.

    Returns:
        One :class:`SkinResult` per solid, in file order.

    Raises:
        GeometryError: If the file has no solids, or a solid ends up with no
            face at all - a silently empty shell would mesh to nothing and the
            run would report an answer for a part that is not there (§13.5).
    """
    occ = _require_occ()
    source = Path(step_path)
    if not source.is_file():
        raise GeometryError(f"STEP file not found: {source}")

    reader = occ["STEPControl_Reader"]()
    reader.ReadFile(str(source))
    reader.TransferRoots()
    shape = reader.OneShape()

    builder = occ["BRep_Builder"]()
    compound = occ["TopoDS_Compound"]()
    builder.MakeCompound(compound)

    target = Path(out_path)
    results: list[SkinResult] = []
    explorer = occ["TopExp_Explorer"](shape, occ["TopAbs_SOLID"])
    index = 0
    while explorer.More():
        solid = occ["TopoDS"].Solid_s(explorer.Current())
        index += 1
        one = occ["TopoDS_Compound"]()
        builder.MakeCompound(one)
        result = _classify_solid(
            occ, solid, index, builder, compound, probe_offset_mm, also=one
        )
        if per_solid:
            side = target.with_suffix(f".{index}.brep")
            occ["BRepTools"].Write_s(one, str(side))
            result.brep_path = side
        results.append(result)
        explorer.Next()

    if not results:
        raise GeometryError(
            f"{source} contains no solids. A surface/sheet body needs no shell "
            "idealisation - mesh it directly with mesh_step_surfaces."
        )
    occ["BRepTools"].Write_s(compound, str(target))
    return results


def _classify_solid(
    occ: dict[str, Any],
    solid: Any,
    index: int,
    builder: Any,
    compound: Any,
    probe_offset_mm: float,
    also: Any = None,
) -> SkinResult:
    """Split one solid's boundary and add its shell faces to ``compound``."""
    props = occ["GProp_GProps"]()
    occ["BRepGProp"].VolumeProperties_s(solid, props)
    volume = float(props.Mass())

    box = occ["Bnd_Box"]()
    occ["BRepBndLib"].Add_s(solid, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    reach = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    reach *= CAVITY_RAY_REACH_FACTOR

    intersector = occ["IntCurvesFace_ShapeIntersector"]()
    intersector.Load(solid, 1.0e-6)

    outer: list[tuple[Any, float]] = []
    measured: list[tuple[float, float]] = []
    gauges: dict[int, float] = {}
    cavity_area = outer_area = 0.0
    dropped = 0

    explorer = occ["TopExp_Explorer"](solid, occ["TopAbs_FACE"])
    while explorer.More():
        face = occ["TopoDS"].Face_s(explorer.Current())
        area_props = occ["GProp_GProps"]()
        occ["BRepGProp"].SurfaceProperties_s(face, area_props)
        area = float(area_props.Mass())

        if _faces_a_cavity(occ, face, intersector, reach, probe_offset_mm):
            cavity_area += area
            dropped += 1
        else:
            outer.append((face, area))
            outer_area += area
            gauge = _wall_thickness_at(occ, face, intersector, reach, probe_offset_mm)
            if gauge is not None:
                measured.append((gauge, area))
            gauges[len(outer) - 1] = gauge if gauge is not None else 0.0
        explorer.Next()

    if not outer:
        raise GeometryError(
            f"Solid {index} of the imported shape lost every face to the cavity "
            "test, so it would mesh to nothing. Check the shape's face "
            "orientations, or raise probe_offset_mm above the local tolerance."
        )

    total = outer_area + cavity_area
    share = outer_area / total if total > 0 else 1.0
    kind: SolidKind = "hollow" if share < HOLLOW_AREA_SHARE_MAX else "plate"

    if kind == "hollow":
        # The cavity test already removed the lining, so what is left is the
        # single outer skin.
        keep = [face for face, _ in outer]
        mid_area = 0.5 * total
    else:
        # A plate has no cavity, so every face survived the ray test - keeping
        # them all would rebuild exactly the two-sided shell this module
        # exists to avoid. Its mid-plane is one face: the dominant one, which
        # already carries the plate's openings (the vent stadium, the terminal
        # holes) as holes in the surface.
        winner = max(range(len(outer)), key=lambda i: outer[i][1])
        face, mid_area = outer[winner]
        keep = [face]
        gauges = {0: gauges.get(winner, 0.0)}
        dropped += len(outer) - 1
        outer_area = mid_area
    # Prefer the thickness the shape actually has over one inferred from
    # areas. Averaging the outer and cavity areas underestimates the
    # mid-surface (the lining carries fewer faces than a pure offset would),
    # which on the Honda can put the wall at 0.412 mm against a gauged 0.389 -
    # a 6 % mass error that survives every area-based cross-check because both
    # sides of it come from the same wrong number.
    thickness = _gauged_thickness(measured)
    if thickness <= 0.0:
        thickness = volume / mid_area if mid_area > 0 else 0.0

    for face in keep:
        builder.Add(compound, face)
        if also is not None:
            builder.Add(also, face)

    return SkinResult(
        index=index,
        kind=kind,
        volume_mm3=volume,
        outer_area_mm2=outer_area,
        cavity_area_mm2=cavity_area,
        kept_faces=len(keep),
        dropped_faces=dropped,
        wall_thickness_mm=thickness,
        face_gauges=gauges,
        uniformity=_uniformity(measured, thickness),
    )


def _uniformity(measured: list[tuple[float, float]], median: float) -> float:
    """Fraction of area whose gauge is within 5 % of ``median``."""
    if not measured or median <= 0.0:
        return 1.0
    total = sum(w for _, w in measured)
    if total <= 0.0:
        return 1.0
    near = sum(w for g, w in measured if abs(g - median) <= 0.05 * median)
    return near / total


def _gauged_thickness(measured: list[tuple[float, float]]) -> float:
    """Area-weighted median of the per-face wall gauges.

    The mean is unusable: a rim or a hole wall casts its inward ray along the
    part instead of across the wall, and those few long readings drag the
    average to twice the truth (measured: 0.76 mm on a 0.39 mm Honda wall).
    Most of a shell's *area* is wall, so the median of the area distribution
    lands on the wall and the outliers stay outliers.
    """
    if not measured:
        return 0.0
    ordered = sorted(measured)
    total = sum(w for _, w in ordered)
    if total <= 0.0:
        return 0.0
    seen = 0.0
    for gauge, weight in ordered:
        seen += weight
        if seen >= total / 2.0:
            return float(gauge)
    return float(ordered[-1][0])


def _wall_thickness_at(
    occ: dict[str, Any],
    face: Any,
    intersector: Any,
    reach: float,
    probe_offset_mm: float,
) -> float | None:
    """Local wall thickness under an outer face, by ray to the cavity lining.

    Cast inwards from just under the face: the first thing the ray meets is
    the other side of the wall, so the hit distance is the thickness there.
    Returns ``None`` where no lining is found - a rim, or a solid section -
    so those faces do not drag the average.
    """
    u0, u1, v0, v1 = occ["BRepTools"].UVBounds_s(face)
    props = occ["BRepLProp_SLProps"](
        occ["BRepAdaptor_Surface"](face), (u0 + u1) / 2.0, (v0 + v1) / 2.0, 1, 1.0e-7
    )
    if not props.IsNormalDefined():
        return None
    normal, point = props.Normal(), props.Value()
    sign = -1.0 if face.Orientation() == occ["TopAbs_REVERSED"] else 1.0
    inward = occ["gp_Dir"](-sign * normal.X(), -sign * normal.Y(), -sign * normal.Z())
    start = occ["gp_Pnt"](
        point.X() + inward.X() * probe_offset_mm,
        point.Y() + inward.Y() * probe_offset_mm,
        point.Z() + inward.Z() * probe_offset_mm,
    )
    intersector.PerformNearest(occ["gp_Lin"](start, inward), 0.0, reach)
    if not (intersector.IsDone() and intersector.NbPnt() > 0):
        return None
    return float(intersector.WParameter(1)) + probe_offset_mm


def _faces_a_cavity(
    occ: dict[str, Any],
    face: Any,
    intersector: Any,
    reach: float,
    probe_offset_mm: float,
) -> bool:
    """Whether this face lines a cavity rather than the outside.

    Point-in-solid cannot answer this: a hollow can's solid *is* the wall, so
    its cavity is outside the solid and both skins probe to "outside". What
    separates them is what lies beyond - leave the outer skin and you are in
    open space, leave the lining and you must hit the far wall, because a
    cavity is enclosed. So: cast the ray and see if anything is there.
    """
    u0, u1, v0, v1 = occ["BRepTools"].UVBounds_s(face)
    props = occ["BRepLProp_SLProps"](
        occ["BRepAdaptor_Surface"](face), (u0 + u1) / 2.0, (v0 + v1) / 2.0, 1, 1.0e-7
    )
    if not props.IsNormalDefined():
        # A degenerate patch cannot be classified; keeping it is the safe side
        # (an extra face meshes, a missing one leaves a hole in the shell).
        return False
    normal, point = props.Normal(), props.Value()
    # BRepLProp reports the SURFACE normal; a REVERSED face's outward direction
    # is the opposite one.
    sign = -1.0 if face.Orientation() == occ["TopAbs_REVERSED"] else 1.0
    direction = occ["gp_Dir"](sign * normal.X(), sign * normal.Y(), sign * normal.Z())
    start = occ["gp_Pnt"](
        point.X() + direction.X() * probe_offset_mm,
        point.Y() + direction.Y() * probe_offset_mm,
        point.Z() + direction.Z() * probe_offset_mm,
    )
    intersector.PerformNearest(occ["gp_Lin"](start, direction), 0.0, reach)
    return bool(intersector.IsDone() and intersector.NbPnt() > 0)


def shell_mass_error(
    *,
    meshed_area_mm2: float,
    thickness_mm: float,
    solid_volume_mm3: float,
) -> float:
    """Relative mass error of a shell idealisation, as a fraction.

    The quantitative check EXP-004 judges shell idealisation by: a correct
    mid-surface times its thickness reproduces the solid's volume, so mass is
    conserved. A two-sided skin doubles the area and fails this by roughly the
    ratio of the two skins.

    Returns:
        ``(shell_volume - solid_volume) / solid_volume``; positive means the
        shell model is heavier than the part it stands for.

    Raises:
        GeometryError: If the solid volume is not positive.
    """
    if solid_volume_mm3 <= 0.0:
        raise GeometryError(
            f"Solid volume must be > 0 mm3 to judge a shell against it, got {solid_volume_mm3}"
        )
    return (meshed_area_mm2 * thickness_mm - solid_volume_mm3) / solid_volume_mm3


def offset_to_mid_surface(mesh: Any, thickness_mm: float) -> float:
    """Move a meshed outer skin onto the wall's mid-surface, in place.

    :func:`extract_shell_skins` keeps the *outer* skin, which sits half a wall
    proud of the surface the shell should occupy. On a can that is a small but
    systematic error - the outer skin of the Honda can measures 19 324 mm²
    against a 18 065 mm² mid-surface, so the shell carries 7 % too much mass
    and, being further from the axis, too much second moment.

    The offset is applied to the mesh rather than to the B-rep because OCC's
    surface offset is fragile on healed CAD (self-intersections at fillets
    tighter than the offset), while moving nodes along an averaged vertex
    normal is unconditional. It is exact for the planes and cylinders a can
    wall is made of; on a fillet of radius r it is off by O(t²/r), which for
    t = 0.4 mm and the millimetre fillets here is below the mesh size.

    Args:
        mesh: A :class:`~crushsim.meshing.mesh_data.ShellMesh` of the outer skin.
        thickness_mm: Wall thickness; nodes move inwards by half of it.

    Returns:
        The area ratio ``after / before`` - below 1 for a convex part, and the
        number that closes the mass error.

    Raises:
        GeometryError: If the thickness is not positive.
    """
    import numpy as np  # noqa: PLC0415

    if thickness_mm <= 0.0:
        raise GeometryError(f"Wall thickness must be > 0 mm to offset, got {thickness_mm}")

    nodes = np.asarray(mesh.nodes, dtype=float)
    index = {int(t): i for i, t in enumerate(mesh.node_ids)}
    centre = nodes.mean(axis=0)
    normals = np.zeros_like(nodes)

    def faces(block: Any, corners: int, coords: Any) -> tuple[Any, Any, Any]:
        """(rows, per-face outward normal, per-face area) for one element block."""
        rows = np.vectorize(index.__getitem__)(block)
        pts = coords[rows]
        # Newell's normal: valid for a warped quad, unlike the cross product of
        # two edges, and its magnitude is twice the face area.
        n = np.zeros((pts.shape[0], 3), dtype=float)
        for k in range(corners):
            a, b = pts[:, k], pts[:, (k + 1) % corners]
            n[:, 0] += (a[:, 1] - b[:, 1]) * (a[:, 2] + b[:, 2])
            n[:, 1] += (a[:, 2] - b[:, 2]) * (a[:, 0] + b[:, 0])
            n[:, 2] += (a[:, 0] - b[:, 0]) * (a[:, 1] + b[:, 1])
        # Orient each face outwards BEFORE accumulating. Healed CAD does not
        # guarantee consistent winding across a sewn shell, and mixing raw
        # windings at a shared node lets contributions cancel: a cube corner
        # then moves inwards in x and y but outwards in z. Fixing the sign per
        # face against the part centroid removes the dependency on winding.
        # This assumes a star-shaped part - true for cans and plates, and the
        # shapes this pipeline idealises are exactly those.
        face_centre = pts.mean(axis=1)
        flip = np.einsum("ij,ij->i", n, face_centre - centre) < 0.0
        n[flip] *= -1.0
        return rows, n, np.linalg.norm(n, axis=1) / 2.0

    area_before = 0.0
    for block, corners in ((mesh.quads, 4), (mesh.tris, 3)):
        if block.size == 0:
            continue
        rows, n, area = faces(block, corners, nodes)
        for k in range(corners):
            np.add.at(normals, rows[:, k], n)
        area_before += float(area.sum())

    lengths = np.linalg.norm(normals, axis=1)
    usable = lengths > 0.0
    unit = np.zeros_like(normals)
    unit[usable] = normals[usable] / lengths[usable, None]
    moved = nodes - unit * (thickness_mm / 2.0)
    mesh.nodes = moved

    area_after = 0.0
    for block, corners in ((mesh.quads, 4), (mesh.tris, 3)):
        if block.size == 0:
            continue
        _, _, area = faces(block, corners, moved)
        area_after += float(area.sum())

    mesh.metadata["mid_surface_offset_mm"] = thickness_mm / 2.0
    mesh.metadata["area_before_offset_mm2"] = area_before
    mesh.metadata["area_after_offset_mm2"] = area_after
    return area_after / area_before if area_before > 0 else 1.0


ELEMENT_GAUGE_OUTLIER_FACTOR: Final[float] = 3.0
"""Reject an element gauge above this multiple of the part's median.

A ray that finds no opposite wall - at a rim, across an opening, or along an
open end - runs the length of the part instead of across its wall and comes
back with a reading in the hundreds of millimetres. Measured on the validation
can: 218 mm readings on 6 961 of 18 117 elements, against a 0.65 mm wall.
Those elements fall back to the part median rather than poisoning the deck.
"""


def gauge_elements(
    step_path: str | Path,
    solid_index: int,
    centroids: Any,
    normals: Any,
    *,
    fallback_mm: float,
    probe_offset_mm: float = SKIN_PROBE_OFFSET_MM,
) -> Any:
    """Per-element wall thickness, by ray from each element into the solid.

    One sample per *face* is not enough for the parts this matters for. A
    pocket milled for weight, a pad left on the load path and a vent score all
    vary the thickness *within* a face, and a single sample at the face's
    parametric midpoint reports whichever of them it happened to land on -
    measured on the validation vent, that put the 0.100 mm score residual on
    all 1 709 elements of a 0.400 mm foil. Sampling per element is what makes
    a thinned region come out thinned and its surroundings come out nominal.

    Args:
        step_path: The STEP the skins came from.
        solid_index: 1-based index of the solid these elements belong to.
        centroids: ``(N, 3)`` element centroids.
        normals: ``(N, 3)`` element normals, any consistent orientation.
        fallback_mm: Thickness for elements whose ray finds no opposite wall.
        probe_offset_mm: Ray start offset.

    Returns:
        ``(N,)`` thickness array.

    Raises:
        GeometryError: If ``solid_index`` is not in the file.
    """
    import numpy as np  # noqa: PLC0415

    occ = _require_occ()
    reader = occ["STEPControl_Reader"]()
    reader.ReadFile(str(step_path))
    reader.TransferRoots()
    shape = reader.OneShape()

    explorer = occ["TopExp_Explorer"](shape, occ["TopAbs_SOLID"])
    solid = None
    index = 0
    while explorer.More():
        index += 1
        if index == solid_index:
            solid = occ["TopoDS"].Solid_s(explorer.Current())
            break
        explorer.Next()
    if solid is None:
        raise GeometryError(f"{step_path} has no solid {solid_index}")

    box = occ["Bnd_Box"]()
    occ["BRepBndLib"].Add_s(solid, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    reach = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    intersector = occ["IntCurvesFace_ShapeIntersector"]()
    intersector.Load(solid, 1.0e-6)

    points = np.asarray(centroids, dtype=float)
    dirs = np.asarray(normals, dtype=float)
    lengths = np.linalg.norm(dirs, axis=1)
    out = np.full(points.shape[0], float(fallback_mm), dtype=float)

    for i in range(points.shape[0]):
        if lengths[i] <= 0.0:
            continue
        n = dirs[i] / lengths[i]
        best = None
        # Try both ways: the mesh's normal orientation is not guaranteed to
        # point into the material, and the wall is whichever side answers.
        for sign in (1.0, -1.0):
            d = occ["gp_Dir"](sign * n[0], sign * n[1], sign * n[2])
            start = occ["gp_Pnt"](
                points[i, 0] + d.X() * probe_offset_mm,
                points[i, 1] + d.Y() * probe_offset_mm,
                points[i, 2] + d.Z() * probe_offset_mm,
            )
            intersector.PerformNearest(occ["gp_Lin"](start, d), 0.0, reach)
            if intersector.IsDone() and intersector.NbPnt() > 0:
                hit = float(intersector.WParameter(1)) + probe_offset_mm
                if best is None or hit < best:
                    best = hit
        if best is not None and best <= fallback_mm * ELEMENT_GAUGE_OUTLIER_FACTOR:
            out[i] = best
    return out
