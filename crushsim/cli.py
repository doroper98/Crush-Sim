"""``csim`` - the command-line interface (spec §6, ADR-03).

Phase 0-2 expose no UI beyond this CLI and the HTML report. Every module is
runnable on its own (spec §13.5), and every failure surfaces as a non-zero exit
code with a one-sentence cause.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from . import __version__
from .errors import CrushSimError
from .units import MESH_TARGET_SIZE_DEFAULT_MM, UNIT_SYSTEM

app = typer.Typer(
    name="csim",
    help="Crush-Sim: can crush/strength simulation pipeline (Gmsh -> OpenRadioss -> report).",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_LEGACY_MATERIAL_PATH = Path("legacy_ref/src/engine/MaterialModel.ts")
DEFAULT_HARVEST_OUTDIR = Path("configs/materials/harvested")


def _fail(exc: Exception) -> None:
    """Print an error and exit non-zero (failures are loud, spec §13.5)."""
    typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _progress_printer(message: str) -> None:
    """Console sink for pipeline/solver progress lines.

    Explicitly flushed: the solver stage takes tens of minutes and users on
    Windows consoles must see progress as it happens, not on exit.
    """
    typer.secho(message, fg=typer.colors.CYAN)
    sys.stdout.flush()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="Print the crushsim version and exit.")
    ] = False,
) -> None:
    """Crush-Sim command-line interface."""
    if version:
        typer.echo(f"crushsim {__version__} (units: {UNIT_SYSTEM})")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


# ---------------------------------------------------------------------------
# harvest (FR-10)
# ---------------------------------------------------------------------------


@app.command()
def harvest(
    input: Annotated[
        Path, typer.Option("--input", "-i", help="Legacy src/engine/MaterialModel.ts")
    ] = DEFAULT_LEGACY_MATERIAL_PATH,
    outdir: Annotated[
        Path, typer.Option("--outdir", "-o", help="Directory receiving the YAML cards")
    ] = DEFAULT_HARVEST_OUTDIR,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing cards")] = True,
) -> None:
    """Harvest the legacy material library into YAML cards (all unverified)."""
    from .harvest.legacy_ts import harvest_file

    try:
        report = harvest_file(input, outdir, overwrite=overwrite)
    except CrushSimError as exc:
        _fail(exc)
        return
    typer.secho(
        f"Harvested {report.count} material cards from {report.source} into {outdir}",
        fg=typer.colors.GREEN,
    )
    typer.echo("All cards are tagged verified: false (spec §4 FR-10).")


# ---------------------------------------------------------------------------
# convert (FR-01, Windows + CATIA V5 only)
# ---------------------------------------------------------------------------


@app.command()
def convert(
    indir: Annotated[
        Path, typer.Option("--indir", "-i", help="Folder with CATPart/CATProduct files")
    ],
    outdir: Annotated[
        Path, typer.Option("--outdir", "-o", help="Folder receiving the STEP files")
    ],
    pattern: Annotated[
        Optional[list[str]],
        typer.Option(
            "--pattern",
            "-p",
            help="Glob pattern(s) to convert; repeatable. Default: *.CATPart and *.CATProduct",
        ),
    ] = None,
) -> None:
    """Batch-convert CATPart/CATProduct files to STEP AP214 via CATIA V5 COM.

    Windows only, and CATIA V5 must be installed. One failing file does not
    stop the batch; every outcome lands in conversion_log.csv (spec FR-01).
    """
    from .converter.catia import DEFAULT_PATTERNS, batch_convert

    try:
        records = batch_convert(
            indir, outdir, patterns=tuple(pattern) if pattern else DEFAULT_PATTERNS
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    counts = {status: sum(1 for r in records if r.status == status) for status in ("ok", "failed")}
    for record in records:
        color = typer.colors.GREEN if record.status == "ok" else typer.colors.RED
        typer.secho(f"  [{record.status}] {record.source} -> {record.target}", fg=color)
        if record.error:
            typer.secho(f"        {record.error}", fg=typer.colors.RED)
    if not records:
        typer.secho(f"No CATIA files matched in {indir}", fg=typer.colors.YELLOW)
        return
    typer.secho(
        f"Converted {counts['ok']}/{len(records)} files "
        f"({counts['failed']} failed). Log: {Path(outdir) / 'conversion_log.csv'}",
        fg=typer.colors.RED if counts["failed"] else typer.colors.GREEN,
    )
    if counts["failed"]:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# mesh (FR-03)
# ---------------------------------------------------------------------------


@app.command()
def mesh(
    config: Annotated[Optional[Path], typer.Option("--config", "-c", help="Case YAML")] = None,
    step: Annotated[Optional[Path], typer.Option("--step", help="Mesh a STEP file instead")] = None,
    radius: Annotated[float, typer.Option(help="Can radius [mm]")] = 33.0,
    height: Annotated[float, typer.Option(help="Can height [mm]")] = 115.0,
    thickness: Annotated[float, typer.Option(help="Wall thickness [mm]")] = 0.1,
    target_size: Annotated[
        float, typer.Option("--target-size", help="Target element size [mm]")
    ] = MESH_TARGET_SIZE_DEFAULT_MM,
    outdir: Annotated[Path, typer.Option("--outdir", "-o", help="Output directory")] = Path(
        "runs/mesh"
    ),
    no_gate: Annotated[
        bool, typer.Option("--no-gate", help="Report the gate but do not enforce it")
    ] = False,
) -> None:
    """Mesh a parametric can, a case, or a STEP file, and run the §7 mesh gate."""
    from .config import load_case
    from .meshing.mesher import mesh_parametric_can, mesh_step_surfaces
    from .pipeline import build_geometry, build_meshes

    try:
        outdir.mkdir(parents=True, exist_ok=True)
        if config is not None:
            case = load_case(config)
            geometry = build_geometry(case)
            results = build_meshes(case, geometry, outdir=outdir, enforce_gate=not no_gate)
            payload = {name: r.summary() for name, r in results.items()}
        elif step is not None:
            result = mesh_step_surfaces(
                step,
                target_size=target_size,
                out_path=outdir / f"{Path(step).stem}.msh",
                enforce=not no_gate,
            )
            payload = {"step": result.summary()}
        else:
            from .geometry.parametric import make_can

            result = mesh_parametric_can(
                make_can(radius, height, thickness),
                target_size=target_size,
                out_path=outdir / "can.msh",
                enforce=not no_gate,
            )
            payload = {"can": result.summary()}
    except CrushSimError as exc:
        _fail(exc)
        return

    for name, summary in payload.items():
        gate = summary["gate"]
        colour = typer.colors.GREEN if gate["passed"] else typer.colors.RED
        typer.secho(
            f"{name}: {summary['mesh']['elements']} elements "
            f"({summary['mesh']['quads']} quads / {summary['mesh']['tris']} tris), "
            f"gate {'PASS' if gate['passed'] else 'FAIL'}",
            fg=colour,
        )
    (outdir / "mesh_summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(f"Wrote {outdir / 'mesh_summary.json'}")


# ---------------------------------------------------------------------------
# deck (FR-04)
# ---------------------------------------------------------------------------


@app.command()
def deck(
    config: Annotated[Path, typer.Option("--config", "-c", help="Case YAML")],
    outdir: Annotated[
        Optional[Path], typer.Option("--outdir", "-o", help="Output directory")
    ] = None,
) -> None:
    """Generate the OpenRadioss starter/engine decks for a case."""
    from .config import load_case
    from .deck.writer import build_deck
    from .pipeline import _solver_tag, build_geometry, build_meshes

    try:
        case = load_case(config)
        run_dir = Path(outdir) if outdir else case.run_dir
        geometry = build_geometry(case)
        meshes = build_meshes(case, geometry, outdir=run_dir / "mesh")
        result = build_deck(
            case,
            case.material(),
            can_mesh=meshes["can"].mesh,
            tool_mesh=meshes["tool"].mesh if "tool" in meshes else None,
            floor_mesh=meshes["floor"].mesh,
            support_mesh=meshes["support"].mesh if "support" in meshes else None,
            extra_tool_meshes=[
                meshes[f"tool{i + 2}"].mesh for i in range(len(case.loading.extra_tools))
            ],
            outdir=run_dir / "deck",
            solver_version_tag=_solver_tag(case.solver.config),
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    typer.secho(f"Starter: {result.starter_path}", fg=typer.colors.GREEN)
    typer.secho(f"Engine:  {result.engine_path}", fg=typer.colors.GREEN)
    typer.secho(f"NOTE: {result.warning}", fg=typer.colors.YELLOW)


# ---------------------------------------------------------------------------
# run (FR-05)
# ---------------------------------------------------------------------------


@app.command()
def run(
    config: Annotated[Optional[Path], typer.Option("--config", "-c", help="Case YAML")] = None,
    starter: Annotated[Optional[Path], typer.Option("--starter", help="Starter deck")] = None,
    engine: Annotated[Optional[Path], typer.Option("--engine", help="Engine deck")] = None,
    threads: Annotated[int, typer.Option(help="OpenMP threads for the engine")] = 4,
    solver_config: Annotated[
        Path, typer.Option("--solver-config", help="Pinned solver configuration")
    ] = Path("configs/solver.yaml"),
) -> None:
    """Run the pinned OpenRadioss starter and engine on an existing deck."""
    from .config import load_case
    from .solver.runner import run_solver

    try:
        if starter is None or engine is None:
            if config is None:
                raise CrushSimError("Provide --config, or both --starter and --engine")
            case = load_case(config)
            deck_dir = case.run_dir / "deck"
            starter = starter or deck_dir / f"{case.name}_0000.rad"
            engine = engine or deck_dir / f"{case.name}_0001.rad"
            threads = case.solver.threads
            solver_config = case.solver.config
        result = run_solver(
            starter,
            engine,
            solver_config=solver_config,
            threads=threads,
            progress=_progress_printer,
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    typer.secho(f"Run complete: {result.summary_path}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# status - human-readable view of a (running) solver job
# ---------------------------------------------------------------------------


@app.command()
def status(
    config: Annotated[Optional[Path], typer.Option("--config", "-c", help="Case YAML")] = None,
    rundir: Annotated[
        Optional[Path], typer.Option("--rundir", help="Run output directory (instead of --config)")
    ] = None,
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Refresh every 10 s until the run completes")
    ] = False,
) -> None:
    """Show solver progress in plain terms: percent done, energy error, ETA.

    Reads the engine's listing file the same way the live ``csim all``
    progress does, so it works from a second console while a run is going.
    """
    import time as _time

    from .config import load_case
    from .solver.runner import _last_cycle_line, read_engine_end_time

    try:
        if rundir is None:
            if config is None:
                raise CrushSimError("Provide --config or --rundir")
            case = load_case(config)
            rundir = case.run_dir
        deck_dir = Path(rundir) / "deck"
        if not deck_dir.is_dir():
            raise CrushSimError(f"No deck directory in {rundir} - has the run started?")
        engine_decks = sorted(deck_dir.glob("*_0001.rad"))
        if not engine_decks:
            raise CrushSimError(f"No engine deck (*_0001.rad) in {deck_dir}")
        run_name = engine_decks[-1].stem.removesuffix("_0001")
        end_time = read_engine_end_time(engine_decks[-1])
    except CrushSimError as exc:
        _fail(exc)
        return

    def sample() -> tuple[int, float, str | None] | None:
        parsed = None
        for out_file in sorted(deck_dir.glob(f"{run_name}_*.out")):
            try:
                text = out_file.read_text(errors="replace")[-6000:]
            except OSError:
                continue
            parsed = _last_cycle_line(text) or parsed
        return parsed

    def describe(
        current: tuple[int, float, str | None] | None,
        previous: tuple[int, float, str | None] | None,
        wall_elapsed: float,
    ) -> str:
        if current is None:
            return f"{run_name}: no engine cycles yet (meshing / starter stage, or not started)"
        cycle, sim_time, error = current
        parts = [run_name + ":"]
        if end_time:
            parts.append(f"{min(100.0, 100.0 * sim_time / end_time):5.1f}% done")
            parts.append(f"(simulated {sim_time:.4e} of {end_time:.4e} s)")
        else:
            parts.append(f"simulated {sim_time:.4e} s")
        parts.append(f"cycle {cycle:,}")
        if error is not None:
            parts.append(f"energy error {error} (limit 5%)")
        animations = len(list(deck_dir.glob(f"{run_name}A[0-9]*")))
        if animations:
            parts.append(f"{animations} animation frames")
        if previous is not None and end_time and wall_elapsed > 0.0:
            rate = (current[1] - previous[1]) / wall_elapsed
            if rate > 0.0:
                eta_s = (end_time - current[1]) / rate
                parts.append(f"ETA ~{eta_s / 60.0:.0f} min" if eta_s > 90.0 else f"ETA ~{eta_s:.0f} s")
        return "  ".join(parts)

    summary_file = deck_dir / "run_summary.json"
    previous = sample()
    previous_wall = _time.monotonic()
    if summary_file.is_file() and not watch:
        typer.secho(describe(previous, None, 0.0), fg=typer.colors.CYAN)
        typer.secho(f"Run complete: {summary_file}", fg=typer.colors.GREEN)
        return

    interval = 10.0 if watch else 4.0
    while True:
        _time.sleep(interval)
        current = sample()
        now = _time.monotonic()
        typer.secho(describe(current, previous, now - previous_wall), fg=typer.colors.CYAN)
        sys.stdout.flush()
        previous, previous_wall = current or previous, now
        if summary_file.is_file():
            typer.secho(f"Run complete: {summary_file}", fg=typer.colors.GREEN)
            return
        if not watch:
            return


# ---------------------------------------------------------------------------
# post (FR-06/07)
# ---------------------------------------------------------------------------


@app.command()
def post(
    csv: Annotated[
        Optional[Path], typer.Option("--csv", help="Converted time-history CSV")
    ] = None,
    config: Annotated[Optional[Path], typer.Option("--config", "-c", help="Case YAML")] = None,
    outdir: Annotated[
        Optional[Path], typer.Option("--outdir", "-o", help="Output directory")
    ] = None,
) -> None:
    """Analyse a converted time-history CSV: curve, peak load, absorbed energy."""
    from .config import load_case
    from .post.curves import (
        compute_metrics,
        extract_force_displacement,
        plot_force_displacement,
        read_th_csv,
    )

    try:
        if csv is None:
            raise CrushSimError(
                "Provide --csv pointing at a th_to_csv output. Raw solver binaries "
                "are never parsed in-house (spec §13.4)."
            )
        case = load_case(config) if config else None
        out = Path(outdir) if outdir else (case.run_dir if case else Path("runs/post"))
        out.mkdir(parents=True, exist_ok=True)
        frame = read_th_csv(csv)
        curve = extract_force_displacement(frame)
        metrics = compute_metrics(curve)
        curve.to_csv(out / "force_displacement.csv", index=False)
        png = plot_force_displacement(
            curve,
            out / "force_displacement.png",
            metrics=metrics,
            unverified=bool(case and not case.material().is_verified),
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    _echo_json(metrics.to_dict())
    typer.secho(f"Curve: {png}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# render - re-render an existing run's animation (FR-07)
# ---------------------------------------------------------------------------


@app.command()
def render(
    config: Annotated[Optional[Path], typer.Option("--config", "-c", help="Case YAML")] = None,
    rundir: Annotated[
        Optional[Path], typer.Option("--rundir", help="Run output directory (instead of --config)")
    ] = None,
) -> None:
    """Re-render an existing run's animation without re-solving.

    Uses the run's converted VTK sequence; the videos get the synced
    force-displacement panel and semi-transparent rigid tools when the run's
    curve and summary are present.
    """
    from .post.curves import read_th_csv
    from .post.render import render_sequence

    try:
        if rundir is None:
            if config is None:
                raise CrushSimError("Provide --config or --rundir")
            from .config import load_case

            rundir = load_case(config).run_dir
        run_dir = Path(rundir)
        vtk_files = sorted((run_dir / "vtk").glob("*.vt*"))
        if not vtk_files:
            raise CrushSimError(
                f"No converted VTK files in {run_dir / 'vtk'}. Run `csim all` first "
                "so the solver's animation output exists and is converted."
            )
        curve = None
        curve_csv = run_dir / "force_displacement.csv"
        if curve_csv.is_file():
            curve = read_th_csv(curve_csv)
        rigid_ids: list[int] = []
        summary_path = run_dir / "pipeline_summary.json"
        if summary_path.is_file():
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            for part in (payload.get("deck") or {}).get("parts") or []:
                if part.get("role") in ("floor", "tool"):
                    rigid_ids.append(int(part["part_id"]))
        result = render_sequence(
            vtk_files,
            run_dir / "anim",
            strict=True,
            curve=curve,
            rigid_part_ids=rigid_ids or None,
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    for name, path in result.videos.items():
        typer.secho(f"Video: {path}", fg=typer.colors.GREEN)
    for name, path in result.gifs.items():
        typer.secho(f"GIF:   {path}", fg=typer.colors.GREEN)
    if result.skipped_reason:
        typer.secho(f"NOTE: {result.skipped_reason}", fg=typer.colors.YELLOW)


# ---------------------------------------------------------------------------
# report (FR-08)
# ---------------------------------------------------------------------------


@app.command()
def report(
    config: Annotated[Path, typer.Option("--config", "-c", help="Case YAML")],
    outdir: Annotated[
        Optional[Path], typer.Option("--outdir", "-o", help="Output directory")
    ] = None,
) -> None:
    """Render the HTML report for a case using whatever artefacts exist."""
    from .config import load_case
    from .report.builder import build_context, render_report

    try:
        case = load_case(config)
        run_dir = Path(outdir) if outdir else case.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        material = case.material()
        artifacts: dict[str, Path] = {}
        for name, candidate in (
            ("curve_png", run_dir / "force_displacement.png"),
            ("curve_csv", run_dir / "force_displacement.csv"),
            ("starter_deck", run_dir / "deck" / f"{case.name}_0000.rad"),
            ("engine_deck", run_dir / "deck" / f"{case.name}_0001.rad"),
        ):
            if candidate.exists():
                artifacts[name] = candidate
        context = build_context(
            case=case,
            material=material,
            artifacts=artifacts,
            report_dir=run_dir,
            notices=["Report generated from existing artefacts (csim report)."],
        )
        path = render_report(context, run_dir / "report.html")
    except CrushSimError as exc:
        _fail(exc)
        return
    typer.secho(f"Report: {path}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------


@app.command(name="all")
def run_all(
    config: Annotated[Path, typer.Option("--config", "-c", help="Case YAML")],
    outdir: Annotated[
        Optional[Path], typer.Option("--outdir", "-o", help="Output directory")
    ] = None,
    skip_solver: Annotated[
        bool,
        typer.Option(
            "--skip-solver",
            help="Stop after deck generation (machines without OpenRadioss installed).",
        ),
    ] = False,
    skip_render: Annotated[bool, typer.Option("--skip-render", help="Skip animation")] = False,
) -> None:
    """Run the whole pipeline for one case (CATPart conversion excluded)."""
    from .pipeline import run_pipeline

    try:
        result = run_pipeline(
            config,
            outdir=outdir,
            skip_solver=skip_solver,
            skip_render=skip_render,
            progress=_progress_printer,
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    typer.secho(
        f"Completed stages: {', '.join(result.stages_completed)}", fg=typer.colors.GREEN
    )
    for notice in result.notices:
        typer.secho(f"NOTE: {notice}", fg=typer.colors.YELLOW)
    if result.report_path:
        typer.secho(f"Report: {result.report_path}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# export-step (issue #26): deformed shape at any frame -> STEP
# ---------------------------------------------------------------------------


@app.command(name="export-step")
def export_step(
    rundir: Annotated[Path, typer.Option("--rundir", help="Completed run directory")],
    frame: Annotated[
        Optional[int],
        typer.Option("--frame", help="Frame index (negative counts from the end; default last)"),
    ] = None,
    time_s: Annotated[
        Optional[float], typer.Option("--time", help="Pick the frame nearest this time [s]")
    ] = None,
    part: Annotated[
        str, typer.Option("--part", help="'can' (default), 'all', or a deck part name")
    ] = "can",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Output .stp")] = None,
) -> None:
    """Export the deformed shape at a frame as STEP (faceted shell, AP214).

    Reads the run's converted VTK sequence; needs the 'cad' extra (OCP).
    """
    from .post.export_step import export_deformed_step

    try:
        path = export_deformed_step(
            rundir, frame=frame, time_s=time_s, part=part, out_path=output
        )
    except CrushSimError as exc:
        _fail(exc)
        return
    typer.secho(f"STEP: {path}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# ui (FR-11, Phase 3)
# ---------------------------------------------------------------------------


@app.command()
def ui(
    root: Annotated[
        Path, typer.Option("--root", help="Crush-Sim checkout to serve (configs/, runs/)")
    ] = Path("."),
    host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8384,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser tab")
    ] = False,
) -> None:
    """Start the local web UI: case launcher, run monitor, results viewer."""
    try:
        from .ui.server import serve
    except ImportError as exc:
        _fail(
            CrushSimError(
                f"The UI needs the 'ui' extra ({exc.name} missing). "
                "Install it with: pip install -e .[ui]"
            )
        )
        return
    typer.secho(f"Crush-Sim UI: http://{host}:{port}/  (Ctrl+C to stop)", fg=typer.colors.GREEN)
    serve(root, host=host, port=port, open_browser=not no_browser)


# ---------------------------------------------------------------------------
# doctor (Phase 0, Day-1 gate)
# ---------------------------------------------------------------------------


def _check_python() -> tuple[bool, str]:
    version = sys.version_info
    ok = version >= (3, 11)
    return ok, f"{version.major}.{version.minor}.{version.micro} (requires >= 3.11)"


def _check_import(module: str) -> tuple[bool, str]:
    import importlib

    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return False, str(exc).splitlines()[0]
    return True, str(getattr(mod, "__version__", "present"))


def _check_gmsh_mesh() -> tuple[bool, str]:
    try:
        from .geometry.parametric import make_can
        from .meshing.mesher import mesh_parametric_can

        result = mesh_parametric_can(
            make_can(20.0, 40.0, 0.3), target_size=8.0, enforce=False, name="doctor"
        )
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return False, str(exc).splitlines()[0]
    return True, f"{result.mesh.n_elements} elements, min SICN {result.quality.min_sicn:.3f}"


def _check_solver(solver_config: Path) -> list[tuple[str, bool, str]]:
    from .solver.runner import solver_status

    status = solver_status(solver_config)
    if not status.get("configured"):
        return [("solver config", False, str(status.get("reason", "not configured")))]
    rows: list[tuple[str, bool, str]] = [
        ("solver version tag", True, str(status.get("version_tag")))
    ]
    for kind in ("starter", "engine"):
        value = status.get(kind)
        rows.append(
            (
                f"solver {kind}",
                value is not None,
                str(value) if value else str(status.get(f"{kind}_reason", "not found")),
            )
        )
    return rows


@app.command()
def doctor(
    solver_config: Annotated[
        Path, typer.Option("--solver-config", help="Pinned solver configuration")
    ] = Path("configs/solver.yaml"),
) -> None:
    """Check the environment: Phase-0 Day-1 gate (spec §9).

    Core checks (Python, Gmsh, rendering) gate the exit code. Solver and CAD
    checks are reported but never fail the command, so this runs anywhere.
    """
    from .geometry.step_inspect import ocp_available
    from .post.render import ffmpeg_available, rendering_available

    rows: list[tuple[str, bool, str, bool]] = []  # name, ok, detail, is_core

    ok, detail = _check_python()
    rows.append(("python", ok, detail, True))
    for module in ("numpy", "pandas", "yaml", "typer", "jinja2", "matplotlib", "gmsh", "pyvista"):
        ok, detail = _check_import(module)
        rows.append((f"import {module}", ok, detail, module not in ("pyvista",)))

    ok, detail = _check_gmsh_mesh()
    rows.append(("gmsh mesh smoke test", ok, detail, True))

    ok, detail = rendering_available()
    rows.append(("pyvista offscreen render", ok, detail, False))

    ok, detail = ffmpeg_available()
    rows.append(("ffmpeg (imageio)", ok, detail, False))

    rows.append(
        (
            "OCP (cad extra)",
            ocp_available(),
            "present" if ocp_available() else "pip install crushsim[cad]",
            False,
        )
    )

    from .converter.catia import catia_available

    ok, detail = catia_available()
    rows.append(("CATIA COM (windows extra)", ok, detail, False))

    for name, ok, detail, core in _core_rows(_check_solver(solver_config)):
        rows.append((name, ok, detail, core))

    width = max(len(name) for name, *_ in rows) + 2
    typer.echo(f"crushsim {__version__} doctor - units {UNIT_SYSTEM}")
    typer.echo("-" * (width + 60))
    failed_core = 0
    for name, ok, detail, core in rows:
        mark = "PASS" if ok else ("FAIL" if core else "warn")
        colour = typer.colors.GREEN if ok else (typer.colors.RED if core else typer.colors.YELLOW)
        typer.secho(f"{name:<{width}} {mark:<5} {detail[:150]}", fg=colour)
        if core and not ok:
            failed_core += 1
    typer.echo("-" * (width + 60))
    if failed_core:
        typer.secho(f"{failed_core} core check(s) failed.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("All core checks passed.", fg=typer.colors.GREEN)


def _core_rows(rows: list[tuple[str, bool, str]]) -> list[tuple[str, bool, str, bool]]:
    """Mark solver rows as non-core so ``doctor`` still exits 0 without a solver."""
    return [(name, ok, detail, False) for name, ok, detail in rows]


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
