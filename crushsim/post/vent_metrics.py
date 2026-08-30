"""Vent opening metrics: when has venting *happened*, engineering-wise?

Two milestones bracket the event, computed from the starter deck (element
geometry/thickness) and the engine listing (rupture log):

- **initiation**: the first membrane element deletes - the first
  through-crack. Gas starts leaking, but the flow area is a pinhole.
- **opening (activation)**: the open flow area first reaches
  :data:`OPENING_AREA_FRACTION` of the vent area, so the petals are
  mechanically free and gas can actually leave. This is the point a vent
  datasheet's burst/activation pressure corresponds to.

A flow-area threshold is used rather than "every score element has torn"
because the latter is not mesh-robust: on a refined score a single
lightly-loaded element at an arc tip can survive (measured: 179 of 180)
and suppress the milestone entirely, and the count itself changes with
the mesh while the area does not.

Between them runs the crack-propagation phase; the quantitative signal is
the cumulative open-area curve A_open(t) (initial areas of deleted
membrane elements), which the viewer overlays on the pressure ramp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

_CYCLE_RE = re.compile(r"\s*\d+\s+([0-9.E+-]+)\s+[0-9.E+-]+\s")

OPENING_AREA_FRACTION: float = 0.25
"""Open area / vent area at which the vent counts as activated."""


def _element_areas(deck_text: str, part_id: int) -> tuple[dict[int, float], dict[int, float]]:
    """(area_mm2, thickness) per element id of ``part_id``, from the starter."""
    nodes: dict[int, np.ndarray] = {}
    in_nodes = False
    for line in deck_text.splitlines():
        if line.startswith("/NODE"):
            in_nodes = True
            continue
        if in_nodes:
            if line.startswith("/"):
                in_nodes = False
                continue
            try:
                nodes[int(line[:10])] = np.array(
                    [float(line[10:30]), float(line[30:50]), float(line[50:70])]
                )
            except ValueError:
                continue
    areas: dict[int, float] = {}
    thickness: dict[int, float] = {}
    current: tuple[str, int] | None = None
    for line in deck_text.splitlines():
        if line.startswith("/SHELL/") or line.startswith("/SH3N/"):
            current = ("q" if "SHELL" in line else "t", int(line.split("/")[2]))
            continue
        if line.startswith("/"):
            current = None
            continue
        if current is None or current[1] != part_id:
            continue
        try:
            eid = int(line[:10])
            count = 4 if current[0] == "q" else 3
            pts = [nodes[int(line[10 + i * 10 : 20 + i * 10])] for i in range(count)]
        except (ValueError, KeyError):
            continue
        if count == 4:
            area = 0.5 * (
                np.linalg.norm(np.cross(pts[2] - pts[0], pts[1] - pts[0]))
                + np.linalg.norm(np.cross(pts[2] - pts[0], pts[3] - pts[0]))
            )
        else:
            area = 0.5 * np.linalg.norm(np.cross(pts[1] - pts[0], pts[2] - pts[0]))
        areas[eid] = float(area)
        # Trailing (phi, thk) fields: a quad line is 50+40 chars, a /SH3N
        # triangle line only 40+40 - size the guard per element type.
        if len(line) >= 10 * (1 + count) + 40:
            try:
                thickness[eid] = float(line[-20:])
            except ValueError:
                pass
    return areas, thickness


def rupture_log(out_text: str) -> list[tuple[int, float]]:
    """(element id, time) for every shell rupture in an engine listing."""
    t_current = 0.0
    events: list[tuple[int, float]] = []
    for line in out_text.splitlines():
        match = _CYCLE_RE.match(line)
        if match:
            try:
                t_current = float(match.group(1))
            except ValueError:
                pass
        elif "RUPTURE OF SHELL" in line:
            events.append((int(line.split()[-1]), t_current))
    return events


def vent_metrics(run_dir: str | Path) -> dict | None:
    """Opening milestones + open-area curve for a foil-vent run, or None.

    Returns:
        ``{t_initiation_s, t_opening_s, vent_area_mm2, score_elements,
        score_ruptured, t_score_fully_torn_s, area_curve: [[t, mm2], ...]}``
        - ``t_opening_s`` is None while the open area never reaches
        :data:`OPENING_AREA_FRACTION` of the vent.
    """
    run = Path(run_dir)
    summary_path = run / "pipeline_summary.json"
    starters = sorted((run / "deck").glob("*_0000.rad"))
    listings = sorted((run / "deck").glob("*_0001.out"))
    if not (summary_path.is_file() and starters and listings):
        return None
    parts = (json.loads(summary_path.read_text(encoding="utf-8")).get("deck") or {}).get(
        "parts"
    ) or []
    membrane = next(
        (p for p in parts if p.get("role") == "deformable" and "MEMBRANE" in str(p.get("name"))),
        None,
    )
    if membrane is None:
        return None
    part_id = int(membrane["part_id"])
    areas, thickness = _element_areas(
        starters[0].read_text(encoding="utf-8", errors="replace"), part_id
    )
    if not areas:
        return None
    scored = (
        {eid for eid, t in thickness.items() if t <= min(thickness.values()) + 1e-9}
        if thickness
        else set()
    )
    events = [
        (eid, t)
        for eid, t in rupture_log(listings[0].read_text(encoding="utf-8", errors="replace"))
        if eid in areas
    ]
    if not events:
        return None
    curve: list[list[float]] = [[0.0, 0.0]]
    open_area = 0.0
    for eid, t in events:
        open_area += areas[eid]
        curve.append([t, open_area])
    score_ruptured = {eid for eid, _ in events if eid in scored}
    vent_area = float(sum(areas.values()))
    threshold = OPENING_AREA_FRACTION * vent_area
    t_opening = next((t for t, area in curve if area >= threshold), None)
    return {
        "t_initiation_s": events[0][1],
        "t_opening_s": t_opening,
        "opening_area_fraction": OPENING_AREA_FRACTION,
        "vent_area_mm2": vent_area,
        "score_elements": len(scored),
        "score_ruptured": len(score_ruptured),
        "t_score_fully_torn_s": (
            max(t for eid, t in events if eid in scored)
            if scored and score_ruptured == scored
            else None
        ),
        "area_curve": curve,
    }
