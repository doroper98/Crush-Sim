"""FR-11 - FastAPI backend for the local web UI.

Three surfaces, matching the spec §10 screens:

* ``/api/cases``   - the case library (configs/cases/*.yaml, parsed).
* ``/api/runs``    - launch and monitor pipeline runs (``csim all`` in a
  subprocess per run; progress is read from the live log).
* ``/api/runs/{name}/…`` - artefacts of a finished run: report, curve,
  videos, and the standalone interactive viewer (generated on demand).

The server only ever launches the same CLI a user would run by hand - the
UI is a front-end to the pipeline, never a second implementation of it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_case

_STATIC = Path(__file__).parent / "static"
_PROGRESS = re.compile(r"engine\s+([0-9.]+)%.*?energy error\s+(-?[0-9.]+)%")
_STAGE = re.compile(r"\[(\d)/6\]\s+(\S+)")
_CASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*\.yaml")


@dataclass
class ActiveRun:
    """One pipeline subprocess started from the UI."""

    case_name: str
    process: subprocess.Popen
    log_path: Path
    run_dir: Path | None = None
    finished: bool = False
    returncode: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def poll(self) -> None:
        code = self.process.poll()
        if code is not None:
            self.finished = True
            self.returncode = code


def create_app(root: str | Path = ".") -> FastAPI:
    """Build the FastAPI app rooted at a Crush-Sim checkout."""
    base = Path(root).resolve()
    cases_dir = base / "configs" / "cases"
    runs_dir = base / "runs"
    ui_logs = runs_dir / "_ui_logs"
    active: dict[str, ActiveRun] = {}

    app = FastAPI(title="Crush-Sim UI", docs_url=None, redoc_url=None)

    @app.get("/api/cases")
    def list_cases() -> list[dict[str, Any]]:
        out = []
        for path in sorted(cases_dir.glob("*.yaml")):
            if path.name.startswith("."):  # a save_case validation probe
                continue
            try:
                case = load_case(path)
                out.append(
                    {
                        "file": path.name,
                        "name": case.name,
                        "load_case": case.load_case,
                        "description": case.description,
                        "geometry": case.geometry.kind,
                        "step_path": str(case.geometry.step_path or ""),
                        "tool": case.loading.tool,
                        "stroke_mm": case.loading.stroke,
                        "material": case.material_key or str(case.material_path or ""),
                        "outdir": str(case.output.dir),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - a broken yaml must not hide the rest
                out.append({"file": path.name, "name": path.stem, "error": str(exc)})
        return out

    def _case_path_or_404(case_file: str) -> Path:
        if not _CASE_NAME.fullmatch(case_file):
            raise HTTPException(404, f"case not found: {case_file}")
        return cases_dir / case_file

    @app.get("/api/cases/{case_file}/raw")
    def case_raw(case_file: str) -> dict[str, Any]:
        """The case yaml as a plain mapping - the workflow editor's node values."""
        path = _case_path_or_404(case_file)
        if not path.is_file():
            raise HTTPException(404, f"case not found: {case_file}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise HTTPException(409, f"unparseable yaml: {exc}") from exc
        if not isinstance(data, dict):
            raise HTTPException(409, "case yaml is not a mapping")
        return data

    @app.put("/api/cases/{case_file}")
    def save_case(case_file: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Save a workflow-editor graph back to configs/cases/.

        The payload is validated by the same ``load_case`` the pipeline uses -
        an invalid graph is rejected with the loader's own message and the
        file on disk is left untouched.
        """
        path = _case_path_or_404(case_file)
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        probe = cases_dir / f".{path.stem}.probe.yaml"
        cases_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text(text, encoding="utf-8")
        try:
            load_case(probe)
        except Exception as exc:  # noqa: BLE001 - loader message goes to the editor
            raise HTTPException(422, str(exc)) from exc
        finally:
            probe.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"saved": case_file}

    def _run_status(run: ActiveRun) -> dict[str, Any]:
        run.poll()
        tail = ""
        progress: float | None = None
        energy: float | None = None
        stage = None
        if run.log_path.is_file():
            text = run.log_path.read_text(encoding="utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if "WARN|" not in ln]
            tail = "\n".join(lines[-25:])
            for line in reversed(lines):
                m = _PROGRESS.search(line)
                if m:
                    progress = float(m.group(1))
                    energy = float(m.group(2))
                    break
            for line in reversed(lines):
                m = _STAGE.search(line)
                if m:
                    stage = f"{m.group(1)}/6 {m.group(2)}"
                    break
        return {
            "case": run.case_name,
            "running": not run.finished,
            "returncode": run.returncode,
            "stage": stage,
            "engine_progress_pct": progress,
            "console_energy_error_pct": energy,
            "log_tail": tail,
        }

    @app.post("/api/runs/{case_file}")
    def start_run(case_file: str) -> dict[str, Any]:
        path = cases_dir / case_file
        if not path.is_file():
            raise HTTPException(404, f"case not found: {case_file}")
        existing = active.get(case_file)
        if existing is not None and not existing.finished:
            existing.poll()
            if not existing.finished:
                raise HTTPException(409, f"{case_file} is already running")
        ui_logs.mkdir(parents=True, exist_ok=True)
        log_path = ui_logs / f"{path.stem}.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "crushsim", "all", "-c", str(path)],
                cwd=base,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        case = load_case(path)
        active[case_file] = ActiveRun(
            case_name=case.name,
            process=process,
            log_path=log_path,
            run_dir=base / case.output.dir,
        )
        return {"started": case_file, "pid": process.pid}

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        finished = []
        if runs_dir.is_dir():
            for summary in sorted(runs_dir.glob("*/pipeline_summary.json")):
                try:
                    data = json.loads(summary.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 - a broken run must not hide the rest
                    continue
                post = data.get("post") or {}
                metrics = post.get("metrics") or {}
                energy = post.get("energy") or {}
                finished.append(
                    {
                        "name": summary.parent.name,
                        "case": data.get("case"),
                        "load_case": data.get("load_case"),
                        "stages_completed": data.get("stages_completed"),
                        "peak_load_N": metrics.get("peak_load_N"),
                        "absorbed_energy_mJ": metrics.get("absorbed_energy_mJ"),
                        "energy_error": energy.get("energy_error"),
                        "gate_passed": (energy.get("gate") or {}).get("passed"),
                        "report": bool((summary.parent / "report.html").is_file()),
                        "viewer": bool((summary.parent / "viewer.html").is_file()),
                    }
                )
        return {
            "active": [_run_status(run) for run in active.values()],
            "finished": finished,
        }

    def _run_dir_or_404(name: str) -> Path:
        run_dir = runs_dir / name
        if not run_dir.is_dir() or not run_dir.resolve().is_relative_to(
            runs_dir.resolve()
        ):
            raise HTTPException(404, f"run not found: {name}")
        return run_dir

    @app.get("/api/runs/{name}/viewer")
    def run_viewer(name: str) -> FileResponse:
        run_dir = _run_dir_or_404(name)
        target = run_dir / "viewer.html"
        if not target.is_file():
            from .viewergen import generate_viewer  # noqa: PLC0415 - pulls pyvista

            try:
                generate_viewer(run_dir, title=name)
            except Exception as exc:  # noqa: BLE001 - surfaced as an HTTP error
                raise HTTPException(409, f"viewer generation failed: {exc}") from exc
        return FileResponse(target, media_type="text/html")

    @app.get("/api/runs/{name}/step")
    def run_step(name: str, frame: int = -1, part: str = "can") -> FileResponse:
        """Deformed-shape STEP export (issue #26); generated once, then cached."""
        run_dir = _run_dir_or_404(name)
        from ..post.export_step import export_deformed_step  # noqa: PLC0415 - pulls OCP

        try:
            target = export_deformed_step(run_dir, frame=frame, part=part)
        except Exception as exc:  # noqa: BLE001 - surfaced as an HTTP error
            raise HTTPException(409, f"STEP export failed: {exc}") from exc
        return FileResponse(
            target, media_type="application/step", filename=target.name
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    app.mount(
        "/runs", StaticFiles(directory=str(runs_dir), check_dir=False), name="runs"
    )
    return app


def serve(
    root: str | Path = ".",
    *,
    host: str = "127.0.0.1",
    port: int = 8384,
    open_browser: bool = True,
) -> None:
    """Run the UI server (blocking), optionally opening a browser tab."""
    import webbrowser  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    app = create_app(root)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
