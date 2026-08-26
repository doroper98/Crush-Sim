"""Resume geometry: a prior run's deformed shape as the next run's can.

Process chaining (e.g. beading -> arbor out -> crimping) needs the deformed
shape carried into a fresh run. Rather than round-tripping through the
faceted STEP export, the can shell is rebuilt directly from the source run's
converted VTK frame: same connectivity, deformed nodal positions, thickness
from the source deck.

Carried over: geometry (deformed mid-surface) and the part's uniform shell
thickness. NOT carried over (v1): residual stresses/plastic strains (state
initialisation), per-element thickness patches, and thinning. Results of a
resumed run are therefore softer than reality around previously worked
regions - trend-grade until state carry-over lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..errors import MeshingError
from .mesh_data import ShellMesh

_VTK_QUAD = 9
_VTK_TRIANGLE = 5


def load_resume_mesh(
    run_dir: str | Path,
    *,
    frame: int = -1,
    name: str = "CAN",
) -> tuple[ShellMesh, float]:
    """Rebuild the deformable part of ``run_dir`` at ``frame`` as a ShellMesh.

    Returns:
        ``(mesh, thickness_mm)`` - thickness from the source run's deck.

    Raises:
        MeshingError: Missing frames/summary, or no deformable elements.
    """
    import pyvista as pv  # noqa: PLC0415 - heavy import kept off the CLI path

    run = Path(run_dir)
    files = sorted((run / "vtk").glob("*.vtk"))
    if not files:
        raise MeshingError(
            f"Resume source {run} has no converted VTK frames (vtk/); run it first."
        )
    summary_path = run / "pipeline_summary.json"
    if not summary_path.is_file():
        raise MeshingError(f"Resume source {run} has no pipeline_summary.json.")
    parts = (json.loads(summary_path.read_text(encoding="utf-8")).get("deck") or {}).get(
        "parts"
    ) or []
    deformable = [p for p in parts if p.get("role") == "deformable"]
    if not deformable:
        raise MeshingError(f"Resume source {run} has no deformable part in its deck.")
    keep_ids = {int(p["part_id"]) for p in deformable}
    thickness = float(deformable[0]["thickness_mm"])

    index = frame % len(files)
    grid = pv.read(files[index])
    celltypes = np.asarray(grid.celltypes)
    part_ids = np.asarray(grid.cell_data["PART_ID"])
    conn = np.asarray(grid.cell_connectivity)
    offsets = np.asarray(grid.offset)
    if len(offsets) == len(celltypes):  # legacy VTK offset convention
        offsets = np.concatenate([[0], offsets])

    quads: list[list[int]] = []
    tris: list[list[int]] = []
    for i, ctype in enumerate(celltypes):
        if int(part_ids[i]) not in keep_ids:
            continue
        nodes = conn[offsets[i] : offsets[i + 1]]
        if ctype == _VTK_QUAD:
            quads.append([nodes[0], nodes[1], nodes[2], nodes[3]])
        elif ctype == _VTK_TRIANGLE:
            tris.append([nodes[0], nodes[1], nodes[2]])
    if not quads and not tris:
        raise MeshingError(f"Frame {index} of {run} holds no deformable shell elements.")

    used = np.unique(np.concatenate([np.asarray(quads).ravel() if quads else np.empty(0, int),
                                     np.asarray(tris).ravel() if tris else np.empty(0, int)]).astype(int))
    remap = {int(old): i + 1 for i, old in enumerate(used)}
    points = np.asarray(grid.points, dtype=float)[used]
    map_block = np.vectorize(remap.__getitem__, otypes=[np.int64])
    mesh = ShellMesh(
        name=name,
        node_ids=np.arange(1, used.size + 1, dtype=np.int64),
        nodes=points,
        quads=map_block(np.asarray(quads, dtype=np.int64)) if quads else np.zeros((0, 4), dtype=np.int64),
        tris=map_block(np.asarray(tris, dtype=np.int64)) if tris else np.zeros((0, 3), dtype=np.int64),
        source=f"resume({run.name}, frame {index})",
    )
    return mesh, thickness


def quality_of_mesh(mesh: ShellMesh) -> dict[str, float]:
    """Geometry-only quality statistics computed without Gmsh.

    SICN is approximated per corner as sin(angle) scaled by the edge-length
    ratio - close enough to rank distortion on a deformed shell, where the
    §7 gate is reported but never enforced (folded elements ARE distorted;
    that is the physics being carried over, not a meshing defect).
    """
    index = mesh.node_index()
    min_sicn, sicn_sum, count = 1.0, 0.0, 0
    min_edge, max_edge, max_ar = np.inf, 0.0, 0.0
    for block, nsides in ((mesh.quads, 4), (mesh.tris, 3)):
        for element in block:
            pts = mesh.nodes[[index[int(n)] for n in element]]
            edges = [pts[(i + 1) % nsides] - pts[i] for i in range(nsides)]
            lens = [float(np.linalg.norm(e)) for e in edges]
            if min(lens) <= 0.0:
                min_sicn = 0.0
                continue
            min_edge = min(min_edge, min(lens))
            max_edge = max(max_edge, max(lens))
            max_ar = max(max_ar, max(lens) / min(lens))
            corner = 1.0
            for i in range(nsides):
                a = edges[i] / lens[i]
                b = -edges[i - 1] / lens[i - 1]
                sin_theta = float(np.linalg.norm(np.cross(a, b)))
                ratio = min(lens[i], lens[i - 1]) / max(lens[i], lens[i - 1])
                corner = min(corner, sin_theta * ratio)
            min_sicn = min(min_sicn, corner)
            sicn_sum += corner
            count += 1
    return {
        "min_sicn": float(min_sicn),
        "mean_sicn": float(sicn_sum / max(count, 1)),
        "min_edge_length": float(min_edge if np.isfinite(min_edge) else 0.0),
        "max_edge_length": float(max_edge),
        "max_aspect_ratio": float(max_ar),
    }
