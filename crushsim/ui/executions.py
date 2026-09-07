"""UI_002 §2.5 - immutable executions on one serial queue.

What the server runs is the **snapshot**, never the draft the user keeps
editing (UI_001 §9.3). Submitting writes

``runs/_ui/executions/<exec_id>/case.yaml``     the normalised case, frozen,
``runs/_ui/executions/<exec_id>/snapshot.json`` input hash, preflight, targets,
``runs/_ui/executions/<exec_id>/state.json``    lifecycle state, timestamps,
``runs/_ui/executions/<exec_id>/log.txt``       the pipeline's stdout,

and enqueues the exec id. One worker thread drains the queue: two OpenRadioss
engines on four cores spin-lock each other ~100x slower (CLAUDE.md), so
parallelism here is not a tuning knob, it is a defect.

State restore after a server restart follows U25: a queued/running execution
whose process is gone is ``interrupted`` unless the run directory actually
holds a ``pipeline_summary.json``. A log file is never evidence of a live run.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .errors import UiError
from .storage import write_json_atomic, write_text_atomic

#: Terminal states - an execution in one of these never changes again.
FINAL_STATES = ("completed", "failed", "cancelled", "interrupted")

_PROGRESS = re.compile(r"engine\s+([0-9.]+)%.*?energy error\s+(-?[0-9.]+)%")
#: ``[  123s] [2/6] meshing (target 1.2 mm)`` - the pipeline's own banner.
_STAGE = re.compile(r"\[\s*([0-9.]+)s\]\s*\[(\d+)/(\d+)\]\s+(\S+)")

#: Pipeline banner -> the stage name the execution contract reports.
_STAGE_NAMES = {
    "geometry": "geometry",
    "meshing": "meshing",
    "deck": "deck",
    "solver": "solver",
    "post-processing": "post",
    "report": "report",
}

_EXEC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")


def _now() -> str:
    # Sub-second resolution matters: the report compares estimate against
    # measured time, and a seconds-only stamp reported a cancelled run as
    # "0.0 s of execution" during testing.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


class ExecutionManager:
    """The process-wide serial run queue and the state on disk behind it."""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.root = self.base / "runs" / "_ui" / "executions"
        self.runs_dir = self.base / "runs"
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelling: set[str] = set()
        self._worker: threading.Thread | None = None
        #: Test seam: what to run for one snapshot. Overridden in tests so the
        #: queue can be exercised without OpenRadioss.
        self.command_builder = self._default_command
        self.restore()

    # -- paths ---------------------------------------------------------------

    def dir_for(self, exec_id: str) -> Path:
        if not _EXEC_ID.fullmatch(exec_id or ""):
            raise UiError("NOT_FOUND", f"실행을 찾을 수 없습니다: {exec_id}")
        return self.root / exec_id

    def case_path(self, exec_id: str) -> Path:
        return self.dir_for(exec_id) / "case.yaml"

    def log_path(self, exec_id: str) -> Path:
        return self.dir_for(exec_id) / "log.txt"

    def _state_path(self, exec_id: str) -> Path:
        return self.dir_for(exec_id) / "state.json"

    def _default_command(self, exec_id: str) -> list[str]:
        # Exactly the command a user would type, pointed at the snapshot
        # (U20: configs/cases/ is never written by an execution).
        return [sys.executable, "-m", "crushsim", "all", "-c", str(self.case_path(exec_id))]

    # -- state ---------------------------------------------------------------

    def read_state(self, exec_id: str) -> dict[str, Any]:
        path = self._state_path(exec_id)
        if not path.is_file():
            raise UiError("NOT_FOUND", f"실행을 찾을 수 없습니다: {exec_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        # Atomic: a request handler reads this file while the worker writes it.
        write_json_atomic(self._state_path(state["exec_id"]), state)

    def _update(self, exec_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self.read_state(exec_id)
            state.update(fields)
            self._write_state(state)
            return state

    def read_snapshot(self, exec_id: str) -> dict[str, Any]:
        path = self.dir_for(exec_id) / "snapshot.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def restore(self) -> None:
        """U25: reconcile stored states with reality after a restart."""
        if not self.root.is_dir():
            return
        for state_file in sorted(self.root.glob("*/state.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if state.get("state") in FINAL_STATES:
                continue
            run_dir = self.base / state.get("run_dir", "")
            if (run_dir / "pipeline_summary.json").is_file():
                state["state"] = "completed"
                state.setdefault("timestamps", {})["finished"] = _now()
            else:
                # No process of ours survives a restart, and a log file is not
                # evidence that one is running.
                state["state"] = "interrupted"
                state.setdefault("timestamps", {})["finished"] = _now()
            write_json_atomic(state_file, state)

    # -- submit --------------------------------------------------------------

    def find_by_idempotency(self, key: str | None) -> list[str]:
        """Exec ids already created for this idempotency key (U19)."""
        if not key:
            return []
        found: list[tuple[int, str]] = []
        for state_file in sorted(self.root.glob("*/state.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if state.get("idempotency_key") == key:
                found.append((int(state.get("batch_index") or 0), state["exec_id"]))
        # U19 wants the *same* answer, order included - a directory listing is
        # alphabetical, which reversed a medium/fine submission in testing.
        return [exec_id for _index, exec_id in sorted(found)]

    def submit(
        self,
        cases: list[dict[str, Any]],
        *,
        input_hash: str,
        preflight_id: str | None = None,
        idempotency_key: str | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Freeze each case as a snapshot and queue it. Same key -> same answer."""
        existing = self.find_by_idempotency(idempotency_key)
        if existing:
            return [self.status(exec_id) for exec_id in existing]
        out: list[dict[str, Any]] = []
        for index, entry in enumerate(cases):
            exec_id = entry["exec_id"]
            case = json.loads(json.dumps(entry["yaml"]))
            case["output"] = dict(case.get("output") or {})
            case["output"]["dir"] = f"runs/{exec_id}"
            out.append(
                self._enqueue(
                    exec_id,
                    case=case,
                    run_dir=f"runs/{exec_id}",
                    case_file=f"{exec_id}.yaml",
                    input_hash=input_hash,
                    preflight_id=preflight_id,
                    idempotency_key=idempotency_key,
                    batch_index=index,
                    snapshot={
                        **(snapshot or {}),
                        "case_name": entry.get("name"),
                        "preset_id": entry.get("preset_id"),
                        "changed_vs_first": entry.get("changed_vs_first") or {},
                    },
                )
            )
        return out

    def submit_case_file(self, case_file: str, path: Path, case_name: str) -> dict[str, Any]:
        """Legacy ``POST /api/runs/{case_file}`` adapter (UI_002 §1.3).

        The stored case is copied into a snapshot and run through the same
        queue. Its ``output.dir`` is kept as written, so runs started the old
        way still land where the old UI looks for them.
        """
        case = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        run_dir = str((case.get("output") or {}).get("dir") or f"runs/{path.stem}")
        digest = f"sha256:{abs(hash((case_file, run_dir))):016x}"
        exec_id = path.stem
        state = self._state_or_none(exec_id)
        if state and state.get("state") in ("queued", "running", "cancelling"):
            raise UiError("ALREADY_RUNNING", f"{case_file}은(는) 이미 실행 중이거나 대기 중입니다.")
        return self._enqueue(
            exec_id,
            case=case,
            run_dir=run_dir,
            case_file=case_file,
            input_hash=digest,
            preflight_id=None,
            idempotency_key=None,
            snapshot={"legacy": True, "case_name": case_name, "source_case": case_file},
        )

    def _state_or_none(self, exec_id: str) -> dict[str, Any] | None:
        try:
            return self.read_state(exec_id)
        except UiError:
            return None

    def _enqueue(
        self,
        exec_id: str,
        *,
        case: dict[str, Any],
        run_dir: str,
        case_file: str,
        input_hash: str,
        preflight_id: str | None,
        idempotency_key: str | None,
        snapshot: dict[str, Any],
        batch_index: int = 0,
    ) -> dict[str, Any]:
        target = self.dir_for(exec_id)
        target.mkdir(parents=True, exist_ok=True)
        write_text_atomic(
            self.case_path(exec_id), yaml.safe_dump(case, sort_keys=False, allow_unicode=True)
        )
        write_json_atomic(
            target / "snapshot.json",
            {
                "exec_id": exec_id,
                "input_hash": input_hash,
                "preflight_id": preflight_id,
                "idempotency_key": idempotency_key,
                "submitted": _now(),
                **snapshot,
            },
        )
        state = {
            "exec_id": exec_id,
            "case_name": snapshot.get("case_name") or case.get("name") or exec_id,
            "case_file": case_file,
            "run_dir": run_dir,
            "input_hash": input_hash,
            "preflight_id": preflight_id,
            "idempotency_key": idempotency_key,
            "batch_index": batch_index,
            "state": "queued",
            "returncode": None,
            "timestamps": {"submitted": _now(), "started": None, "finished": None},
        }
        with self._lock:
            self._write_state(state)
            if exec_id not in self._pending:
                self._pending.append(exec_id)
        self._ensure_worker()
        return self.status(exec_id)

    # -- worker --------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            worker = self._worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._drain, daemon=True, name="csim-ui-runner")
                self._worker = worker
                worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                exec_id = self._pending.pop(0)
            self._run_one(exec_id)

    def _run_one(self, exec_id: str) -> None:
        log_path = self.log_path(exec_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        state = self._state_or_none(exec_id)
        if state is None or state.get("state") in FINAL_STATES:
            return
        try:
            with log_path.open("w", encoding="utf-8") as log:
                # Own process group: a cancel must take the solver engine down
                # with the pipeline process, not orphan it on the cores.
                process = subprocess.Popen(
                    self.command_builder(exec_id),
                    cwd=self.base,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:  # noqa: BLE001 - a broken launch must not kill the worker
            log_path.write_text(f"launch failed: {exc}\n", encoding="utf-8")
            self._finish(exec_id, "failed", returncode=None)
            return
        with self._lock:
            self._processes[exec_id] = process
        timestamps = dict(state.get("timestamps") or {})
        timestamps["started"] = _now()
        self._update(exec_id, state="running", timestamps=timestamps)
        returncode = process.wait()
        with self._lock:
            self._processes.pop(exec_id, None)
            cancelled = exec_id in self._cancelling
            self._cancelling.discard(exec_id)
        if cancelled:
            self._finish(exec_id, "cancelled", returncode=returncode)
        else:
            self._finish(exec_id, "completed" if returncode == 0 else "failed", returncode=returncode)

    def _finish(self, exec_id: str, state_name: str, *, returncode: int | None) -> None:
        state = self._state_or_none(exec_id)
        timestamps = dict((state or {}).get("timestamps") or {})
        timestamps["finished"] = _now()
        self._update(exec_id, state=state_name, returncode=returncode, timestamps=timestamps)

    # -- cancel --------------------------------------------------------------

    def cancel(self, exec_id: str) -> dict[str, Any]:
        """Dequeue a waiting run, or ask a running one to stop (U21/§9.3)."""
        state = self.read_state(exec_id)
        with self._lock:
            if exec_id in self._pending:
                self._pending.remove(exec_id)
                queued = True
            else:
                queued = False
            process = self._processes.get(exec_id)
        if queued:
            self._finish(exec_id, "cancelled", returncode=None)
            return self.status(exec_id)
        if process is not None and process.poll() is None:
            self._cancelling.add(exec_id)
            self._update(exec_id, state="cancelling")
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            # 'cancelled' is only confirmed once the process is actually gone -
            # the worker sets it after wait() returns (UI_001 §9.3).
            return self.status(exec_id)
        if state.get("state") in FINAL_STATES:
            raise UiError(
                "NOT_FOUND",
                f"{exec_id}은(는) 이미 종료된 실행입니다.",
                detail=str(state.get("state")),
            )
        self._finish(exec_id, "cancelled", returncode=None)
        return self.status(exec_id)

    # -- read ----------------------------------------------------------------

    def queue_position(self, exec_id: str) -> int | None:
        with self._lock:
            if exec_id in self._pending:
                return self._pending.index(exec_id) + 1
        return None

    def log_tail(self, exec_id: str, lines: int = 25) -> str:
        path = self.log_path(exec_id)
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        kept = [ln for ln in text.splitlines() if "WARN|" not in ln]
        return "\n".join(kept[-lines:])

    def progress(self, exec_id: str) -> dict[str, Any]:
        """Stage, engine percentage and per-stage seconds, read from the log."""
        path = self.log_path(exec_id)
        out: dict[str, Any] = {
            "stage": None,
            "engine_progress_pct": None,
            "console_energy_error_pct": None,
            "stage_seconds": {},
        }
        if not path.is_file():
            return out
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        banners: list[tuple[float, str]] = []
        for line in lines:
            match = _STAGE.search(line)
            if match:
                banners.append((float(match.group(1)), _STAGE_NAMES.get(match.group(4), match.group(4))))
        if banners:
            out["stage"] = banners[-1][1]
            for (start, name), (end, _next) in zip(banners, banners[1:]):
                out["stage_seconds"][name] = round(end - start, 1)
        for line in reversed(lines):
            match = _PROGRESS.search(line)
            if match:
                out["engine_progress_pct"] = float(match.group(1))
                out["console_energy_error_pct"] = float(match.group(2))
                break
        return out

    def artifacts(self, run_dir: Path, exec_id: str) -> dict[str, Any]:
        """Only what actually exists on disk gets a link (UI_001 §11)."""
        summary = run_dir / "pipeline_summary.json"
        return {
            "report": f"/api/runs/{exec_id}/report" if (run_dir / "report.html").is_file() else None,
            "viewer": f"/api/runs/{exec_id}/viewer" if summary.is_file() else None,
            "csv": (
                f"/runs/{exec_id}/force_displacement.csv"
                if (run_dir / "force_displacement.csv").is_file()
                else None
            ),
            "summary": f"/runs/{exec_id}/pipeline_summary.json" if summary.is_file() else None,
        }

    def status(self, exec_id: str) -> dict[str, Any]:
        """The UI_002 §2.5 execution body."""
        state = self.read_state(exec_id)
        run_dir = self.base / state.get("run_dir", f"runs/{exec_id}")
        progress = self.progress(exec_id)
        timestamps = state.get("timestamps") or {}
        submitted, started, finished = (
            _parse(timestamps.get("submitted")),
            _parse(timestamps.get("started")),
            _parse(timestamps.get("finished")),
        )
        queue_seconds = round((started - submitted).total_seconds(), 1) if started and submitted else None
        execution_seconds = (
            round((finished - started).total_seconds(), 1) if started and finished else None
        )
        stage_seconds = dict(progress["stage_seconds"])
        summary_path = run_dir / "pipeline_summary.json"
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                for stage in (summary.get("solver") or {}).get("stages") or []:
                    stage_seconds[str(stage.get("stage"))] = round(
                        float(stage.get("duration_s") or 0.0), 1
                    )
            except (json.JSONDecodeError, ValueError):
                pass
        return {
            "exec_id": exec_id,
            "case_name": state.get("case_name"),
            "case_file": state.get("case_file"),
            "state": state.get("state"),
            "stage": progress["stage"] if state.get("state") == "running" else None,
            "engine_progress_pct": progress["engine_progress_pct"],
            "console_energy_error_pct": progress["console_energy_error_pct"],
            "queue_position": self.queue_position(exec_id),
            "returncode": state.get("returncode"),
            "timestamps": {
                "submitted": timestamps.get("submitted"),
                "started": timestamps.get("started"),
                "finished": timestamps.get("finished"),
            },
            "timing": {
                "queue_seconds": queue_seconds,
                "execution_seconds": execution_seconds,
                "stage_seconds": stage_seconds,
            },
            "diagnostics": self.diagnostics(exec_id, state, run_dir),
            "artifacts": self.artifacts(run_dir, exec_id),
            "run_dir": state.get("run_dir"),
            "snapshot": {
                "input_hash": state.get("input_hash"),
                "preflight_id": state.get("preflight_id"),
                "case_yaml_url": f"/api/executions/{exec_id}/case",
            },
            "log_tail": self.log_tail(exec_id),
        }

    def diagnostics(
        self, exec_id: str, state: dict[str, Any], run_dir: Path
    ) -> list[dict[str, Any]]:
        """Execution-level diagnostics; result-level ones live in :mod:`.results`."""
        out: list[dict[str, Any]] = []
        if state.get("state") == "interrupted":
            out.append(
                {
                    "code": "EXECUTION_INTERRUPTED",
                    "severity": "block",
                    "message": "실행 환경 중단으로 완료되지 않았습니다.",
                }
            )
        if state.get("state") == "failed":
            out.append(
                {
                    "code": "EXECUTION_FAILED",
                    "severity": "block",
                    "message": "해석이 완료되지 못했습니다. 진단 로그를 확인하세요.",
                    "detail": self.log_tail(exec_id, lines=5),
                }
            )
        if state.get("state") == "completed" and not (run_dir / "pipeline_summary.json").is_file():
            out.append(
                {
                    "code": "SUMMARY_MISSING",
                    "severity": "review",
                    "message": "실행은 끝났지만 결과 요약 파일이 없습니다.",
                }
            )
        return out

    def list(self) -> dict[str, Any]:
        """Queue + active + finished, snapshots first and legacy runs after (§2.5)."""
        items: list[dict[str, Any]] = []
        known_runs: set[str] = set()
        if self.root.is_dir():
            for state_file in sorted(self.root.glob("*/state.json")):
                exec_id = state_file.parent.name
                try:
                    item = self.status(exec_id)
                except (UiError, json.JSONDecodeError):
                    continue
                item["legacy"] = False
                known_runs.add(Path(item.get("run_dir") or "").name)
                items.append(item)
        if self.runs_dir.is_dir():
            for summary in sorted(self.runs_dir.glob("*/pipeline_summary.json")):
                name = summary.parent.name
                if name in known_runs or name.startswith("_"):
                    continue
                items.append(
                    {
                        "exec_id": name,
                        "case_name": name,
                        "state": "completed",
                        "legacy": True,
                        "stage": None,
                        "queue_position": None,
                        "timestamps": {
                            "submitted": None,
                            "started": None,
                            "finished": datetime.fromtimestamp(
                                summary.stat().st_mtime, tz=timezone.utc
                            ).isoformat(timespec="seconds"),
                        },
                        "artifacts": self.artifacts(summary.parent, name),
                        "run_dir": f"runs/{name}",
                        "snapshot": None,
                    }
                )
        return {"items": items}


__all__ = ["FINAL_STATES", "ExecutionManager"]
