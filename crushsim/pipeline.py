"""End-to-end pipeline orchestration for ``csim all`` (spec §3, §12).

Stage order: geometry -> meshing (+ mesh gate) -> deck -> solver -> official
converters -> curves/animation -> HTML report. CATPart conversion (FR-01) is
deliberately outside this chain: it is a Windows-only pre-step.

ADR-06 is enforced between stages - a failed mesh gate raises before a deck is
written, so no ungated artefact ever reaches the solver.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import (
    CaseConfig,
    MaterialCard,
    find_material_card,
    load_case,
    load_material_card,
)
from .deck.writer import INITIAL_CONTACT_CLEARANCE, DeckResult, build_deck
from .errors import CrushSimError, SolverError
from .geometry.parametric import BoxCan, CanShell, ToolShape, VentSpec, make_can, make_tool
from .meshing.gates import GateResult
from .meshing.mesher import (
    MeshResult,
    mesh_box_can,
    mesh_parametric_can,
    mesh_step_surfaces,
    mesh_tool,
)
from .report.builder import build_context, render_report
from .solver.runner import RunResult, run_solver
from .units import MESH_TARGET_SIZE_DEFAULT_MM

FLOOR_TOOL_SIZE_FACTOR: float = 1.6
"""FLOOR plate edge length as a multiple of the can diameter."""

RIGID_MESH_SIZE_FACTOR: float = 4.0
"""Rigid-part target element size as a multiple of the can target size."""


@dataclass(slots=True)
class GeometryStage:
    """Result of the geometry stage."""

    can: CanShell
    tool: ToolShape | None
    floor: ToolShape
    support: ToolShape | None = None
    step_path: Path | None = None
    extra_tools: list[ToolShape] = field(default_factory=list)
    box: BoxCan | None = None
    """The prismatic can (kind box_can); ``can`` is then its sizing proxy."""

    def summary(self) -> dict[str, Any]:
        """Flat dictionary for reports."""
        return {
            **self.can.summary(),
            "tool": self.tool.summary() if self.tool else None,
            "support": self.support.summary() if self.support else None,
            "extra_tools": [t.summary() for t in self.extra_tools],
            "box": self.box.summary() if self.box else None,
            "step_path": str(self.step_path) if self.step_path else None,
        }


@dataclass(slots=True)
class PipelineResult:
    """Everything one pipeline run produced."""

    case: CaseConfig
    material: MaterialCard
    run_dir: Path
    geometry: GeometryStage | None = None
    meshes: dict[str, MeshResult] = field(default_factory=dict)
    deck: DeckResult | None = None
    run: RunResult | None = None
    post: dict[str, Any] = field(default_factory=dict)
    report_path: Path | None = None
    stages_completed: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def gates(self) -> list[GateResult]:
        """Every gate evaluated during the run."""
        return [m.gate for m in self.meshes.values()]

    def summary(self) -> dict[str, Any]:
        """Flat dictionary written as ``pipeline_summary.json``."""
        return {
            "case": self.case.name,
            "load_case": self.case.load_case,
            "run_dir": str(self.run_dir),
            "material": self.material.key,
            "material_verified": self.material.is_verified,
            "geometry": self.geometry.summary() if self.geometry else None,
            "meshes": {k: v.summary() for k, v in self.meshes.items()},
            "deck": self.deck.summary() if self.deck else None,
            "solver": self.run.to_dict() if self.run else None,
            "post": self.post,
            "report": str(self.report_path) if self.report_path else None,
            "stages_completed": list(self.stages_completed),
            "notices": list(self.notices),
        }


def build_geometry(case: CaseConfig, *, can_override: CanShell | None = None) -> GeometryStage:
    """Build the can and the reference tools for a case (FR-02).

    Args:
        case: The loaded case configuration.
        can_override: Reference can used to size and place the tools instead
            of the case's parametric dimensions. The STEP path passes the
            proxy derived from the imported part's seated bounding box here,
            so tools wrap the real shape even when it is modelled lying down.

    Raises:
        GeometryError: If the case geometry is invalid.
    """
    box: BoxCan | None = None
    if case.geometry.kind == "box_can" and can_override is None:
        vent = None
        if case.geometry.vent is not None:
            raw = dict(case.geometry.vent)
            vent = VentSpec(
                length=raw["length"],
                width=raw["width"],
                band=raw.get("band", 0.8),
                score_thickness=raw.get("score_thickness", 0.05),
                membrane_thickness=raw.get("membrane_thickness"),
                pattern=raw.get("pattern", "perimeter"),
                arc_bulge=raw.get("arc_bulge", 0.30),
            )
        box = BoxCan(
            width=case.geometry.width or 0.0,
            depth=case.geometry.depth or 0.0,
            height=case.geometry.height,
            thickness=case.geometry.thickness,
            closed_bottom=case.geometry.closed_bottom,
            closed_top=case.geometry.closed_top,
            vent=vent,
        )
    can = can_override or make_can(
        # For a box can this is the tool/floor sizing proxy: footprint
        # half-diagonal as the radius, so plates clear the corners.
        math.hypot(box.width, box.depth) / 2.0 if box else case.geometry.radius,
        case.geometry.height,
        case.geometry.thickness,
        closed_bottom=case.geometry.closed_bottom,
        closed_top=case.geometry.closed_top,
    )
    tool = (
        None
        if case.loading.tool == "none"
        else make_tool(
            can,
            case.loading.tool,
            case.loading.direction,
            gap=case.loading.tool_gap,
            size=case.loading.tool_size,
            indenter_radius=case.loading.indenter_radius,
            step_path=str(case.loading.step_path) if case.loading.step_path else None,
            height_frac=case.loading.height_frac,
        )
    )
    extra_tools = [
        make_tool(
            can,
            extra.tool,
            extra.direction,
            gap=extra.tool_gap,
            size=extra.tool_size,
            indenter_radius=extra.indenter_radius,
            height_frac=extra.height_frac,
        )
        for extra in case.loading.extra_tools
    ]
    floor = make_tool(
        can,
        "platen",
        (0.0, 0.0, 1.0),
        # A touching floor sits inside the TYPE7 contact gap and the starter
        # rejects the deck with initial penetrations (ERROR 612).
        gap=INITIAL_CONTACT_CLEARANCE,
        size=can.diameter * FLOOR_TOOL_SIZE_FACTOR,
    )
    support = None
    if case.loading.support is not None:
        # Fixed support opposite the drive: without it a radially driven jig
        # just pushes the can across the floor instead of crushing it.
        opposite = tuple(-c for c in case.loading.direction)
        support = make_tool(
            can,
            case.loading.support,
            opposite,
            gap=INITIAL_CONTACT_CLEARANCE,
            size=case.loading.support_size or case.loading.tool_size,
            height_frac=case.loading.height_frac,
        )
    return GeometryStage(
        can=can,
        tool=tool,
        floor=floor,
        support=support,
        step_path=case.geometry.step_path,
        extra_tools=extra_tools,
        box=box,
    )


def _seated_can_proxy(mesh: Any, case: CaseConfig) -> CanShell:
    """Derive the tool-placement reference can from a seated STEP mesh.

    The tools (drive tool, floor, support) are sized and placed from a
    :class:`CanShell`; for imported geometry that reference must describe the
    part *as seated*, not the case's nominal radius/height - a can modelled
    lying down is wider than it is tall. The proxy takes the seated bounding
    box: height from the Z extent, radius from the in-plane extent *along the
    drive direction* for a lateral case (pressing the thin broad face of a
    rectangular can must start at that face, not at the widest one), falling
    back to the larger in-plane extent for axial drives.
    """
    lo, hi = mesh.bounding_box()
    dx, dy = hi[0] - lo[0], hi[1] - lo[1]
    ux, uy, uz = case.loading.direction
    norm = math.sqrt(ux * ux + uy * uy + uz * uz) or 1.0
    ux, uy, uz = ux / norm, uy / norm, uz / norm
    if abs(uz) > 0.9:
        radius = max(dx, dy) / 2.0
    else:
        radius = math.hypot(ux * dx / 2.0, uy * dy / 2.0) or max(dx, dy) / 2.0
    height = hi[2] - lo[2]
    return make_can(radius, height, case.geometry.thickness)


def build_meshes(
    case: CaseConfig,
    geometry: GeometryStage,
    *,
    outdir: Path,
    enforce_gate: bool = True,
) -> dict[str, MeshResult]:
    """Mesh the can, the floor and the reference tool (FR-03).

    For an imported STEP can the mesh is seated on the floor plane first
    (:meth:`ShellMesh.seat_on_floor`) and the tools in ``geometry`` are
    rebuilt around the seated bounding box, mutating the passed stage so the
    report reflects what was actually simulated. The ``can.msh`` file keeps
    the source file's coordinates; the deck carries the seated ones.

    Raises:
        GateFailure: If the can mesh fails the §7 gate and ``enforce_gate``.
    """
    target = case.mesh.target_size or MESH_TARGET_SIZE_DEFAULT_MM
    rigid_target = target * RIGID_MESH_SIZE_FACTOR

    if case.geometry.kind == "resume":
        from .meshing.mesher import MeshQuality, MeshResult  # noqa: PLC0415
        from .meshing.gates import evaluate_mesh_gate  # noqa: PLC0415
        from .meshing.resume import load_resume_mesh, quality_of_mesh  # noqa: PLC0415

        mesh, source_thickness = load_resume_mesh(
            case.geometry.resume_run, frame=case.geometry.resume_frame, name="CAN"
        )
        if abs(case.geometry.thickness - source_thickness) > 1e-9:
            raise CrushSimError(
                f"geometry.thickness {case.geometry.thickness} mm does not match the "
                f"resume source's shell thickness {source_thickness} mm - set "
                f"geometry.thickness: {source_thickness}"
            )
        mesh.seat_on_floor()
        stats = quality_of_mesh(mesh)
        gate = evaluate_mesh_gate(
            min_sicn=stats["min_sicn"],
            max_aspect_ratio=stats["max_aspect_ratio"],
            min_edge_length=stats["min_edge_length"],
            triangle_fraction=mesh.triangle_fraction,
            non_manifold_edges=mesh.non_manifold_edge_count(),
            info={
                "source": mesh.source,
                "note": "resume geometry: gate reported, never enforced - a "
                "deformed shell's distortion is carried-over physics",
            },
        )
        can_mesh = MeshResult(
            mesh=mesh,
            quality=MeshQuality(
                min_sicn=stats["min_sicn"],
                mean_sicn=stats["mean_sicn"],
                max_aspect_ratio=stats["max_aspect_ratio"],
                min_edge_length=stats["min_edge_length"],
                max_edge_length=stats["max_edge_length"],
                triangle_fraction=mesh.triangle_fraction,
                n_elements=mesh.n_elements,
                n_quads=mesh.n_quads,
                n_tris=mesh.n_tris,
            ),
            gate=gate,
            target_size=target,
            attempts=1,
        )
        seated = build_geometry(case, can_override=_seated_can_proxy(mesh, case))
        geometry.can = seated.can
        geometry.tool = seated.tool
        geometry.floor = seated.floor
        geometry.support = seated.support
        geometry.extra_tools = seated.extra_tools
    elif case.geometry.kind == "step":
        if case.geometry.step_path is None:  # pragma: no cover - validated at load time
            raise CrushSimError("geometry.kind == 'step' requires geometry.step_path")
        can_mesh = mesh_step_surfaces(
            case.geometry.step_path,
            target_size=target,
            min_size=case.mesh.min_size,
            max_size=case.mesh.max_size,
            recombine=case.mesh.recombine,
            curvature_points=case.mesh.curvature_points,
            out_path=outdir / "can.msh",
            enforce=enforce_gate,
            name="CAN",
        )
        can_mesh.mesh.seat_on_floor()
        seated = build_geometry(case, can_override=_seated_can_proxy(can_mesh.mesh, case))
        geometry.can = seated.can
        geometry.tool = seated.tool
        geometry.floor = seated.floor
        geometry.support = seated.support
        geometry.extra_tools = seated.extra_tools
    elif geometry.box is not None:
        can_mesh = mesh_box_can(
            geometry.box,
            target_size=target,
            min_size=case.mesh.min_size,
            max_size=case.mesh.max_size,
            recombine=case.mesh.recombine,
            curvature_points=case.mesh.curvature_points,
            out_path=outdir / "can.msh",
            enforce=enforce_gate,
            vent_size=case.mesh.vent_size,
            name="CAN",
        )
    else:
        can_mesh = mesh_parametric_can(
            geometry.can,
            target_size=target,
            min_size=case.mesh.min_size,
            max_size=case.mesh.max_size,
            recombine=case.mesh.recombine,
            curvature_points=case.mesh.curvature_points,
            imperfection_mm=case.mesh.imperfection_mm,
            out_path=outdir / "can.msh",
            enforce=enforce_gate,
            name="CAN",
        )

    floor_mesh = mesh_tool(
        geometry.floor,
        can_height=geometry.can.height,
        target_size=rigid_target,
        out_path=outdir / "floor.msh",
        name="FLOOR",
    )
    meshes = {"can": can_mesh, "floor": floor_mesh}
    if geometry.tool is not None:
        meshes["tool"] = mesh_tool(
            geometry.tool,
            can_height=geometry.can.height,
            target_size=rigid_target,
            out_path=outdir / "ref_tool.msh",
            name="REF_TOOL",
        )
    for index, shape in enumerate(geometry.extra_tools):
        name = f"tool{index + 2}"
        meshes[name] = mesh_tool(
            shape,
            can_height=geometry.can.height,
            target_size=rigid_target,
            out_path=outdir / f"{name}.msh",
            name=name.upper(),
        )
    if geometry.support is not None:
        meshes["support"] = mesh_tool(
            geometry.support,
            can_height=geometry.can.height,
            target_size=rigid_target,
            out_path=outdir / "support.msh",
            name="SUPPORT",
        )
    return meshes


def run_pipeline(
    case_path: str | Path,
    *,
    outdir: str | Path | None = None,
    skip_solver: bool = False,
    skip_render: bool = False,
    material_roots: list[Path] | None = None,
    solver_config: str | Path | None = None,
    enforce_gate: bool = True,
    progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    """Run the whole pipeline for one case.

    Args:
        case_path: Path to the case YAML.
        outdir: Output directory; defaults to the case's ``output.dir``.
        skip_solver: Stop after deck generation (still writes a report). Use
            this on machines without OpenRadioss - the report is then marked
            as having no solver stage.
        skip_render: Skip the animation rendering stage.
        material_roots: Extra roots to search for the material card.
        solver_config: Override for ``configs/solver.yaml``.
        enforce_gate: Enforce the §7 mesh gate (ADR-06). Leave this on.
        progress: Optional sink for human-readable progress lines (one per
            stage transition, plus live engine cycle progress). ``csim all``
            passes a console printer; leave ``None`` for silent library use.

    Returns:
        A :class:`PipelineResult` describing every stage.

    Raises:
        CrushSimError: Any stage failure; nothing is swallowed (spec §13.5).
    """
    case = load_case(case_path)
    material = case.material(material_roots)
    run_dir = Path(outdir) if outdir is not None else case.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()

    def emit(message: str) -> None:
        if progress is not None:
            progress(f"[{time.monotonic() - started:6.0f}s] {message}")

    result = PipelineResult(case=case, material=material, run_dir=run_dir)
    if not material.is_verified:
        result.notices.append(
            f"UNVERIFIED MATERIAL: '{material.key}' has verified: false "
            f"(source: {material.source}). Results are trend-only."
        )

    total = 4 if skip_solver else 6
    emit(f"case {case.name} ({case.load_case}) -> {run_dir}")

    emit(f"[1/{total}] geometry")
    result.geometry = build_geometry(case)
    result.stages_completed.append("geometry")

    emit(f"[2/{total}] meshing (target {case.mesh.target_size or MESH_TARGET_SIZE_DEFAULT_MM:g} mm)")
    mesh_dir = run_dir / "mesh"
    result.meshes = build_meshes(case, result.geometry, outdir=mesh_dir, enforce_gate=enforce_gate)
    result.stages_completed.append("meshing")
    for label, mesh_result in result.meshes.items():
        emit(
            f"  {label}: {mesh_result.mesh.n_elements} elements, "
            f"min SICN {mesh_result.quality.min_sicn:.3f}, "
            f"gate {'PASS' if mesh_result.gate.passed else 'FAIL'}"
        )
    seated_offset = result.meshes["can"].mesh.metadata.get("seated_offset_mm")
    if seated_offset is not None:
        lo, hi = result.meshes["can"].mesh.bounding_box()
        note = (
            "STEP geometry auto-seated on the floor: translated by "
            f"({seated_offset[0]:.2f}, {seated_offset[1]:.2f}, {seated_offset[2]:.2f}) mm; "
            f"seated extents {hi[0] - lo[0]:.1f} x {hi[1] - lo[1]:.1f} x "
            f"{hi[2] - lo[2]:.1f} mm, tools placed around them."
        )
        result.notices.append(note)
        emit(f"  {note}")

    # Foil-vent construction: split the stadium region of the cap into its
    # own membrane part (thin foil, own material) welded to the cap through
    # the shared node block.
    can_deck_mesh = result.meshes["can"].mesh
    membrane_mesh = None
    membrane_material = None
    membrane_thickness = None
    membrane_eps = None
    vent_raw = case.geometry.vent or {}
    if result.geometry.box is not None and vent_raw.get("membrane_thickness") is not None:
        from .meshing.mesher import split_vent_membrane  # noqa: PLC0415

        can_deck_mesh, membrane_mesh = split_vent_membrane(can_deck_mesh, result.geometry.box)
        membrane_thickness = float(vent_raw["membrane_thickness"])
        membrane_eps = (
            None if vent_raw.get("eps_p_max") is None else float(vent_raw["eps_p_max"])
        )
        if vent_raw.get("material"):
            membrane_material = load_material_card(
                find_material_card(str(vent_raw["material"]), material_roots)
            )
            if not membrane_material.is_verified:
                result.notices.append(
                    f"UNVERIFIED MATERIAL (vent membrane): '{membrane_material.key}' has "
                    f"verified: false (source: {membrane_material.source}). Results are trend-only."
                )
        emit(
            f"  vent membrane: {membrane_mesh.n_elements} foil elements "
            f"({membrane_mesh.metadata.get('vent_scored_elements', 0)} scored, "
            f"pattern {membrane_mesh.metadata.get('vent_pattern')}), welded to the cap"
        )

    emit(f"[3/{total}] deck")
    solver_cfg = Path(solver_config) if solver_config else case.solver.config
    result.deck = build_deck(
        case,
        material,
        can_mesh=can_deck_mesh,
        membrane_mesh=membrane_mesh,
        membrane_material=membrane_material,
        membrane_thickness=membrane_thickness,
        membrane_eps_p_max=membrane_eps,
        tool_mesh=result.meshes["tool"].mesh if "tool" in result.meshes else None,
        floor_mesh=result.meshes["floor"].mesh,
        support_mesh=result.meshes["support"].mesh if "support" in result.meshes else None,
        extra_tool_meshes=[
            result.meshes[f"tool{i + 2}"].mesh for i in range(len(case.loading.extra_tools))
        ],
        outdir=run_dir / "deck",
        solver_version_tag=_solver_tag(solver_cfg),
    )
    result.stages_completed.append("deck")

    if skip_solver:
        result.notices.append(
            "Solver stage skipped: no OpenRadioss run was performed, so no "
            "solution gate was evaluated. This report covers inputs and meshing only."
        )
    else:
        emit(f"[4/{total}] solver (longest stage - live cycle progress below)")
        result.run = run_solver(
            result.deck.starter_path,
            result.deck.engine_path,
            solver_config=solver_cfg,
            threads=case.solver.threads,
            stop_on_energy_error=case.solver.stop_on_energy_error,
            stop_on_negative_volume=case.solver.stop_on_negative_volume,
            run_name=result.deck.run_name,
            progress=(lambda message: emit(message)) if progress is not None else None,
        )
        result.stages_completed.append("solver")
        emit(f"[5/{total}] post-processing (convert, curves{', render' if not skip_render else ''})")
        result.post = _post_process(result, skip_render=skip_render, solver_config=solver_cfg)
        result.stages_completed.append("post")

    emit(f"[{total}/{total}] report")
    result.report_path = _write_report(result)
    result.stages_completed.append("report")

    (run_dir / "pipeline_summary.json").write_text(
        json.dumps(result.summary(), indent=2, default=str), encoding="utf-8"
    )
    return result


def _solver_tag(solver_config: str | Path) -> str:
    """Read the pinned solver tag, tolerating a missing config file."""
    from .config import load_solver_config  # noqa: PLC0415 - avoids a cycle at import time

    try:
        return load_solver_config(solver_config).version_tag
    except CrushSimError:
        return "unpinned"


def _post_process(
    result: PipelineResult,
    *,
    skip_render: bool,
    solver_config: str | Path,
) -> dict[str, Any]:
    """Convert results, build the curve and render the animation (FR-06/07)."""
    from .post.convert import convert_animation, convert_time_history, find_result_files
    from .post.curves import (
        compute_metrics,
        extract_energy_balance,
        extract_force_displacement,
        plot_force_displacement,
        read_th_csv,
    )

    assert result.deck is not None
    run_dir = result.deck.starter_path.parent
    found = find_result_files(run_dir, result.deck.run_name)
    out: dict[str, Any] = {"raw": {k: [str(p) for p in v] for k, v in found.items()}}

    if not found["time_history"]:
        raise SolverError(
            f"The run produced no time-history file in {run_dir}. "
            "Check /TFILE in the engine deck and the solver log."
        )
    conversion = convert_time_history(found["time_history"][0], solver_config=solver_config)
    out["th_conversion"] = conversion.to_dict()

    frame = read_th_csv(conversion.outputs[0])
    has_tool = any(p.role == "tool" for p in result.deck.parts)
    curve = metrics = None
    if has_tool:
        curve = extract_force_displacement(frame)
        metrics = compute_metrics(curve)
        out["metrics"] = metrics.to_dict()
    else:
        result.notices.append(
            "Pressure-driven case: no tool force-displacement curve; see the "
            "time-history energies and the animation for the response."
        )

    engine_log = result.run.engine_log if result.run else None
    # The energy error is computed from the time-history balance (all tracked
    # energies vs external work): the engine console's ERROR column omits
    # interface friction energy and over-reports on frictional cases.
    energy = extract_energy_balance(
        frame,
        added_mass_ratio=(engine_log.max_mass_error or 0.0) if engine_log else 0.0,
    )
    out["energy"] = energy.to_dict()

    if curve is not None:
        curve_csv = result.run_dir / "force_displacement.csv"
        curve.to_csv(curve_csv, index=False)
        png = plot_force_displacement(
            curve,
            result.run_dir / "force_displacement.png",
            metrics=metrics,
            title_text=f"{result.case.name} - {result.case.load_case}",
            unverified=not result.material.is_verified,
        )
        out["curve_csv"] = str(curve_csv)
        out["curve_png"] = str(png)

    if found["animation"] and not skip_render:
        anim = convert_animation(
            list(found["animation"]),
            outdir=result.run_dir / "vtk",
            solver_config=solver_config,
        )
        out["anim_conversion"] = anim.to_dict()
        # The render runs in a subprocess: VTK's clip filter intermittently
        # aborts natively (free(): invalid pointer) on the first render of a
        # freshly converted sequence, which no Python except can catch - in
        # process it killed the whole post/report stage. A crashed render is
        # retried once (the second pass reliably succeeds), and a still-failed
        # render degrades to a note instead of losing the report.
        out["render"] = _render_in_subprocess(result.run_dir)

    # Vent milestones, cached here so the UI and the reports read them from the
    # summary instead of re-parsing a multi-megabyte deck on every poll.
    if result.case.pressure.peak > 0.0:
        try:
            from .post.vent_metrics import vent_metrics  # noqa: PLC0415

            peak, rise = result.case.pressure.peak, result.case.pressure.rise
            metrics_v = vent_metrics(result.run_dir)
            if metrics_v is not None:
                def _mpa(t: float | None) -> float | None:
                    return None if t is None else peak * min(t / rise, 1.0) if rise > 0 else peak

                out["vent"] = {
                    "initiation_MPa": _mpa(metrics_v["t_initiation_s"]),
                    "opening_MPa": _mpa(metrics_v["t_opening_s"]),
                    "opening_area_fraction": metrics_v["opening_area_fraction"],
                    "vent_area_mm2": metrics_v["vent_area_mm2"],
                    "score_elements": metrics_v["score_elements"],
                    "score_ruptured": metrics_v["score_ruptured"],
                }
        except Exception:  # noqa: BLE001 - metrics are an extra, never a blocker
            pass
    return out


def _render_in_subprocess(run_dir: Path) -> dict[str, Any]:
    """Run ``csim render --rundir`` isolated from native VTK crashes."""
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    command = [sys.executable, "-m", "crushsim", "render", "--rundir", str(run_dir)]
    for attempt in (1, 2):
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            anim = run_dir / "anim"
            return {
                "videos": {p.stem: str(p) for p in sorted(anim.glob("*.mp4"))},
                "gifs": {p.stem: str(p) for p in sorted(anim.glob("*.gif"))},
                "stills": {p.stem: str(p) for p in sorted(anim.glob("*_last.png"))},
                "attempts": attempt,
            }
    return {
        "skipped_reason": (
            f"render subprocess failed twice (exit {proc.returncode}); "
            "re-run 'csim render --rundir' manually"
        ),
        "attempts": 2,
    }


def _write_report(result: PipelineResult) -> Path:
    """Render the HTML report for a pipeline run (FR-08)."""
    artifacts: dict[str, Path] = {}
    if result.deck is not None:
        artifacts["starter_deck"] = result.deck.starter_path
        artifacts["engine_deck"] = result.deck.engine_path
    for key in ("curve_png", "curve_csv"):
        if key in result.post:
            artifacts[key] = Path(result.post[key])
    render = result.post.get("render") or {}
    for name, path in (render.get("videos") or {}).items():
        artifacts[f"video_{name}"] = Path(path)
    for name, path in (render.get("gifs") or {}).items():
        artifacts[f"gif_{name}"] = Path(path)

    gates = list(result.gates)
    energy = result.post.get("energy")
    if energy and isinstance(energy.get("gate"), dict):
        from .meshing.gates import GateMetric, GateResult as _GateResult  # noqa: PLC0415

        raw = energy["gate"]
        gates.append(
            _GateResult(
                name=raw["name"],
                metrics=[
                    GateMetric(
                        name=m["name"],
                        value=m["value"],
                        limit=m["limit"],
                        mode=m["mode"],
                        unit=m.get("unit", ""),
                        recommendation=m.get("recommendation", ""),
                    )
                    for m in raw["metrics"]
                ],
                info=raw.get("info", {}),
            )
        )

    context = build_context(
        case=result.case,
        material=result.material,
        geometry=result.geometry.summary() if result.geometry else {},
        mesh_summaries=[m.mesh.summary() for m in result.meshes.values()],
        gates=gates,
        metrics=result.post.get("metrics"),
        energy=energy,
        deck=result.deck.summary() if result.deck else None,
        solver=result.run.to_dict() if result.run else None,
        artifacts=artifacts,
        report_dir=result.run_dir,
        notices=result.notices,
    )
    return render_report(context, result.run_dir / "report.html")
