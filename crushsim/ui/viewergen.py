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
import re
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


def _parts_from_summary(summary: dict, part: np.ndarray) -> np.ndarray | None:
    """Role-based part remap from pipeline_summary deck parts, if they cover
    every solver part id in the frames. Beats the behaviour heuristic when
    available: a flying vent foil would otherwise be classified as a TOOL.

    Viewer ids: 1=CAN 2=FLOOR 3=TOOL 4=SUPPORT 5=VENT (foil membrane).
    """
    rows = (summary.get("deck") or {}).get("parts") or []
    remap: dict[int, int] = {}
    for row in rows:
        pid = row.get("part_id")
        if pid is None:
            return None
        name, role = str(row.get("name", "")), row.get("role")
        if role == "deformable":
            remap[int(pid)] = 5 if "MEMBRANE" in name or "VENT" in name else 1
        elif name == "FLOOR":
            remap[int(pid)] = 2
        elif role == "floor":  # fixed supports
            remap[int(pid)] = 4
        else:
            remap[int(pid)] = 3
    if not remap or any(int(p) not in remap for p in np.unique(part)):
        return None
    return np.vectorize(remap.__getitem__)(part).astype(np.uint8)


def _pressure_curve(run: Path, summary: dict) -> dict | None:
    """P(t) polyline for pressure-driven runs, from the starter's ramp FUNCT.

    The deck is the source of truth (works for runs made before the summary
    carried pressure data). Returns the viewer curve payload, with the first
    shell-rupture time from the engine listing as a marker when one exists.
    """
    starter = (summary.get("deck") or {}).get("starter")
    if not starter:
        return None
    path = Path(starter)
    if not path.is_absolute():
        path = run.parent.parent / starter if not path.exists() else path
    if not path.is_file():
        candidates = sorted((run / "deck").glob("*_0000.rad"))
        if not candidates:
            return None
        path = candidates[0]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    ts: list[float] = []
    ps: list[float] = []
    grab = 0
    for ln in lines:
        if ln.startswith("/FUNCT/") and grab == 0:
            grab = 1
            continue
        if grab == 1:  # function title line
            grab = 2 if ln.strip() == "CAN_PRESSURE_RAMP" else 0
            continue
        if grab == 2:
            if ln.startswith("/"):
                break
            if ln.startswith("#"):
                continue
            try:
                x, y = float(ln[:20]), float(ln[20:40])
            except ValueError:
                break
            ts.append(x)
            ps.append(y)
    if len(ts) < 2:
        return None
    curve: dict[str, Any] = {
        "kind": "pressure",
        "t": ts,
        "x": [t * 1000.0 for t in ts],
        "y": ps,
        "xlabel": "t [ms]",
        "ylabel": "내압 P [MPa]",
        "xunit": " ms",
        "yunit": " MPa",
    }
    # Vent milestones + open-area overlay for foil-vent runs; plain
    # first-rupture marker otherwise.
    metrics = None
    try:
        from ..post.vent_metrics import vent_metrics  # noqa: PLC0415

        metrics = vent_metrics(run)
    except Exception:  # noqa: BLE001 - metrics are an extra, never a blocker
        metrics = None
    if metrics is not None:
        marks = [{"t": metrics["t_initiation_s"], "label": "파단 개시"}]
        if metrics["t_opening_s"] is not None:
            pct = int(round(100 * metrics.get("opening_area_fraction", 0.25)))
            marks.append({"t": metrics["t_opening_s"], "label": f"벤트 개방 ({pct}%)"})
        curve["marks"] = marks
        vent_area = metrics["vent_area_mm2"] or 1.0
        curve["area"] = {
            "t": [p[0] for p in metrics["area_curve"]],
            "y": [100.0 * p[1] / vent_area for p in metrics["area_curve"]],
            "label": "개구 면적 [% 벤트]",
            "total_mm2": vent_area,
        }
        return curve
    out_files = sorted((run / "deck").glob("*_0001.out"))
    if out_files:
        t_cur, t_first = 0.0, None
        for ln in out_files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            m = _CYCLE_RE.match(ln)
            if m:
                try:
                    t_cur = float(m.group(1))
                except ValueError:
                    pass
            elif "RUPTURE OF SHELL" in ln:
                t_first = t_cur
                break
        if t_first is not None:
            curve["marks"] = [{"t": t_first, "label": "파단 개시"}]
    return curve


_CYCLE_RE = re.compile(r"\s*\d+\s+([0-9.E+-]+)\s+[0-9.E+-]+\s")


def _dims_lines(summary: dict) -> list[list[str]]:
    """[label, value] rows for the viewer's 주요 치수 panel."""
    rows: list[list[str]] = []
    geo = summary.get("geometry") or {}
    box = geo.get("box") or {}

    def g(src: dict, key: str) -> float | None:
        v = src.get(key)
        return None if v is None else float(v)

    if box:
        w, d, h = g(box, "width_mm"), g(box, "depth_mm"), g(box, "height_mm")
        if w and d and h:
            rows.append(["캔 (W×D×H)", f"{w:g} × {d:g} × {h:g} mm"])
        if g(box, "thickness_mm"):
            rows.append(["벽 두께", f"{box['thickness_mm']:g} mm"])
        vent = box.get("vent") or {}
        if vent:
            if g(vent, "length_mm") and g(vent, "width_mm"):
                rows.append(["벤트 (L×W)", f"{vent['length_mm']:g} × {vent['width_mm']:g} mm"])
            if g(vent, "membrane_thickness_mm"):
                rows.append(["멤브레인 두께", f"{vent['membrane_thickness_mm']:g} mm"])
            if g(vent, "score_thickness_mm"):
                rows.append(["스코어 잔여 두께", f"{vent['score_thickness_mm']:g} mm"])
            if g(vent, "band_mm"):
                rows.append(["스코어 밴드 폭", f"{vent['band_mm']:g} mm"])
    elif geo:
        if g(geo, "diameter_mm") and g(geo, "height_mm"):
            rows.append(["캔 (Ø×H)", f"{geo['diameter_mm']:.1f} × {geo['height_mm']:g} mm"])
        if g(geo, "thickness_mm"):
            rows.append(["벽 두께", f"{geo['thickness_mm']:g} mm"])
    for part in (summary.get("deck") or {}).get("parts") or []:
        if part.get("role") == "deformable" and part.get("material"):
            label = "재료" if part.get("name") in (None, "CAN") else f"재료 ({part['name']})"
            rows.append([label, f"{part['material']} · t={part.get('thickness_mm', '?'):g} mm"])
    return rows


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
    curve_csv = run / "force_displacement.csv"  # absent on pressure-driven runs

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

    summary: dict = {}
    summary_path = run / "pipeline_summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a broken summary must not block the viewer
            summary = {}

    part_canon = _parts_from_summary(summary, part)
    if part_canon is None:
        part_canon = _canonical_parts(quads, part, positions[0], positions[-1])

    # Score mask: the engraved grooves of a vent foil (its thinnest elements)
    # render darker in part-colour mode, so the pattern the tear follows is
    # visible before anything moves.
    score_mask = np.zeros(quads.shape[0], dtype=np.uint8)
    if "2DELEM_Thickness" in first_mesh.cell_data:
        thickness0 = np.asarray(first_mesh.cell_data["2DELEM_Thickness"], dtype=float)[source]
        vent_sel = part_canon == 5
        if vent_sel.any():
            t_lo, t_hi = thickness0[vent_sel].min(), thickness0[vent_sel].max()
            if t_hi > t_lo * 1.5:
                score_mask[vent_sel & (thickness0 < (t_lo + t_hi) / 2.0)] = 1

    import pandas as pd  # noqa: PLC0415

    curve_payload: dict | None = None
    if curve_csv.is_file():
        curve = pd.read_csv(curve_csv)
        curve_payload = {
            "kind": "force",
            "t": curve["time"].tolist(),
            "x": curve["displacement"].tolist(),
            "y": curve["force"].tolist(),
            "xlabel": "변위 d [mm]",
            "ylabel": "하중 F [N]",
            "xunit": " mm",
            "yunit": " N",
        }
    else:  # pressure-driven runs: P(t) from the deck's ramp function
        try:
            curve_payload = _pressure_curve(run, summary)
        except Exception:  # noqa: BLE001 - the curve is an extra, never a blocker
            curve_payload = None

    n_points = int(first_mesh.n_points)
    index_type: Any = np.uint16 if n_points < 65536 else np.uint32
    data = {
        "meta": {
            "case": title,
            "elements": int(quads.shape[0]),
            "nodes": n_points,
            "frames": len(files),
            "end_time_s": times[-1],
            "parts": {"1": "CAN", "2": "FLOOR", "3": "TOOL", "4": "SUPPORT", "5": "VENT"},
            "dims": _dims_lines(summary),
        },
        "quads": _b64(quads.astype(index_type)),
        "quads_dtype": "u2" if n_points < 65536 else "u4",
        "part": _b64(part_canon),
        "score": _b64(score_mask) if score_mask.any() else None,
        "times": times,
        "pos": [_b64(p) for p in positions],
        "vm": [_b64(v) for v in von_mises],
        "ps": [_b64(p) for p in plastic],
        "curve": curve_payload,
    }

    # The viewer draws the shell mid-surface, so the wall looks paper-thin;
    # state explicitly that the thickness is a solved property, not omitted.
    thickness_note = ""
    try:
        parts = (summary.get("deck") or {}).get("parts") or []
        values = sorted(
            {float(p["thickness_mm"]) for p in parts if p.get("role") == "deformable"}
        )
        if values:
            shown = "/".join(f"{v:g}" for v in values)
            thickness_note = (
                f" · 쉘 중립면 표시 — 벽 두께 t={shown}mm는 쉘 물성으로 "
                "강성(굽힘 ∝ t³)·질량·접촉에 반영됨"
            )
    except Exception:  # noqa: BLE001 - a broken summary must not block the viewer
        pass

    html = _TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__TITLE__", title)
    html = html.replace("__NOTE__", (note or f"{title} 런의 실제 프레임 데이터로") + thickness_note)
    html = html.replace("__DATA__", json.dumps(data))

    target = Path(out_path) if out_path else run / "viewer.html"
    target.write_text(html, encoding="utf-8")
    return target
