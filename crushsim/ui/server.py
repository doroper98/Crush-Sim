"""FR-11 / UI_002 §1.1 - the FastAPI app: routing only.

Every decision lives in a module next to this one:

* :mod:`.capabilities` - what the server can run (purposes, presets, materials),
* :mod:`.assets`       - uploaded STEP geometry,
* :mod:`.graphc`       - graph -> case yaml (the authoritative compiler),
* :mod:`.preflight`    - normalise, check, hash, estimate,
* :mod:`.executions`   - immutable snapshots on one serial queue,
* :mod:`.results`      - metrics, target, validity, judgement,
* :mod:`.errors`       - the single error contract.

This file only maps HTTP to those calls. The server never implements physics
and never launches anything but the same ``csim`` CLI a user would type.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, File, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_case
from . import capabilities as caps
from . import results as results_mod
from .assets import AssetStore
from .errors import UiError, from_exception, install_error_handlers, not_found
from .executions import LIVE_STATES, ExecutionManager
from .graphc import GRAPH_SCHEMA_VERSION, compile_graph
from .preflight import PreflightStore

_STATIC = Path(__file__).parent / "static"
_CASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*\.yaml")
_GRAPH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*\.json")


def create_app(root: str | Path = ".") -> FastAPI:
    """Build the FastAPI app rooted at a Crush-Sim checkout."""
    base = Path(root).resolve()
    cases_dir = base / "configs" / "cases"
    graphs_dir = base / "configs" / "graphs"
    runs_dir = base / "runs"

    assets = AssetStore(base)
    executions = ExecutionManager(base)
    preflights = PreflightStore(base, assets)

    app = FastAPI(title="Crush-Sim UI", docs_url=None, redoc_url=None)
    install_error_handlers(app)
    app.state.assets = assets
    app.state.executions = executions
    app.state.preflights = preflights

    # -- capabilities --------------------------------------------------------

    @app.get("/api/capabilities")
    def get_capabilities() -> dict[str, Any]:
        return caps.capabilities(base)

    # -- assets --------------------------------------------------------------

    @app.post("/api/assets", status_code=202)
    async def upload_asset(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008
    ) -> dict[str, Any]:
        # Never hold more than the limit in memory: the declared size is
        # checked first, and the body is then read in chunks that stop one
        # byte past the limit instead of buffering a 2 GB "STEP file".
        limit = caps.MAX_UPLOAD_BYTES
        declared = file.size or int(request.headers.get("content-length") or 0)
        if declared and declared > limit:
            raise UiError(
                "FILE_TOO_LARGE", detail=f"{declared} bytes > {limit}", field_path="file"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UiError("FILE_TOO_LARGE", detail=f">{limit} bytes", field_path="file")
            chunks.append(chunk)
        record = assets.create(b"".join(chunks), file.filename)
        return {"asset_id": record["id"], "status": record["status"]}

    @app.get("/api/assets/{asset_id}")
    def get_asset(asset_id: str) -> dict[str, Any]:
        return assets.get(asset_id)

    @app.get("/api/assets/{asset_id}/preview")
    def get_asset_preview(asset_id: str) -> dict[str, Any]:
        return assets.preview(asset_id)

    # -- preflights ----------------------------------------------------------

    @app.post("/api/preflights")
    def create_preflight(payload: dict[str, Any]) -> dict[str, Any]:
        graph = payload.get("graph")
        if not isinstance(graph, dict):
            raise UiError("INVALID_VALUE", "그래프가 필요합니다.", field_path="graph")
        return preflights.create(graph, payload.get("solver_node_id"))

    @app.get("/api/preflights/{preflight_id}")
    def get_preflight(preflight_id: str) -> dict[str, Any]:
        return preflights.get(preflight_id)

    # -- executions ----------------------------------------------------------

    @app.post("/api/executions", status_code=201)
    def create_execution(payload: dict[str, Any]) -> dict[str, Any]:
        preflight = preflights.get(str(payload.get("preflight_id") or ""))
        supplied = str(payload.get("input_hash") or "")
        if supplied != preflight["input_hash"]:
            raise UiError(
                "STALE_PREFLIGHT",
                detail=f"expected {preflight['input_hash']}, got {supplied}",
            )
        if not preflight["runnable"]:
            blocks = [c["message"] for c in preflight["checks"] if c["severity"] == "block"]
            raise UiError("PREFLIGHT_NOT_RUNNABLE", detail="; ".join(blocks) or None)
        started = executions.submit(
            preflight["cases"],
            input_hash=preflight["input_hash"],
            preflight_id=preflight["id"],
            idempotency_key=payload.get("idempotency_key"),
            snapshot={
                "purpose": preflight.get("purpose"),
                "targets": preflight.get("targets") or [],
                "graph_revision": preflight.get("graph_revision"),
                "estimate": preflight.get("estimate"),
                "policy_version": preflight.get("policy_version"),
            },
        )
        return {
            "executions": [
                {
                    "exec_id": item["exec_id"],
                    "state": item["state"],
                    "queue_position": item["queue_position"],
                }
                for item in started
            ]
        }

    @app.get("/api/executions")
    def list_executions() -> dict[str, Any]:
        return executions.list()

    @app.get("/api/executions/{exec_id}")
    def get_execution(exec_id: str) -> dict[str, Any]:
        return executions.status(exec_id)

    @app.get("/api/executions/{exec_id}/case")
    def get_execution_case(exec_id: str) -> Response:
        path = executions.case_path(exec_id)
        if not path.is_file():
            raise not_found(f"실행 스냅샷을 찾을 수 없습니다: {exec_id}")
        return Response(path.read_text(encoding="utf-8"), media_type="text/yaml")

    @app.post("/api/executions/{exec_id}/cancel")
    def cancel_execution(exec_id: str) -> dict[str, Any]:
        return executions.cancel(exec_id)

    @app.get("/api/executions/{exec_id}/result")
    def get_execution_result(exec_id: str) -> dict[str, Any]:
        return results_mod.build(base, exec_id, executions)

    # -- cases (existing) ----------------------------------------------------

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
            raise not_found(f"케이스를 찾을 수 없습니다: {case_file}")
        return cases_dir / case_file

    @app.get("/api/cases/{case_file}/raw")
    def case_raw(case_file: str) -> dict[str, Any]:
        """The case yaml as a plain mapping - the workflow editor's node values."""
        path = _case_path_or_404(case_file)
        if not path.is_file():
            raise not_found(f"케이스를 찾을 수 없습니다: {case_file}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise UiError("UNPARSEABLE", "케이스 YAML을 읽을 수 없습니다.", detail=str(exc)) from exc
        if not isinstance(data, dict):
            raise UiError("UNPARSEABLE", "케이스 YAML이 매핑이 아닙니다.")
        return data

    @app.put("/api/cases/{case_file}")
    def save_case(case_file: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Save an edited case back to ``configs/cases/``.

        Validated by the same ``load_case`` the pipeline uses - an invalid case
        is rejected with the loader's own message and the file on disk is left
        untouched. Executions never write here (U20).
        """
        path = _case_path_or_404(case_file)
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        probe = cases_dir / f".{path.stem}.probe.yaml"
        cases_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text(text, encoding="utf-8")
        try:
            load_case(probe)
        except Exception as exc:  # noqa: BLE001 - loader message goes to the editor
            raise from_exception(exc, message="케이스 설정에 오류가 있습니다.") from exc
        finally:
            probe.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"saved": case_file}

    # -- runs (existing surface, executed through the new queue) -------------

    @app.post("/api/runs/{case_file}")
    def start_run(case_file: str) -> dict[str, Any]:
        """Legacy launch: the case file is snapshotted and queued (UI_002 §1.3)."""
        path = _case_path_or_404(case_file)
        if not path.is_file():
            raise not_found(f"케이스를 찾을 수 없습니다: {case_file}")
        try:
            case = load_case(path)
        except Exception as exc:  # noqa: BLE001 - the loader message is the answer
            raise from_exception(exc, message="케이스 설정에 오류가 있습니다.") from exc
        status = executions.submit_case_file(case_file, path, case.name)
        return {
            "queued": case_file,
            "exec_id": status["exec_id"],
            "position": status["queue_position"] or 1,
        }

    @app.delete("/api/runs/{case_file}")
    def cancel_run(case_file: str) -> dict[str, Any]:
        """Dequeue a waiting run, or terminate a running one (whole group)."""
        live = executions.list(states=set(LIVE_STATES), include_legacy=False)
        for item in live["items"]:
            if item.get("case_file") != case_file:
                continue
            executions.cancel(item["exec_id"])
            return {"cancelled": case_file, "was": item["state"]}
        raise not_found(f"{case_file}은(는) 대기 중도 실행 중도 아닙니다.")

    @app.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        """The legacy monitor payload: active / queued / finished."""
        active, queued = [], []
        # Only live executions: the finished list below is built from the run
        # directories, so scanning (and log-parsing) every past execution here
        # would make the 5-second poll cost grow with the run history.
        live = executions.list(states=set(LIVE_STATES), include_legacy=False)
        for item in live["items"]:
            if item["state"] == "queued":
                queued.append(item.get("case_file") or f"{item['exec_id']}.yaml")
            elif item["state"] in ("running", "cancelling"):
                active.append(
                    {
                        "case": item.get("case_name"),
                        "file": item.get("case_file") or f"{item['exec_id']}.yaml",
                        "exec_id": item["exec_id"],
                        "running": True,
                        "returncode": item.get("returncode"),
                        "stage": item.get("stage"),
                        "engine_progress_pct": item.get("engine_progress_pct"),
                        "console_energy_error_pct": item.get("console_energy_error_pct"),
                        "log_tail": item.get("log_tail", ""),
                    }
                )
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
                        "vent": post.get("vent"),
                        "report": bool((summary.parent / "report.html").is_file()),
                        "viewer": bool((summary.parent / "viewer.html").is_file()),
                    }
                )
        return {"active": active, "queued": queued, "finished": finished}

    def _run_dir_or_404(name: str) -> Path:
        run_dir = runs_dir / name
        if not run_dir.is_dir() or not run_dir.resolve().is_relative_to(runs_dir.resolve()):
            raise not_found(f"실행 결과를 찾을 수 없습니다: {name}")
        return run_dir

    @app.get("/api/runs/{name}/report")
    def run_report(name: str) -> FileResponse:
        target = _run_dir_or_404(name) / "report.html"
        if not target.is_file():
            raise not_found(f"{name}에는 report.html이 없습니다.")
        return FileResponse(target, media_type="text/html")

    @app.get("/api/runs/{name}/viewer")
    def run_viewer(name: str) -> FileResponse:
        run_dir = _run_dir_or_404(name)
        target = run_dir / "viewer.html"
        if not target.is_file():
            from .viewergen import generate_viewer  # noqa: PLC0415 - pulls pyvista

            try:
                generate_viewer(run_dir, title=name)
            except Exception as exc:  # noqa: BLE001 - surfaced through the contract
                raise from_exception(
                    exc, message="3D 뷰어를 만들지 못했습니다.", status=409
                ) from exc
        return FileResponse(target, media_type="text/html")

    @app.get("/api/runs/{name}/step")
    def run_step(name: str, frame: int = -1, part: str = "can") -> FileResponse:
        """Deformed-shape STEP export (issue #26); generated once, then cached."""
        run_dir = _run_dir_or_404(name)
        from ..post.export_step import export_deformed_step  # noqa: PLC0415 - pulls OCP

        try:
            target = export_deformed_step(run_dir, frame=frame, part=part)
        except Exception as exc:  # noqa: BLE001 - surfaced through the contract
            raise from_exception(
                exc, message="변형 형상 STEP을 만들지 못했습니다.", status=409
            ) from exc
        return FileResponse(target, media_type="application/step", filename=target.name)

    # -- graphs --------------------------------------------------------------

    @app.get("/api/graphs")
    def list_graphs() -> list[str]:
        if not graphs_dir.is_dir():
            return []
        return sorted(p.name for p in graphs_dir.glob("*.json"))

    @app.get("/api/graphs/{name}")
    def get_graph(name: str) -> dict[str, Any]:
        if not _GRAPH_NAME.fullmatch(name) or not (graphs_dir / name).is_file():
            raise not_found(f"그래프를 찾을 수 없습니다: {name}")
        try:
            return json.loads((graphs_dir / name).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UiError("UNPARSEABLE", "그래프 파일을 읽을 수 없습니다.", detail=str(exc)) from exc

    @app.put("/api/graphs/{name}")
    def save_graph(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not _GRAPH_NAME.fullmatch(name):
            raise not_found(f"그래프 이름이 올바르지 않습니다: {name}")
        if "nodes" not in payload or "edges" not in payload:
            raise UiError("INVALID_VALUE", "그래프에는 'nodes'와 'edges'가 필요합니다.")
        target = graphs_dir / name
        stored_revision = 0
        if target.is_file():
            try:
                stored_revision = int(
                    (json.loads(target.read_text(encoding="utf-8")) or {}).get("revision") or 0
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                stored_revision = 0
        base_revision = payload.get("base_revision")
        if base_revision is not None:
            try:
                base_revision = int(base_revision)
            except (TypeError, ValueError) as exc:
                raise UiError(
                    "INVALID_VALUE",
                    "base_revision은 정수여야 합니다.",
                    field_path="base_revision",
                    detail=repr(payload.get("base_revision")),
                ) from exc
            if stored_revision > base_revision:
                # U41: detect only. The merge is P3 backlog; the user is told
                # and keeps both copies rather than losing the other tab's edit.
                raise UiError(
                    "REVISION_CONFLICT",
                    detail=f"server revision {stored_revision} > base_revision {base_revision}",
                )
        body = {k: v for k, v in payload.items() if k not in ("base_revision", "revision")}
        body.setdefault("schema_version", GRAPH_SCHEMA_VERSION)
        # The counter belongs to the SERVER. Echoing back the revision the
        # client sent meant a client that re-saved what it had loaded never
        # advanced it, so the conflict check could never fire (U41).
        body["revision"] = stored_revision + 1
        graphs_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"saved": name, "revision": body["revision"]}

    @app.post("/api/graphs/compile")
    def compile_graph_route(payload: dict[str, Any]) -> JSONResponse:
        """Compile without checking or hashing - the editor's preview (§1.4)."""
        graph = payload.get("graph")
        if not isinstance(graph, dict):
            raise UiError("INVALID_VALUE", "그래프가 필요합니다.", field_path="graph")
        return JSONResponse(compile_graph(graph, solver_node_id=payload.get("solver_node_id")))

    # -- static --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    app.mount("/runs", StaticFiles(directory=str(runs_dir), check_dir=False), name="runs")
    return app


def serve(
    root: str | Path = ".",
    *,
    host: str = "127.0.0.1",
    port: int = 8384,
    open_browser: bool = True,
) -> None:
    """Run the UI server (blocking), optionally opening a browser tab."""
    import threading  # noqa: PLC0415
    import webbrowser  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    app = create_app(root)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
