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
from ..geometry.parametric import CanShell, ToolShape
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


def _build_can_surfaces(gmsh: Any, can: CanShell) -> list[int]:
    """Build the can mid-surface in the current model and return surface tags.

    The lateral wall is created by extruding a circle (an edge), which yields a
    pure surface - never a solid - matching the FR-02 "outer skin only" rule.
    """
    occ = gmsh.model.occ
    r = can.mid_surface_radius
    circle = occ.addCircle(0.0, 0.0, 0.0, r)
    extruded = occ.extrude([(1, circle)], 0.0, 0.0, can.height)
    surfaces = [tag for dim, tag in extruded if dim == 2]
    if can.closed_bottom:
        loop_b = occ.addCurveLoop([circle])
        surfaces.append(occ.addPlaneSurface([loop_b]))
    if can.closed_top:
        top_circle = occ.addCircle(0.0, 0.0, can.height, r)
        loop_t = occ.addCurveLoop([top_circle])
        surfaces.append(occ.addPlaneSurface([loop_t]))
    if len(surfaces) > 1:
        fused, _ = occ.fragment([(2, surfaces[0])], [(2, s) for s in surfaces[1:]])
        surfaces = [tag for dim, tag in fused if dim == 2]
    occ.synchronize()
    if not surfaces:
        raise MeshingError("Failed to build the can mid-surface with the Gmsh OCC kernel")
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


def _build_tool_surfaces(gmsh: Any, tool: ToolShape, *, can_height: float) -> list[int]:
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
    else:
        raise MeshingError(
            f"Tool kind {tool.kind!r} has no parametric builder; "
            "use a STEP file (loading.tool: step) instead."
        )
    occ.synchronize()
    if not surfaces:
        raise MeshingError(f"Failed to build tool surfaces for kind {tool.kind!r}")
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


def mesh_parametric_can(
    can: CanShell,
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
    """Mesh a parametric can shell with quad-dominant elements.

    Args:
        can: The can definition.
        target_size: Target element size [mm].
        min_size: Lower mesh-size bound [mm]; defaults to ``0.5 * target_size``.
        max_size: Upper mesh-size bound [mm]; defaults to ``1.5 * target_size``.
        recombine: Recombine triangles into quads.
        curvature_points: Elements per 2*pi of curvature.
        out_path: Optional ``.msh`` output path.
        max_attempts: Automatic remesh attempts on gate failure.
        enforce: Raise :class:`~crushsim.errors.GateFailure` when the gate fails.
        name: Part name.

    Raises:
        GateFailure: If the mesh gate fails and ``enforce`` is True.
        MeshingError: If Gmsh cannot build or mesh the geometry.
    """

    def build(gmsh: Any, _target_size: float) -> None:
        _build_can_surfaces(gmsh, can)

    return _mesh_with_gate(
        build,
        name=name,
        source=(
            f"parametric_can(R={can.radius}, H={can.height}, t={can.thickness})"
        ),
        target_size=target_size,
        min_size=min_size,
        max_size=max_size,
        recombine=recombine,
        curvature_points=curvature_points,
        out_path=out_path,
        max_attempts=max_attempts,
        enforce=enforce,
    )


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

    def build(gmsh: Any, _target_size: float) -> None:
        _build_tool_surfaces(gmsh, tool, can_height=can_height)

    return _mesh_with_gate(
        build,
        name=name,
        source=f"tool({tool.kind})",
        target_size=target_size,
        out_path=out_path,
        max_attempts=1,
        enforce=enforce,
    )
