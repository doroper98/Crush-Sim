"""FR-03 - Gmsh shell meshing with automatic quality gating.

Quad-dominant surface meshes are generated through the Gmsh Python API using
the OCC kernel. Three entry points exist:

* :func:`mesh_parametric_can` - meshes a code-generated cylindrical shell.
* :func:`mesh_step_surfaces` - meshes the surfaces of an imported STEP file.
* :func:`mesh_tool` - meshes the rigid reference tool (platen / V-block /
  indenter / real-shape jig).

Every entry point runs the spec §7 mesh gate and, on failure, automatically
remeshes with a smaller target size up to
:data:`crushsim.units.MESH_REMESH_MAX_ATTEMPTS` times before raising
:class:`~crushsim.errors.GateFailure` (spec §4 FR-03, ADR-06).

Gmsh is a global-state library: every public function here owns its session
through :func:`gmsh_session` and never leaves a model open.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import GateFailure, MeshingError
from ..geometry.parametric import BoxCan, CanShell, ToolShape
from ..units import (
    MESH_MIN_EDGE_LENGTH_MM,
    MESH_REMESH_MAX_ATTEMPTS,
    MESH_REMESH_SHRINK_FACTOR,
    MESH_TARGET_SIZE_DEFAULT_MM,
)
from .gates import GateResult, evaluate_mesh_gate
from .mesh_data import ShellMesh

_GMSH_TRIANGLE: int = 2
"""Gmsh element type id of the 3-node triangle."""

_GMSH_QUAD: int = 3
"""Gmsh element type id of the 4-node quadrangle."""

STEP_HEAL_TOLERANCE_MM: float = 0.5
"""OCC shape-healing tolerance for imported CAD: sliver faces and edges below
this size are sewn/merged before meshing. Real CATIA exports carry sub-0.1 mm
fillet bands that otherwise force sliver elements (min SICN ~0.02)."""

STEP_MESH_SMOOTHING_STEPS: int = 25
"""Laplacian smoothing passes for meshes on imported (healed) CAD faces."""

STEP_STRIP_WIDTH_LIMIT_MM: float = 2.0 * MESH_MIN_EDGE_LENGTH_MM
"""Faces narrower than this cannot hold an element that clears the §7 minimum
edge length, so they are defeatured (removed and the shell re-sewn). Real CATIA
can exports carry ~0.4 mm flat rim strips that otherwise pin the whole mesh
below the gate."""

STEP_STRIP_AREA_FRACTION_MAX: float = 0.1
"""Defeaturing never removes more than this fraction of the total surface area:
a shape made mostly of narrow faces is meshed as-is and judged by the gate."""

STEP_SHORT_CURVE_TARGET_FACTOR: float = 2.0
"""Boundary curves shorter than this multiple of the target size are meshed as
a single element edge. Subdividing a sub-target corner arc squeezes sliver
quads into the adjacent faces (measured: min SICN 0.06 -> 0.31 on a CATIA can
export)."""

STEP_BAND_SIDE_RATIO_MIN: float = 1.8
"""A four-sided face counts as a narrow fillet band when its long sides are at
least this multiple of its short sides (and the short sides are opposite)."""


@dataclass(frozen=True, slots=True)
class MeshQuality:
    """Element-quality statistics of one shell mesh."""

    min_sicn: float
    mean_sicn: float
    max_aspect_ratio: float
    min_edge_length: float
    max_edge_length: float
    triangle_fraction: float
    n_elements: int
    n_quads: int
    n_tris: int
    worst_elements: list[dict[str, float]] = field(default_factory=list)
    """The lowest-SICN elements with their centroids, for the defect report."""

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "min_sicn": self.min_sicn,
            "mean_sicn": self.mean_sicn,
            "max_aspect_ratio": self.max_aspect_ratio,
            "min_edge_length_mm": self.min_edge_length,
            "max_edge_length_mm": self.max_edge_length,
            "triangle_fraction": self.triangle_fraction,
            "elements": self.n_elements,
            "quads": self.n_quads,
            "tris": self.n_tris,
            "worst_elements": list(self.worst_elements),
        }


@dataclass(slots=True)
class MeshResult:
    """A meshed part together with its quality verdict."""

    mesh: ShellMesh
    quality: MeshQuality
    gate: GateResult
    target_size: float
    attempts: int
    msh_path: Path | None = None

    @property
    def passed(self) -> bool:
        """Whether the mesh gate passed."""
        return self.gate.passed

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for run summaries and reports."""
        return {
            "mesh": self.mesh.summary(),
            "quality": self.quality.to_dict(),
            "gate": self.gate.to_dict(),
            "target_size_mm": self.target_size,
            "attempts": self.attempts,
            "msh_path": str(self.msh_path) if self.msh_path else None,
        }


@contextlib.contextmanager
def gmsh_session(*, verbosity: int = 0) -> Iterator[Any]:
    """Own one Gmsh session and guarantee it is finalised.

    Yields:
        The imported :mod:`gmsh` module with a fresh, empty model.

    Raises:
        MeshingError: If Gmsh cannot be imported (a hard dependency, so this
            means a broken installation rather than a missing extra).
    """
    try:
        import gmsh  # noqa: PLC0415 - imported lazily to keep CLI start-up fast
    except ImportError as exc:  # pragma: no cover - hard dependency
        raise MeshingError(
            "Gmsh Python API is not importable. Reinstall with 'pip install gmsh'. "
            "On headless Linux the shared libraries libGLU.so.1 and libXft.so.2 "
            "must also be present."
        ) from exc

    already = bool(gmsh.isInitialized())
    if not already:
        gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.Verbosity", verbosity)
        gmsh.clear()
        yield gmsh
    finally:
        with contextlib.suppress(Exception):
            gmsh.clear()
        if not already:
            gmsh.finalize()


def _configure_mesh_options(
    gmsh: Any,
    *,
    target_size: float,
    min_size: float | None,
    max_size: float | None,
    recombine: bool,
    curvature_points: int,
) -> None:
    """Apply the shell-meshing options shared by every entry point."""
    gmsh.option.setNumber("Mesh.MeshSizeMin", float(min_size if min_size else target_size * 0.5))
    gmsh.option.setNumber("Mesh.MeshSizeMax", float(max_size if max_size else target_size * 1.5))
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", float(curvature_points))
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
    # Algorithm 6 = Frontal-Delaunay; recombination 1 = Blossom.
    # Deliberate choices, both verified against the shapes this package meshes:
    #   * full-quad recombination (2/3) is rejected by Gmsh on periodic surfaces,
    #     which is exactly what a cylindrical can wall is;
    #   * algorithm 8 (Frontal-Delaunay for quads) aborts the process on seamed
    #     surfaces such as a sphere (indenter tool), so it is not used.
    # Frontal-Delaunay + Blossom yields an all-quad mesh on the can wall.
    gmsh.option.setNumber("Mesh.Algorithm", 6)
    gmsh.option.setNumber("Mesh.RecombineAll", 1 if recombine else 0)
    gmsh.option.setNumber("Mesh.RecombinationAlgorithm", 1 if recombine else 0)
    gmsh.option.setNumber("Mesh.RecombineOptimizeTopology", 5)
    gmsh.option.setNumber("Mesh.ElementOrder", 1)


def _extract_shell_mesh(gmsh: Any, *, name: str, source: str) -> ShellMesh:
    """Pull the 2-D mesh out of the current Gmsh model as a :class:`ShellMesh`.

    Node tags are compacted to a contiguous 1-based range so the deck writer can
    emit them directly.

    Raises:
        MeshingError: If the model contains no surface elements.
    """
    node_tags, coords, _ = gmsh.model.mesh.getNodes()
    if node_tags.size == 0:
        raise MeshingError(f"Gmsh produced no nodes for {name!r}")
    tags = np.asarray(node_tags, dtype=np.int64)
    xyz = np.asarray(coords, dtype=float).reshape(-1, 3)
    order = np.argsort(tags)
    tags = tags[order]
    xyz = xyz[order]
    compact = {int(t): i + 1 for i, t in enumerate(tags)}

    elem_types, _elem_tags, elem_nodes = gmsh.model.mesh.getElements(2)
    quads: np.ndarray = np.zeros((0, 4), dtype=np.int64)
    tris: np.ndarray = np.zeros((0, 3), dtype=np.int64)
    for etype, enodes in zip(elem_types, elem_nodes):
        connectivity = np.asarray(enodes, dtype=np.int64)
        if int(etype) == _GMSH_QUAD:
            block = connectivity.reshape(-1, 4)
            quads = np.vectorize(compact.__getitem__, otypes=[np.int64])(block)
        elif int(etype) == _GMSH_TRIANGLE:
            block = connectivity.reshape(-1, 3)
            tris = np.vectorize(compact.__getitem__, otypes=[np.int64])(block)

    if quads.shape[0] + tris.shape[0] == 0:
        raise MeshingError(
            f"Gmsh produced no shell elements for {name!r}. "
            "Check that the geometry contains meshable surfaces."
        )
    return ShellMesh(
        node_ids=np.arange(1, tags.size + 1, dtype=np.int64),
        nodes=xyz,
        quads=quads,
        tris=tris,
        name=name,
        source=source,
    )


def _compute_quality(gmsh: Any, mesh: ShellMesh, *, worst_n: int = 10) -> MeshQuality:
    """Compute SICN / aspect ratio / edge-length statistics of the current model.

    Raises:
        MeshingError: If Gmsh reports no elements to evaluate.
    """
    elem_types, elem_tags, _ = gmsh.model.mesh.getElements(2)
    tags: list[int] = []
    for etype, block in zip(elem_types, elem_tags):
        if int(etype) in (_GMSH_QUAD, _GMSH_TRIANGLE):
            tags.extend(int(t) for t in block)
    if not tags:
        raise MeshingError("No surface elements available for quality evaluation")

    sicn = np.asarray(gmsh.model.mesh.getElementQualities(tags, "minSICN"), dtype=float)
    min_edge = np.asarray(gmsh.model.mesh.getElementQualities(tags, "minEdge"), dtype=float)
    max_edge = np.asarray(gmsh.model.mesh.getElementQualities(tags, "maxEdge"), dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        aspect = np.where(min_edge > 0.0, max_edge / min_edge, np.inf)

    worst_order = np.argsort(sicn)[:worst_n]
    worst: list[dict[str, float]] = []
    for idx in worst_order:
        tag = tags[int(idx)]
        try:
            _etype, enodes, _dim, _tag = gmsh.model.mesh.getElement(tag)
            coords = np.array(
                [gmsh.model.mesh.getNode(int(n))[0] for n in enodes], dtype=float
            ).reshape(-1, 3)
            centroid = coords.mean(axis=0)
        except Exception:  # pragma: no cover - defensive; quality report only
            centroid = np.array([math.nan, math.nan, math.nan])
        worst.append(
            {
                "element_tag": float(tag),
                "sicn": float(sicn[int(idx)]),
                "aspect_ratio": float(aspect[int(idx)]),
                "min_edge_mm": float(min_edge[int(idx)]),
                "x": float(centroid[0]),
                "y": float(centroid[1]),
                "z": float(centroid[2]),
            }
        )

    return MeshQuality(
        min_sicn=float(np.min(sicn)),
        mean_sicn=float(np.mean(sicn)),
        max_aspect_ratio=float(np.max(aspect)),
        min_edge_length=float(np.min(min_edge)),
        max_edge_length=float(np.max(max_edge)),
        triangle_fraction=mesh.triangle_fraction,
        n_elements=mesh.n_elements,
        n_quads=mesh.n_quads,
        n_tris=mesh.n_tris,
        worst_elements=worst,
    )


# ---------------------------------------------------------------------------
# Geometry builders (OCC kernel)
# ---------------------------------------------------------------------------


def _build_can_surfaces(gmsh: Any, can: CanShell, target_size: float) -> list[int]:
    """Build the can mid-surface in the current model and return surface tags.

    The lateral wall is created by extruding two half-circle arcs (edges),
    which yields pure surfaces - never a solid - matching the FR-02 "outer
    skin only" rule. Splitting the wall in two makes each half a four-sided
    face that is meshed as a structured quad grid: unstructured recombination
    near the extrusion seam produces borderline quads (measured: min SICN
    0.25-0.36 at a 1 mm target, gate-flaky across platforms), while the
    structured grid is deterministic with min SICN > 0.9.
    """
    occ = gmsh.model.occ
    r = can.mid_surface_radius
    arcs = [
        occ.addCircle(0.0, 0.0, 0.0, r, angle1=0.0, angle2=math.pi),
        occ.addCircle(0.0, 0.0, 0.0, r, angle1=math.pi, angle2=2.0 * math.pi),
    ]
    extruded = occ.extrude([(1, a) for a in arcs], 0.0, 0.0, can.height)
    walls = [tag for dim, tag in extruded if dim == 2]
    surfaces = list(walls)
    if can.closed_bottom:
        loop_b = occ.addCurveLoop(arcs)
        surfaces.append(occ.addPlaneSurface([loop_b]))
    if can.closed_top:
        top_arcs = [
            occ.addCircle(0.0, 0.0, can.height, r, angle1=0.0, angle2=math.pi),
            occ.addCircle(0.0, 0.0, can.height, r, angle1=math.pi, angle2=2.0 * math.pi),
        ]
        loop_t = occ.addCurveLoop(top_arcs)
        surfaces.append(occ.addPlaneSurface([loop_t]))
    # Fragmenting is mandatory even without caps: the two extruded halves
    # carry coincident-but-distinct seam edges, and meshing them unsewn
    # duplicates every seam node - the §7 gate cannot see that (0 non-manifold
    # edges), but the TYPE7 self-contact then collapses on the zero-distance
    # node pairs (negative interface timestep, measured on the B-3 bench).
    if len(surfaces) > 1:
        fused, _ = occ.fragment([(2, surfaces[0])], [(2, s) for s in surfaces[1:]])
        surfaces = [tag for dim, tag in fused if dim == 2]
    occ.synchronize()
    if not surfaces:
        raise MeshingError("Failed to build the can mid-surface with the Gmsh OCC kernel")

    # Structured grid on the wall halves: arc and seam divisions at the target
    # size. The caps (when closed) stay unstructured but switch to the plain
    # Delaunay algorithm: with the wall arcs' node spacing imposed on the disc
    # rim, Frontal-Delaunay recombines a rim sliver (min SICN 0.06) where
    # Delaunay recombines cleanly (0.60). Faces are told apart geometrically
    # because fragmenting reassigns tags; OCC pads bounding boxes by ~1e-7,
    # so "flat" needs a loose test.
    n_arc = max(2, int(round(math.pi * r / target_size)) + 1)
    n_height = max(2, int(round(can.height / target_size)) + 1)
    for _dim, face in gmsh.model.getEntities(2):
        bb = gmsh.model.getBoundingBox(2, face)
        if abs(bb[5] - bb[2]) < 1e-3:  # flat cap [mm]
            gmsh.model.mesh.setAlgorithm(2, face, 5)
            continue
        curves = gmsh.model.getBoundary([(2, face)], oriented=False)
        if len(curves) != 4:
            continue  # fragmented into something unexpected; mesh unstructured
        for _cdim, ctag in curves:
            length = float(occ.getMass(1, abs(ctag)))
            is_arc = abs(length - math.pi * r) < abs(length - can.height)
            gmsh.model.mesh.setTransfiniteCurve(abs(ctag), n_arc if is_arc else n_height)
        gmsh.model.mesh.setTransfiniteSurface(face)
        gmsh.model.mesh.setRecombine(2, face)
    return surfaces


def _orthonormal_frame(
    normal: tuple[float, float, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(n, u, v)`` - a right-handed orthonormal frame around ``normal``."""
    n = np.asarray(normal, dtype=float)
    norm = float(np.linalg.norm(n))
    if norm <= 0.0:
        raise MeshingError("Cannot build a frame around a zero normal vector")
    n = n / norm
    helper = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(n, helper)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return n, u, v


def _add_plate(
    gmsh: Any,
    center: np.ndarray,
    normal: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    half_u: float,
    half_v: float,
) -> int:
    """Add a rectangular plane surface centred at ``center`` with the given frame."""
    occ = gmsh.model.occ
    corners = [
        center - half_u * u - half_v * v,
        center + half_u * u - half_v * v,
        center + half_u * u + half_v * v,
        center - half_u * u + half_v * v,
    ]
    points = [occ.addPoint(float(c[0]), float(c[1]), float(c[2])) for c in corners]
    lines = [occ.addLine(points[i], points[(i + 1) % 4]) for i in range(4)]
    loop = occ.addCurveLoop(lines)
    return occ.addPlaneSurface([loop])


def _structure_plate_faces(gmsh: Any, surfaces: list[int], target_size: float) -> None:
    """Mesh rectangular plane faces as structured quad grids.

    A rigid plate needs no particular mesh for the physics (/RBODY), but the
    unstructured mesher's fronts meet mid-plate and draw seam lines across
    every render; a transfinite grid keeps the wireframe (and the contact
    segments) uniform. Non-planar or non-four-sided faces are left alone.
    """
    occ = gmsh.model.occ
    for face in surfaces:
        if gmsh.model.getType(2, face) != "Plane":
            continue
        curves = gmsh.model.getBoundary([(2, face)], oriented=False)
        if len(curves) != 4:
            continue
        for _cdim, ctag in curves:
            length = float(occ.getMass(1, abs(ctag)))
            gmsh.model.mesh.setTransfiniteCurve(
                abs(ctag), max(2, int(round(length / target_size)) + 1)
            )
        gmsh.model.mesh.setTransfiniteSurface(face)
        gmsh.model.mesh.setRecombine(2, face)


def _build_tool_surfaces(
    gmsh: Any, tool: ToolShape, *, can_height: float, target_size: float
) -> list[int]:
    """Build the reference-tool surface(s) in the current model.

    Raises:
        MeshingError: If the tool kind has no parametric builder (``step`` tools
            are meshed by :func:`mesh_step_surfaces` instead).
    """
    occ = gmsh.model.occ
    origin = np.asarray(tool.origin, dtype=float)
    d = np.asarray(tool.unit_direction, dtype=float)

    if tool.kind in ("platen", "jig_plane"):
        n, u, v = _orthonormal_frame(tuple(d))
        surfaces = [_add_plate(gmsh, origin, n, u, v, tool.size * 0.5, tool.size * 0.5)]
    elif tool.kind == "v_block":
        z = np.array([0.0, 0.0, 1.0])
        surfaces = []
        for angle in (math.radians(45.0), math.radians(-45.0)):
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            n_i = np.array([cos_a * d[0] - sin_a * d[1], sin_a * d[0] + cos_a * d[1], 0.0])
            n_i /= np.linalg.norm(n_i)
            w_i = np.cross(z, n_i)
            if float(np.dot(w_i, -d)) < 0.0:
                w_i = -w_i
            length = tool.size * 0.5
            center = origin + 0.5 * length * w_i
            surfaces.append(
                _add_plate(gmsh, center, n_i, w_i, z, length * 0.5, can_height * 0.5)
            )
    elif tool.kind == "indenter":
        radius = tool.radius if tool.radius > 0.0 else tool.size * 0.25
        centre = origin - d * radius
        sphere = occ.addSphere(float(centre[0]), float(centre[1]), float(centre[2]), radius)
        occ.synchronize()
        # The solid is left in the model but never meshed: only 2-D elements are
        # generated, so the rigid tool stays a shell (removing the volume through
        # the OCC kernel after synchronisation is unsafe).
        surfaces = [
            tag for dim, tag in gmsh.model.getBoundary([(3, sphere)], oriented=False) if dim == 2
        ]
    elif tool.kind == "cylinder":
        # Horizontal pipe roller: axis perpendicular to the drive direction and
        # level with the ground, length = tool.size, radius = tool.radius.
        radius = tool.radius if tool.radius > 0.0 else tool.size * 0.25
        z = np.array([0.0, 0.0, 1.0])
        axis = np.cross(z, d)
        axis_norm = float(np.linalg.norm(axis))
        if axis_norm < 1e-9:  # axial drive: any horizontal axis works
            axis = np.array([1.0, 0.0, 0.0])
        else:
            axis /= axis_norm
        centre = origin - d * radius
        start = centre - axis * (tool.size * 0.5)
        span = axis * tool.size
        pipe = occ.addCylinder(
            float(start[0]), float(start[1]), float(start[2]),
            float(span[0]), float(span[1]), float(span[2]),
            radius,
        )
        occ.synchronize()
        # Same rule as the indenter: the solid stays, only its skin is meshed.
        surfaces = [
            tag for dim, tag in gmsh.model.getBoundary([(3, pipe)], oriented=False) if dim == 2
        ]
    elif tool.kind == "bead_roller":
        # Beading roller: a torus with a vertical axis (parallel to the can
        # axis). The rounded rim (minor radius = tool.radius) forms the
        # groove profile; tool.size is the roller's outer diameter. As with
        # every builder, the nearest feature - the outer rim - sits at the
        # tool origin.
        r_minor = tool.radius if tool.radius > 0.0 else tool.size * 0.15
        r_major = tool.size * 0.5 - r_minor
        if r_major <= 0.0:
            raise MeshingError(
                f"bead_roller size {tool.size} mm too small for rim radius {r_minor} mm"
            )
        centre = origin - d * (r_major + r_minor)
        torus = occ.addTorus(
            float(centre[0]), float(centre[1]), float(centre[2]), r_major, r_minor
        )
        occ.synchronize()
        surfaces = [
            tag for dim, tag in gmsh.model.getBoundary([(3, torus)], oriented=False) if dim == 2
        ]
    elif tool.kind == "bead_arbor":
        # Internal support arbor for rotary beading: two coaxial sleeve bands
        # (pure surfaces, like the can wall) above and below the groove
        # window. The rollers press the wall into the window while the bands
        # stop the free rim from ovalising - the production grooving-arbor
        # arrangement. tool.size is the arbor diameter; tool.radius (or a
        # 1.8 mm default) is the groove-window half-height; the origin's Z
        # is the groove centre.
        r_arb = tool.size * 0.5
        window = tool.radius if tool.radius > 0.0 else 1.8
        band = 3.0
        z0 = float(origin[2])
        surfaces = []
        for za, zb in ((z0 - window - band, z0 - window), (z0 + window, z0 + window + band)):
            arcs = [
                occ.addCircle(0.0, 0.0, za, r_arb, angle1=0.0, angle2=math.pi),
                occ.addCircle(0.0, 0.0, za, r_arb, angle1=math.pi, angle2=2.0 * math.pi),
            ]
            extruded = occ.extrude([(1, a) for a in arcs], 0.0, 0.0, zb - za)
            surfaces += [tag for dim, tag in extruded if dim == 2]
        occ.synchronize()
    else:
        raise MeshingError(
            f"Tool kind {tool.kind!r} has no parametric builder; "
            "use a STEP file (loading.tool: step) instead."
        )
    occ.synchronize()
    if not surfaces:
        raise MeshingError(f"Failed to build tool surfaces for kind {tool.kind!r}")
    _structure_plate_faces(gmsh, surfaces, target_size)
    return surfaces


# ---------------------------------------------------------------------------
# Defeaturing of imported CAD (FR-03: the gate judges the mesh, so features
# that can never mesh above the gate are removed or constrained up front)
# ---------------------------------------------------------------------------


def _face_width_proxy(gmsh: Any, face_tag: int) -> float:
    """Estimate the width of a face as ``2 * area / perimeter``.

    Exact for long rectangular strips, conservative (over-estimating) for
    everything else, so it only ever flags genuinely narrow faces.
    """
    occ = gmsh.model.occ
    area = float(occ.getMass(2, face_tag))
    perimeter = sum(
        float(occ.getMass(1, abs(ct)))
        for _, ct in gmsh.model.getBoundary([(2, face_tag)], oriented=False)
    )
    return 2.0 * area / perimeter if perimeter > 0.0 else math.inf


def _defeature_strip_faces(gmsh: Any) -> int:
    """Remove faces too narrow to ever satisfy the §7 minimum edge length.

    The narrowest faces go first until :data:`STEP_STRIP_AREA_FRACTION_MAX` of
    the surface area is spent; the shell is then healed again so the sub-mm gap
    left behind is sewn shut. Returns the number of faces removed.
    """
    occ = gmsh.model.occ
    faces = gmsh.model.getEntities(2)
    if not faces:
        return 0
    areas = {tag: float(occ.getMass(2, tag)) for _, tag in faces}
    total_area = sum(areas.values())
    strips = sorted(
        (width, tag)
        for _, tag in faces
        if (width := _face_width_proxy(gmsh, tag)) < STEP_STRIP_WIDTH_LIMIT_MM
    )
    budget = STEP_STRIP_AREA_FRACTION_MAX * total_area
    chosen: list[tuple[int, int]] = []
    spent = 0.0
    for _width, tag in strips:
        if spent + areas[tag] > budget:
            break
        chosen.append((2, tag))
        spent += areas[tag]
    if not chosen:
        return 0
    occ.remove(chosen, recursive=False)
    with contextlib.suppress(Exception):  # healing must never block the import
        occ.healShapes(
            tolerance=STEP_HEAL_TOLERANCE_MM,
            fixDegenerated=True,
            fixSmallEdges=True,
            fixSmallFaces=True,
            sewFaces=True,
            makeSolids=False,
        )
    occ.synchronize()
    return len(chosen)


def _constrain_micro_features(gmsh: Any, target_size: float) -> None:
    """Mesh narrow fillet bands as structured ladders and sub-target curves as
    single element edges.

    Both constraints keep element edges at feature size instead of letting the
    mesher subdivide features smaller than the target: unconstrained, a CATIA
    can export meshes its 1.6 mm rim fillets into 0.2 mm slivers.
    """
    occ = gmsh.model.occ
    short_limit = STEP_SHORT_CURVE_TARGET_FACTOR * target_size

    # Structured ladders over narrow four-sided bands. A curve is committed to
    # one node count only once; a band whose curve is already claimed with a
    # different count is skipped so transfinite constraints stay consistent.
    committed: dict[int, int] = {}
    for _dim, face in gmsh.model.getEntities(2):
        curves = gmsh.model.getBoundary([(2, face)], oriented=False)
        if len(curves) != 4:
            continue
        lengths = sorted((float(occ.getMass(1, abs(ct))), abs(ct)) for _, ct in curves)
        short_pair, long_pair = lengths[:2], lengths[2:]
        if short_pair[1][0] >= short_limit:
            continue
        if long_pair[0][0] < STEP_BAND_SIDE_RATIO_MIN * short_pair[1][0]:
            continue
        endpoints = [
            {pt for _, pt in gmsh.model.getBoundary([(1, ct)], oriented=False)}
            for _, ct in short_pair
        ]
        if not all(endpoints) or endpoints[0] & endpoints[1]:
            continue  # closed or adjacent short sides: not a ladder band
        rungs = max(2, int(round(long_pair[1][0] / target_size)) + 1)
        wanted = dict.fromkeys((ct for _, ct in short_pair), 2)
        wanted.update(dict.fromkeys((ct for _, ct in long_pair), rungs))
        if any(committed.get(ct, n) != n for ct, n in wanted.items()):
            continue
        for ct, n in wanted.items():
            gmsh.model.mesh.setTransfiniteCurve(ct, n)
            committed[ct] = n
        gmsh.model.mesh.setTransfiniteSurface(face)
        gmsh.model.mesh.setRecombine(2, face)

    # Any remaining sub-target open curve becomes a single element edge.
    for _dim, ct in gmsh.model.getEntities(1):
        if ct in committed:
            continue
        length = float(occ.getMass(1, ct))
        if not 0.0 < length < short_limit:
            continue
        if not gmsh.model.getBoundary([(1, ct)], oriented=False):
            continue  # closed curve: one edge cannot form a loop
        gmsh.model.mesh.setTransfiniteCurve(ct, 2)


# ---------------------------------------------------------------------------
# Public meshing entry points
# ---------------------------------------------------------------------------


def _mesh_once(
    build: Any,
    *,
    name: str,
    source: str,
    target_size: float,
    min_size: float | None,
    max_size: float | None,
    recombine: bool,
    curvature_points: int,
    out_path: Path | None,
    finish: Any | None = None,
    option_overrides: dict[str, float] | None = None,
) -> tuple[ShellMesh, MeshQuality]:
    """Run one build-and-mesh pass and return the mesh with its quality stats."""
    with gmsh_session() as gmsh:
        gmsh.model.add(name)
        build(gmsh, target_size)
        _configure_mesh_options(
            gmsh,
            target_size=target_size,
            min_size=min_size,
            max_size=max_size,
            recombine=recombine,
            curvature_points=curvature_points,
        )
        for option, value in (option_overrides or {}).items():
            gmsh.option.setNumber(option, float(value))
        gmsh.option.setNumber("Mesh.MeshSizeFactor", 1.0)
        gmsh.model.mesh.setSize(gmsh.model.getEntities(0), float(target_size))
        try:
            gmsh.model.mesh.generate(2)
        except Exception as exc:  # pragma: no cover - gmsh internal failure
            raise MeshingError(f"Gmsh failed to mesh {name!r}: {exc}") from exc
        if finish is not None:
            finish(gmsh)
        mesh = _extract_shell_mesh(gmsh, name=name, source=source)
        quality = _compute_quality(gmsh, mesh)
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            gmsh.write(str(out_path))
    return mesh, quality


def _mesh_with_gate(
    build: Any,
    *,
    name: str,
    source: str,
    target_size: float,
    min_size: float | None = None,
    max_size: float | None = None,
    recombine: bool = True,
    curvature_points: int = 12,
    out_path: str | Path | None = None,
    max_attempts: int = MESH_REMESH_MAX_ATTEMPTS,
    enforce: bool = True,
    finish: Any | None = None,
    option_overrides: dict[str, float] | None = None,
) -> MeshResult:
    """Mesh, gate, and automatically remesh with a smaller target on failure.

    Raises:
        GateFailure: If ``enforce`` and the gate still fails after
            ``max_attempts`` attempts (ADR-06).
    """
    path = Path(out_path) if out_path is not None else None
    size = float(target_size)
    last: MeshResult | None = None

    for attempt in range(1, max(1, int(max_attempts)) + 1):
        mesh, quality = _mesh_once(
            build,
            name=name,
            source=source,
            target_size=size,
            min_size=min_size,
            max_size=max_size,
            recombine=recombine,
            curvature_points=curvature_points,
            out_path=path,
            finish=finish,
            option_overrides=option_overrides,
        )
        gate = evaluate_mesh_gate(
            min_sicn=quality.min_sicn,
            max_aspect_ratio=quality.max_aspect_ratio,
            min_edge_length=quality.min_edge_length,
            triangle_fraction=quality.triangle_fraction,
            non_manifold_edges=mesh.non_manifold_edge_count(),
            info={
                "part": name,
                "source": source,
                "target_size_mm": size,
                "attempt": attempt,
                "free_edges": mesh.free_edge_count(),
                "nodes": mesh.n_nodes,
                "elements": mesh.n_elements,
            },
        )
        last = MeshResult(
            mesh=mesh,
            quality=quality,
            gate=gate,
            target_size=size,
            attempts=attempt,
            msh_path=path,
        )
        if gate.passed:
            return last
        size *= MESH_REMESH_SHRINK_FACTOR

    assert last is not None  # loop runs at least once
    if enforce:
        raise GateFailure(
            f"Mesh gate failed for {name!r} after {last.attempts} attempt(s).\n"
            f"{last.gate.describe()}\n"
            f"Worst elements: {last.quality.worst_elements[:3]}"
        )
    return last


def _seed_imperfection(mesh: ShellMesh, can: CanShell, amplitude: float) -> None:
    """Perturb the wall nodes radially with a deterministic multi-mode field.

    A geometrically perfect cylinder's first buckling peak is pathologically
    mesh-sensitive (imperfection sensitivity): every refinement finds a higher
    buckling mode and the peak keeps dropping - measured on the B-3 sweep as
    1258 -> 940 -> 834 N across 2.0/1.0/0.5 mm. Seeding a small deterministic
    imperfection triggers the same physical mode on every mesh, which is what
    lets the peak converge; real cans carry far larger forming imperfections.

    The axial content is set by the fold wavelength ``~4*sqrt(R*t)`` and is
    non-zero at the can ends - the first buckle forms at the loaded rim, so
    an envelope vanishing there (the first attempt used a half-sine) leaves
    the peak untouched (measured: 943 vs 940 N). A light circumferential
    modulation breaks the axial symmetry. Modifies ``mesh.nodes`` in place.
    """
    xyz = mesh.nodes
    r = np.hypot(xyz[:, 0], xyz[:, 1])
    theta = np.arctan2(xyz[:, 1], xyz[:, 0])
    z = xyz[:, 2]
    wavelength = max(4.0 * math.sqrt(max(can.radius * can.thickness, 1e-12)), 1e-6)
    axial = np.cos(2.0 * np.pi * z / wavelength)
    axial_slow = np.cos(2.0 * np.pi * z / (3.0 * wavelength))
    dr = amplitude * (0.7 * axial + 0.3 * np.cos(4.0 * theta + 0.5) * axial_slow)
    scale = np.where(r > 1e-9, (r + dr) / np.maximum(r, 1e-9), 1.0)
    xyz[:, 0] *= scale
    xyz[:, 1] *= scale
    mesh.metadata["imperfection_mm"] = float(amplitude)


def _stadium_outline(
    occ: Any, length: float, width: float, z: float
) -> tuple[list[int], list[int]]:
    """Stadium outline in the z-plane: (junction point tags, curve tags).

    Each 180-degree end cap is built as TWO 90-degree arcs through an
    explicit apex point: a half-circle between two points about a centre is
    ambiguous in the OCC kernel and the wrong pick self-intersects the
    outline (measured: gmsh loops forever splitting 1D intersections).
    Six junctions and six curves, in loop order: top line, upper-left arc,
    lower-left arc, bottom line, lower-right arc, upper-right arc.
    """
    r = width / 2.0
    c = max(length / 2.0 - r, 1e-9)
    p1 = occ.addPoint(c, r, z)
    p2 = occ.addPoint(-c, r, z)
    pml = occ.addPoint(-c - r, 0.0, z)
    p3 = occ.addPoint(-c, -r, z)
    p4 = occ.addPoint(c, -r, z)
    pmr = occ.addPoint(c + r, 0.0, z)
    e1 = occ.addPoint(c, 0.0, z)
    e2 = occ.addPoint(-c, 0.0, z)
    points = [p1, p2, pml, p3, p4, pmr]
    curves = [
        occ.addLine(p1, p2),
        occ.addCircleArc(p2, e2, pml),
        occ.addCircleArc(pml, e2, p3),
        occ.addLine(p3, p4),
        occ.addCircleArc(p4, e1, pmr),
        occ.addCircleArc(pmr, e1, p1),
    ]
    return points, curves


def _build_box_surfaces(gmsh: Any, box: BoxCan, target_size: float) -> None:
    """Build the box-can mid-surface (walls + caps) with a structured score band.

    Walls and the bottom are structured quad grids. The scored cap is built
    from explicit faces: the inner flap, a four-piece structured band between
    the two stadium outlines (clean quads, two elements across the band), and
    the cap remainder with the stadium as a hole - so the thin score band is
    made of well-shaped aligned quads instead of fragmented slivers.
    """
    occ = gmsh.model.occ
    a, b, h = box.half_width_mid, box.half_depth_mid, box.height
    ex = np.array([1.0, 0.0, 0.0])
    ey = np.array([0.0, 1.0, 0.0])
    ez = np.array([0.0, 0.0, 1.0])
    faces = [
        _add_plate(gmsh, np.array([a, 0.0, h / 2.0]), ex, ey, ez, b, h / 2.0),
        _add_plate(gmsh, np.array([-a, 0.0, h / 2.0]), ex, ey, ez, b, h / 2.0),
        _add_plate(gmsh, np.array([0.0, b, h / 2.0]), ey, ex, ez, a, h / 2.0),
        _add_plate(gmsh, np.array([0.0, -b, h / 2.0]), ey, ex, ez, a, h / 2.0),
    ]
    if box.closed_bottom:
        faces.append(_add_plate(gmsh, np.array([0.0, 0.0, 0.0]), ez, ex, ey, a, b))

    band_faces: list[int] = []
    score_curves: list[int] = []
    if box.closed_top and box.vent is not None:
        vent = box.vent
        pts_i, cur_i = _stadium_outline(occ, vent.length - vent.band, vent.width - vent.band, h)
        pts_o, cur_o = _stadium_outline(occ, vent.length + vent.band, vent.width + vent.band, h)
        radials = [occ.addLine(pi, po) for pi, po in zip(pts_i, pts_o)]
        # Six 4-sided band pieces: inner curve, radial, outer curve, radial.
        for k in range(6):
            loop = occ.addCurveLoop(
                [cur_i[k], radials[(k + 1) % 6], -cur_o[k], -radials[k]]
            )
            band_faces.append(occ.addPlaneSurface([loop]))
        flap = occ.addPlaneSurface([occ.addCurveLoop(cur_i)])
        # petal_x: imprint the score arcs into the flap, so mesh edges align
        # with the engraved pattern and the tear follows the real geometry
        # instead of a centroid-tagged jagged band.
        if vent.pattern == "petal_x":
            for arm in vent.petal_arms():
                tags = [occ.addPoint(px, py, h) for px, py in arm]
                score_curves.append(occ.addSpline(tags))
        # Cap remainder: rectangle with the outer stadium as a hole.
        c1 = occ.addPoint(a, b, h)
        c2 = occ.addPoint(-a, b, h)
        c3 = occ.addPoint(-a, -b, h)
        c4 = occ.addPoint(a, -b, h)
        rect = occ.addCurveLoop(
            [occ.addLine(c1, c2), occ.addLine(c2, c3), occ.addLine(c3, c4), occ.addLine(c4, c1)]
        )
        remainder = occ.addPlaneSurface([rect, occ.addCurveLoop(cur_o)])
        cap_faces = [flap, remainder]
        faces += cap_faces + band_faces
        occ.synchronize()
    elif box.closed_top:
        faces.append(_add_plate(gmsh, np.array([0.0, 0.0, h]), ez, ex, ey, a, b))

    occ.fragment(
        [(2, faces[0])],
        [(2, f) for f in faces[1:]] + [(1, c) for c in score_curves],
    )
    occ.synchronize()

    # Refine along the imprinted score arcs so the score band resolves: every
    # curve on the cap plane strictly inside the inner stadium is (a piece
    # of) a score arc after retagging - identified geometrically, like the
    # faces below.
    if score_curves and box.vent is not None:
        score_size = min(target_size, max(box.vent.band, 0.4))
        for _dim, ctag in gmsh.model.getEntities(1):
            bb = gmsh.model.getBoundingBox(1, ctag)
            if abs(bb[2] - h) > 1e-3 or abs(bb[5] - h) > 1e-3:
                continue
            cx = (bb[0] + bb[3]) / 2.0
            cy = (bb[1] + bb[4]) / 2.0
            if box.vent.contains(cx, cy, grow=-box.vent.band):
                points = gmsh.model.getBoundary([(1, ctag)], oriented=False, recursive=True)
                gmsh.model.mesh.setSize(points, score_size)

    # All meshing constraints are applied AFTER the fragment (which retags
    # entities): faces are re-identified geometrically. Cap faces are told
    # apart by their centroid against the two stadium outlines; the band
    # pieces get matched structured divisions (~square quads, two elements
    # across the band), flap and remainder mesh with plain Delaunay.
    vent = box.vent
    band_len = max(vent.band, 1e-6) if vent else target_size
    for _dim, face in gmsh.model.getEntities(2):
        bb = gmsh.model.getBoundingBox(2, face)
        on_cap = abs(bb[5] - bb[2]) < 1e-3 and abs(bb[2] - h) < 1e-3
        boundary = gmsh.model.getBoundary([(2, face)], oriented=False)
        if on_cap and vent is not None:
            cx, cy, _cz = occ.getCenterOfMass(2, face)
            in_outer = vent.contains(cx, cy, grow=vent.band)
            in_inner = vent.contains(cx, cy, grow=-vent.band)
            if in_outer and not in_inner and len(boundary) == 4:
                for _cdim, ctag in boundary:
                    length = float(occ.getMass(1, abs(ctag)))
                    n = 3 if length < 2.0 * vent.band else max(3, int(round(length / band_len)) + 1)
                    gmsh.model.mesh.setTransfiniteCurve(abs(ctag), n)
                gmsh.model.mesh.setTransfiniteSurface(face)
                gmsh.model.mesh.setRecombine(2, face)
            else:
                gmsh.model.mesh.setAlgorithm(2, face, 5)
            continue
        if len(boundary) != 4:
            continue
        for _cdim, ctag in boundary:
            length = float(occ.getMass(1, abs(ctag)))
            n = max(2, int(round(length / target_size)) + 1)
            gmsh.model.mesh.setTransfiniteCurve(abs(ctag), n)
        gmsh.model.mesh.setTransfiniteSurface(face)
        gmsh.model.mesh.setRecombine(2, face)


def mesh_box_can(
    box: BoxCan,
    *,
    target_size: float = MESH_TARGET_SIZE_DEFAULT_MM,
    min_size: float | None = None,
    max_size: float | None = None,
    recombine: bool = True,
    curvature_points: int = 12,
    out_path: str | Path | None = None,
    max_attempts: int = MESH_REMESH_MAX_ATTEMPTS,
    enforce: bool = True,
    name: str = "can",
) -> MeshResult:
    """Mesh a prismatic (box) can shell; the scored vent band gets its own
    per-element thickness (:attr:`ShellMesh.element_thickness`).

    Raises:
        GateFailure: If the mesh gate fails and ``enforce`` is True.
        MeshingError: If Gmsh cannot build or mesh the geometry.
    """

    def build(gmsh: Any, size: float) -> None:
        _build_box_surfaces(gmsh, box, size)

    def finish(gmsh: Any) -> None:
        with contextlib.suppress(Exception):
            gmsh.model.mesh.removeDuplicateNodes()

    result = _mesh_with_gate(
        build,
        name=name,
        source=(
            f"box_can(W={box.width}, D={box.depth}, H={box.height}, t={box.thickness})"
        ),
        finish=finish,
        target_size=target_size,
        min_size=min_size,
        max_size=max_size,
        recombine=recombine,
        curvature_points=curvature_points,
        out_path=out_path,
        max_attempts=max_attempts,
        enforce=enforce,
    )
    if box.vent is not None and box.vent.membrane_thickness is None:
        mesh = result.mesh
        vent = box.vent
        index = mesh.node_index()
        thickness = np.full(mesh.n_quads + mesh.n_tris, box.thickness, dtype=float)
        cursor = 0
        for block in (mesh.quads, mesh.tris):
            for element in block:
                pts = mesh.nodes[[index[int(n)] for n in element]]
                cx, cy, cz = pts.mean(axis=0)
                on_cap = abs(cz - box.height) < 1e-3
                if on_cap and vent.contains(cx, cy, grow=vent.band) and not vent.contains(
                    cx, cy, grow=-vent.band
                ):
                    thickness[cursor] = vent.score_thickness
                cursor += 1
        mesh.element_thickness = thickness
        scored = int((thickness < box.thickness).sum())
        if not scored:
            raise MeshingError("Vent score band caught no elements - check the vent size")
        mesh.metadata["vent_scored_elements"] = scored
    return result


def split_vent_membrane(mesh: ShellMesh, box: BoxCan) -> tuple[ShellMesh, ShellMesh]:
    """Split a box-can mesh into (can, vent membrane) sharing one node block.

    The foil-vent construction: the stadium region of the cap (flap plus
    score band, everything inside the outer fragment outline) becomes its
    own part - thin foil, its own material - welded to the thick cap along
    that outline. Both returned meshes keep the source mesh's FULL node
    array and ids, so the deck writer can renumber them with the same
    offset and the shared boundary nodes ARE the weld (a weld is exact as
    merged nodes, like the box can's own cap).

    The membrane carries per-element thickness: ``membrane_thickness``
    everywhere, ``score_thickness`` on the score pattern - the ``petal_x``
    arms (elements whose centroid lies within ``band/2`` of an arm), or the
    legacy perimeter band.

    Raises:
        MeshingError: If the vent has no membrane_thickness, or the split
            catches no membrane or no scored elements.
    """
    vent = box.vent
    if vent is None or vent.membrane_thickness is None:
        raise MeshingError("split_vent_membrane needs a vent with membrane_thickness")
    index = mesh.node_index()
    can_quads, can_tris, mem_quads, mem_tris, mem_thk = [], [], [], [], []
    for block, keep_q in ((mesh.quads, True), (mesh.tris, False)):
        for element in block:
            pts = mesh.nodes[[index[int(n)] for n in element]]
            cx, cy, cz = pts.mean(axis=0)
            on_cap = abs(cz - box.height) < 1e-3
            if on_cap and vent.contains(cx, cy, grow=vent.band):
                (mem_quads if keep_q else mem_tris).append(element)
                if vent.pattern == "petal_x":
                    scored = vent.score_distance(cx, cy) <= vent.band / 2.0
                else:  # perimeter score on the foil
                    scored = not vent.contains(cx, cy, grow=-vent.band)
                mem_thk.append(vent.score_thickness if scored else vent.membrane_thickness)
            else:
                (can_quads if keep_q else can_tris).append(element)
    if not mem_quads and not mem_tris:
        raise MeshingError("Vent membrane split caught no elements - check the vent size")
    n_scored = sum(1 for t in mem_thk if t == vent.score_thickness)
    if not n_scored:
        raise MeshingError("Vent score pattern caught no elements - check band vs mesh size")
    # mem_thk is already quads-first: the outer loop walks quads then tris.

    def _mesh(name: str, quads: list, tris: list, thickness: list | None) -> ShellMesh:
        return ShellMesh(
            name=name,
            node_ids=mesh.node_ids.copy(),
            nodes=mesh.nodes.copy(),
            quads=np.asarray(quads, dtype=np.int64).reshape(-1, 4),
            tris=np.asarray(tris, dtype=np.int64).reshape(-1, 3),
            element_thickness=None if thickness is None else np.asarray(thickness, dtype=float),
            source=mesh.source,
            metadata=dict(mesh.metadata),
        )

    can_mesh = _mesh(mesh.name, can_quads, can_tris, None)
    membrane = _mesh("VENT_MEMBRANE", mem_quads, mem_tris, mem_thk)
    membrane.metadata["vent_scored_elements"] = n_scored
    membrane.metadata["vent_pattern"] = vent.pattern
    return can_mesh, membrane


def mesh_parametric_can(
    can: CanShell,
    *,
    target_size: float = MESH_TARGET_SIZE_DEFAULT_MM,
    min_size: float | None = None,
    max_size: float | None = None,
    recombine: bool = True,
    curvature_points: int = 12,
    imperfection_mm: float = 0.0,
    out_path: str | Path | None = None,
    max_attempts: int = MESH_REMESH_MAX_ATTEMPTS,
    enforce: bool = True,
    name: str = "can",
) -> MeshResult:
    """Mesh a parametric can shell with quad-dominant elements.

    Args:
        can: The can definition.
        target_size: Target element size [mm].
        min_size: Lower mesh-size bound [mm]; defaults to ``0.5 * target_size``.
        max_size: Upper mesh-size bound [mm]; defaults to ``1.5 * target_size``.
        recombine: Recombine triangles into quads.
        curvature_points: Elements per 2*pi of curvature.
        imperfection_mm: Radial imperfection amplitude seeded into the wall
            after meshing (see :func:`_seed_imperfection`); 0 disables it.
        out_path: Optional ``.msh`` output path.
        max_attempts: Automatic remesh attempts on gate failure.
        enforce: Raise :class:`~crushsim.errors.GateFailure` when the gate fails.
        name: Part name.

    Raises:
        GateFailure: If the mesh gate fails and ``enforce`` is True.
        MeshingError: If Gmsh cannot build or mesh the geometry.
    """

    def build(gmsh: Any, target_size: float) -> None:
        _build_can_surfaces(gmsh, can, target_size)

    def finish(gmsh: Any) -> None:
        # Fragmenting the halves and caps can leave a handful of coincident
        # vertices (measured: 5 zero-distance node pairs on a closed can's top
        # rim); merged here because duplicate nodes collapse the TYPE7
        # self-contact timestep in the solver.
        with contextlib.suppress(Exception):
            gmsh.model.mesh.removeDuplicateNodes()

    result = _mesh_with_gate(
        build,
        name=name,
        source=(
            f"parametric_can(R={can.radius}, H={can.height}, t={can.thickness})"
        ),
        finish=finish,
        target_size=target_size,
        min_size=min_size,
        max_size=max_size,
        recombine=recombine,
        curvature_points=curvature_points,
        out_path=out_path,
        max_attempts=max_attempts,
        enforce=enforce,
    )
    if imperfection_mm > 0.0:
        # Seeded after gating: the sub-element-size perturbation (~0.5x wall
        # thickness against millimetre elements) does not move the metrics.
        _seed_imperfection(result.mesh, can, float(imperfection_mm))
    return result


def mesh_step_surfaces(
    step_path: str | Path,
    *,
    target_size: float = MESH_TARGET_SIZE_DEFAULT_MM,
    min_size: float | None = None,
    max_size: float | None = None,
    recombine: bool = True,
    curvature_points: int = 12,
    out_path: str | Path | None = None,
    max_attempts: int = MESH_REMESH_MAX_ATTEMPTS,
    enforce: bool = True,
    name: str = "step_part",
) -> MeshResult:
    """Mesh the surfaces of a STEP file as shells.

    Solids in the file are replaced by their boundary faces (FR-02: the outer
    skin is meshed, the wall thickness is a shell property). Imported CAD is
    healed and then defeatured: strip faces below the §7 minimum edge length
    are removed, and sub-target fillet bands and corner arcs are meshed at
    feature size instead of being subdivided into slivers.

    Raises:
        MeshingError: If the file is missing or contains no meshable surface.
        GateFailure: If the mesh gate fails and ``enforce`` is True.
    """
    p = Path(step_path)
    if not p.is_file():
        raise MeshingError(f"STEP file not found: {p}")

    def build(gmsh: Any, target_size: float) -> None:
        occ = gmsh.model.occ
        try:
            occ.importShapes(str(p))
        except Exception as exc:
            raise MeshingError(f"Gmsh/OCC could not import STEP file {p}: {exc}") from exc
        # Real CAD exports carry sliver faces and micro-fillets that force
        # near-degenerate elements (measured: min SICN 0.02 -> 0.25 on a CATIA
        # can export). Healing is best-effort: an un-healable shape still
        # meshes and the §7 gate judges the result.
        try:
            occ.healShapes(
                tolerance=STEP_HEAL_TOLERANCE_MM,
                fixDegenerated=True,
                fixSmallEdges=True,
                fixSmallFaces=True,
                sewFaces=True,
                makeSolids=False,
            )
        except Exception:  # noqa: BLE001 - healing must never block the import
            pass
        occ.synchronize()
        # Defeature what healing keeps but the gate can never accept: sub-limit
        # strip faces are removed and the shell re-sewn, then sub-target fillet
        # bands and corner arcs are constrained to feature-sized elements
        # (measured on a CATIA can export: gate FAIL 0.02 -> PASS 0.38).
        _defeature_strip_faces(gmsh)
        _constrain_micro_features(gmsh, target_size)
        # Solids are never volume-meshed: generate(2) touches surfaces only, so
        # the result is the outer skin as shells (FR-02).
        if not gmsh.model.getEntities(2):
            raise MeshingError(f"STEP file {p} contains no meshable surfaces")

    def finish(gmsh: Any) -> None:
        # Node-relocation passes lift the healed fillet bands' element quality
        # without touching connectivity; failures degrade to the raw mesh.
        for method in ("Laplace2D", "Relocate2D"):
            try:
                gmsh.model.mesh.optimize(method)
            except Exception:  # noqa: BLE001 - optimisation is best-effort
                return

    return _mesh_with_gate(
        build,
        name=name,
        source=str(p),
        target_size=target_size,
        min_size=min_size,
        max_size=max_size,
        recombine=recombine,
        curvature_points=curvature_points,
        out_path=out_path,
        max_attempts=max_attempts,
        enforce=enforce,
        finish=finish,
        # Healed CAD keeps narrow fillet bands: sizes must spread from their
        # boundaries into the faces (extend=1) or the bands mesh as slivers,
        # and smoothing lifts the transition elements (measured on a CATIA
        # can export: min SICN 0.02 -> 0.26 with these two).
        option_overrides={
            "Mesh.MeshSizeExtendFromBoundary": 1,
            "Mesh.Smoothing": STEP_MESH_SMOOTHING_STEPS,
        },
    )


def mesh_tool(
    tool: ToolShape,
    *,
    can_height: float,
    target_size: float = MESH_TARGET_SIZE_DEFAULT_MM * 4.0,
    out_path: str | Path | None = None,
    enforce: bool = False,
    name: str = "ref_tool",
) -> MeshResult:
    """Mesh the rigid reference tool.

    The tool is rigid (/RBODY), so its mesh only has to describe the contact
    surface: a coarser target size than the can is intentional. The mesh gate is
    evaluated for the report but not enforced by default - a rigid body has no
    element-quality-driven timestep.

    Args:
        tool: Tool placement produced by :func:`crushsim.geometry.make_tool`.
        can_height: Can height [mm], used to size V-block plates.
        target_size: Target element size [mm].
        out_path: Optional ``.msh`` output path.
        enforce: Raise on gate failure.
        name: Part name.

    Raises:
        MeshingError: If the tool kind has no parametric builder.
    """
    if tool.kind == "step":
        if not tool.step_path:
            raise MeshingError("Tool kind 'step' requires loading.step_path in the case file")
        return mesh_step_surfaces(
            tool.step_path,
            target_size=target_size,
            out_path=out_path,
            enforce=enforce,
            name=name,
        )

    def build(gmsh: Any, target_size: float) -> None:
        _build_tool_surfaces(gmsh, tool, can_height=can_height, target_size=target_size)

    return _mesh_with_gate(
        build,
        name=name,
        source=f"tool({tool.kind})",
        target_size=target_size,
        out_path=out_path,
        max_attempts=1,
        enforce=enforce,
    )
