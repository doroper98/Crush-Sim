"""FR (issue #26) - export a run's deformed shape at any frame as STEP.

The solver's converted VTK frames carry the deformed nodal positions; this
module rebuilds the selected part's shell as a faceted OCC shape (every
element becomes a planar triangular face, quads split along a diagonal),
sews it into a connected shell and writes STEP AP214. A faceted STEP is what
CAD packages produce when converting scanned/mesh data - it imports cleanly
into CATIA/SolidWorks for downstream jig design or as the starting shape of
a follow-on forming stage (e.g. crimping after beading, once the arbor is
out).

Needs the ``cad`` extra (OCP); everything else in the pipeline runs without
it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import PostProcessError

_VTK_QUAD = 9
_VTK_TRIANGLE = 5

_SEW_FACE_LIMIT = 80_000
"""Above this face count sewing is skipped (a compound still exports)."""


def _require_ocp() -> None:
    try:
        import OCP  # noqa: F401, PLC0415
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise PostProcessError(
            "STEP export needs the OCC kernel: pip install crushsim[cad] "
            "(package cadquery-ocp)."
        ) from exc


def _frame_files(run_dir: Path) -> list[Path]:
    files = sorted((run_dir / "vtk").glob("*.vtk"))
    if not files:
        raise PostProcessError(
            f"No VTK frames in {run_dir / 'vtk'} - run the pipeline (or csim render) first."
        )
    return files


def _part_ids_for(run_dir: Path, part: str) -> set[int] | None:
    """Solver part ids matching ``part`` ('can', 'all', or a part name)."""
    if part == "all":
        return None
    summary = run_dir / "pipeline_summary.json"
    if not summary.is_file():
        raise PostProcessError(
            f"No pipeline_summary.json in {run_dir}; pass part='all' to export everything."
        )
    parts = (json.loads(summary.read_text(encoding="utf-8")).get("deck") or {}).get("parts") or []
    if part == "can":
        ids = {int(p["part_id"]) for p in parts if p.get("role") == "deformable"}
    else:
        ids = {int(p["part_id"]) for p in parts if p.get("name", "").lower() == part.lower()}
    if not ids:
        known = ", ".join(str(p.get("name")) for p in parts)
        raise PostProcessError(f"No deck part matches {part!r} (parts: {known})")
    return ids


def _triangles(mesh: Any, keep_ids: set[int] | None) -> tuple[np.ndarray, np.ndarray]:
    """(points[n,3], tris[m,3]) of the selected part, quads split on a diagonal."""
    celltypes = np.asarray(mesh.celltypes)
    part_ids = np.asarray(mesh.cell_data["PART_ID"])
    conn = np.asarray(mesh.cell_connectivity)
    offsets = np.asarray(mesh.offset)
    if len(offsets) == len(celltypes):  # legacy VTK offset convention
        offsets = np.concatenate([[0], offsets])
    tris: list[list[int]] = []
    for i, ctype in enumerate(celltypes):
        if keep_ids is not None and int(part_ids[i]) not in keep_ids:
            continue
        nodes = conn[offsets[i] : offsets[i + 1]]
        if ctype == _VTK_TRIANGLE:
            tris.append([nodes[0], nodes[1], nodes[2]])
        elif ctype == _VTK_QUAD:
            tris.append([nodes[0], nodes[1], nodes[2]])
            tris.append([nodes[0], nodes[2], nodes[3]])
    if not tris:
        raise PostProcessError("Selected part has no shell elements in this frame.")
    return np.asarray(mesh.points, dtype=float), np.asarray(tris, dtype=np.int64)


def _deformable_thicknesses(run_dir: Path) -> list[float]:
    """Deformable-part thicknesses [mm] from the run summary (may be empty)."""
    summary = run_dir / "pipeline_summary.json"
    if not summary.is_file():
        return []
    try:
        parts = (json.loads(summary.read_text(encoding="utf-8")).get("deck") or {}).get(
            "parts"
        ) or []
        return sorted(
            {float(p["thickness_mm"]) for p in parts if p.get("role") == "deformable"}
        )
    except Exception:  # noqa: BLE001 - cosmetic metadata only
        return []


def export_deformed_step(
    run_dir: str | Path,
    *,
    frame: int | None = None,
    time_s: float | None = None,
    part: str = "can",
    out_path: str | Path | None = None,
) -> Path:
    """Export the deformed shape of ``part`` at a frame (or time) as STEP.

    Args:
        run_dir: A completed run directory (with the converted ``vtk/``).
        frame: Frame index into the animation sequence; negative counts from
            the end. Default: the last frame.
        time_s: Alternatively, pick the frame nearest this solution time [s].
        part: ``can`` (the deformable part, default), ``all``, or a deck part
            name (``REF_TOOL``, ``FLOOR``, ...).
        out_path: Output ``.stp`` path; defaults to
            ``<run_dir>/export/<part>_f<NN>.stp``.

    Raises:
        PostProcessError: Missing OCP, missing frames, or an empty selection.
    """
    _require_ocp()
    import pyvista as pv  # noqa: PLC0415

    from OCP.BRep import BRep_Builder  # noqa: PLC0415
    from OCP.BRepBuilderAPI import (  # noqa: PLC0415
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_Sewing,
    )
    from OCP.gp import gp_Pnt  # noqa: PLC0415
    from OCP.IFSelect import IFSelect_RetDone  # noqa: PLC0415
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: PLC0415
    from OCP.TopoDS import TopoDS_Compound  # noqa: PLC0415

    run = Path(run_dir)
    files = _frame_files(run)
    if time_s is not None:
        stamps = []
        for f in files:
            m = pv.read(f)
            stamp = 0.0
            for key in m.field_data.keys():
                if "time" in key.lower():
                    stamp = float(np.asarray(m.field_data[key]).ravel()[0])
            stamps.append(stamp)
        index = int(np.argmin(np.abs(np.asarray(stamps) - time_s)))
    else:
        index = (frame if frame is not None else -1) % len(files)

    mesh = pv.read(files[index])
    points, tris = _triangles(mesh, _part_ids_for(run, part))

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    n_faces = 0
    for a, b, c in tris:
        pa, pb, pc = (gp_Pnt(*points[i]) for i in (a, b, c))
        # Degenerate slivers (deleted/collapsed elements) are skipped.
        if (
            np.linalg.norm(
                np.cross(points[b] - points[a], points[c] - points[a])
            )
            < 1.0e-9
        ):
            continue
        polygon = BRepBuilderAPI_MakePolygon(pa, pb, pc, True)
        face = BRepBuilderAPI_MakeFace(polygon.Wire(), True)
        if face.IsDone():
            builder.Add(compound, face.Face())
            n_faces += 1
    if not n_faces:
        raise PostProcessError("Every selected element was degenerate; nothing to export.")

    shape = compound
    if n_faces <= _SEW_FACE_LIMIT:
        sewing = BRepBuilderAPI_Sewing(1.0e-3)
        sewing.Add(compound)
        sewing.Perform()
        shape = sewing.SewedShape()

    target = (
        Path(out_path)
        if out_path
        else run / "export" / f"{part}_f{index:02d}.stp"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    # Stamp the shell nature and thickness into the STEP header so the file
    # itself says how to rebuild the solid (CAD thicken with this t).
    try:
        from OCP.APIHeaderSection import APIHeaderSection_MakeHeader  # noqa: PLC0415
        from OCP.TCollection import TCollection_HAsciiString  # noqa: PLC0415

        thicknesses = _deformable_thicknesses(run)
        shown = "/".join(f"{t:g}" for t in thicknesses) if thicknesses else "unknown"
        header = APIHeaderSection_MakeHeader(writer.Model())
        header.SetName(TCollection_HAsciiString(f"{run.name} {part} frame {index}"))
        header.SetDescriptionValue(
            1,
            TCollection_HAsciiString(
                f"Crush-Sim deformed SHELL MID-SURFACE (zero-thickness faces); "
                f"wall thickness t={shown} mm is a shell property - thicken in "
                "CAD to rebuild the solid. Scored regions may be locally thinner."
            ),
        )
    except Exception:  # noqa: BLE001 - the header stamp must never block the export
        pass
    status = writer.Write(str(target))
    if status != IFSelect_RetDone or not target.is_file():
        raise PostProcessError(f"STEP write failed (status {status}) for {target}")
    return target
