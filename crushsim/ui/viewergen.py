"""Generate the standalone interactive viewer HTML for a completed run.

The viewer is a single self-contained page (pure WebGL, no external
libraries): frame slider with inter-frame interpolation, orbit camera,
section planes, per-part opacity and von Mises / plastic-strain colouring.
Frame data comes from the run's converted VTK sequence - the official
``anim_to_vtk`` output, never a hand-rolled binary parser (spec §13.4).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import PostProcessError

_TEMPLATE = Path(__file__).parent / "static" / "viewer_template.html"

_VTK_QUAD = 9
_VTK_TRIANGLE = 5


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def _aligned_cells(mesh: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(quads[n,4], part_id[n], source_cell_index[n]) in file order.

    Cell connectivity and PART_ID are walked together - filtering them
    through separate APIs desynchronises the part labels the moment cell
    types interleave (measured: half a tool drawn under another part's
    settings). Triangles ride along as degenerate quads (n0,n1,n2,n2); the
    viewer's quad split renders them as single triangles.
    """
    celltypes = np.asarray(mesh.celltypes)
    part_ids = np.asarray(mesh.cell_data["PART_ID"])
    conn = np.asarray(mesh.cell_connectivity)
    offsets = np.asarray(mesh.offset)
    if len(offsets) == len(celltypes):  # legacy VTK offset convention
        offsets = np.concatenate([[0], offsets])
    quads, parts, source = [], [], []
    for i, ctype in enumerate(celltypes):
        nodes = conn[offsets[i] : offsets[i + 1]]
        if ctype == _VTK_QUAD:
            quads.append([nodes[0], nodes[1], nodes[2], nodes[3]])
        elif ctype == _VTK_TRIANGLE:
            quads.append([nodes[0], nodes[1], nodes[2], nodes[2]])
        else:
            continue
        parts.append(part_ids[i])
        source.append(i)
    return (
        np.asarray(quads, dtype=np.int64),
        np.asarray(parts, dtype=np.int32),
        np.asarray(source, dtype=np.int64),
    )


def _canonical_parts(
    quads: np.ndarray, part: np.ndarray, first: np.ndarray, last: np.ndarray
) -> np.ndarray:
    """Remap solver part ids to the viewer's 1=CAN 2=FLOOR 3=TOOL 4=SUPPORT.

    Deck part numbering varies between cases, so parts are identified by
    behaviour: the largest part is the can, the flattest small static part
    the floor, the moving rigid the tool, the remaining static one the
    support.
    """
    uniq = np.unique(part)
    counts = {int(p): int((part == p).sum()) for p in uniq}
    motion, flatness = {}, {}
    for p in uniq:
        nodes = np.unique(quads[part == p])
        motion[int(p)] = float(np.abs(last[nodes] - first[nodes]).max())
        z = first[nodes][:, 2]
        flatness[int(p)] = float(z.max() - z.min())
    can = max(counts, key=lambda p: counts[p])
    rest = [p for p in counts if p != can]
    remap = {can: 1}
    if rest:
        floor = min(rest, key=lambda p: (flatness[p], counts[p]))
        remap[floor] = 2
        rest = [p for p in rest if p != floor]
    if rest:
        # Every moving rigid is a TOOL (multi-tool decks have several); the
        # static remainder are SUPPORTs.
        top_motion = max(motion[p] for p in rest)
        for p in rest:
            moving = motion[p] > max(0.1, 0.05 * top_motion)
            remap[p] = 3 if moving else 4
        if not any(v == 3 for v in remap.values()):
            remap[max(rest, key=lambda p: motion[p])] = 3
    return np.vectorize(remap.__getitem__)(part).astype(np.uint8)


def generate_viewer(
    run_dir: str | Path,
    *,
    title: str,
    note: str = "",
    out_path: str | Path | None = None,
    max_frames: int | None = None,
    include_plastic: bool = True,
) -> Path:
    """Build the standalone viewer for ``run_dir`` and return its path.

    ``max_frames`` subsamples the frame sequence evenly (first and last kept)
    and ``include_plastic=False`` drops the plastic-strain field - both are
    size levers for very fine meshes, where the full page can exceed what a
    browser (or an artifact host) will take. The slider still interpolates
    between the frames that remain.

    Raises:
        PostProcessError: If the run has no VTK sequence or curve.
    """
    import pyvista as pv  # noqa: PLC0415 - heavy import kept off the CLI path

    run = Path(run_dir)
    files = sorted((run / "vtk").glob("*.vtk"))
    if not files:
        raise PostProcessError(
            f"No VTK frames in {run / 'vtk'} - run the pipeline (or csim render) first."
        )
    if max_frames is not None and 2 <= max_frames < len(files):
        keep = np.unique(np.linspace(0, len(files) - 1, max_frames).round().astype(int))
        files = [files[i] for i in keep]
    curve_csv = run / "force_displacement.csv"
    if not curve_csv.is_file():
        raise PostProcessError(
            f"No curve at {curve_csv} - post-processing has not run."
        )

    first_mesh = pv.read(files[0])
    quads, part, source = _aligned_cells(first_mesh)

    positions, von_mises, plastic, times = [], [], [], []
    for f in files:
        mesh = pv.read(f)
        positions.append(np.asarray(mesh.points, dtype=np.float32))
        von_mises.append(
            np.asarray(mesh.cell_data["2DELEM_Von_Mises"], dtype=np.float32)[source]
        )
        if include_plastic:
            plastic.append(
                np.asarray(mesh.cell_data["2DELEM_Plastic_Strain"], dtype=np.float32)[
                    source
                ]
            )
        stamp = None
        for key in mesh.field_data.keys():
            if "time" in key.lower():
                stamp = float(np.asarray(mesh.field_data[key]).ravel()[0])
        times.append(stamp)
    if times[0] is None:
        times = [float(i) for i in range(len(files))]

    part_canon = _canonical_parts(quads, part, positions[0], positions[-1])

    import pandas as pd  # noqa: PLC0415

    curve = pd.read_csv(curve_csv)
    n_points = int(first_mesh.n_points)
    index_type: Any = np.uint16 if n_points < 65536 else np.uint32
    data = {
        "meta": {
            "case": title,
            "elements": int(quads.shape[0]),
            "nodes": n_points,
            "frames": len(files),
            "end_time_s": times[-1],
            "parts": {"1": "CAN", "2": "FLOOR", "3": "TOOL", "4": "SUPPORT"},
        },
        "quads": _b64(quads.astype(index_type)),
        "quads_dtype": "u2" if n_points < 65536 else "u4",
        "part": _b64(part_canon),
        "times": times,
        "pos": [_b64(p) for p in positions],
        "vm": [_b64(v) for v in von_mises],
        "ps": [_b64(p) for p in plastic],
        "curve": {
            "t": curve["time"].tolist(),
            "d": curve["displacement"].tolist(),
            "f": curve["force"].tolist(),
        },
    }

    html = _TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__TITLE__", title)
    html = html.replace("__NOTE__", note or f"{title} 런의 실제 프레임 데이터로")
    html = html.replace("__DATA__", json.dumps(data))

    target = Path(out_path) if out_path else run / "viewer.html"
    target.write_text(html, encoding="utf-8")
    return target
