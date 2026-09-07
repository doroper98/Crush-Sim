"""UI_002 §2.2 - uploaded STEP assets: store, inspect, preview.

An upload lands under ``runs/_ui/assets/<asset_id>/`` as three files:

``original.stp``   the bytes exactly as uploaded,
``inspect.json``   the asset record (status, units, dimensions, parts, diagnostics),
``preview.json``   a display-only triangle mesh (~3 mm, never the analysis mesh).

The id is ``sha256(content)[:12]``: the same file uploaded twice is the same
asset, so re-importing costs nothing and two graphs can point at one geometry.
The uploaded file *name* is kept only as ``display_name`` and never used to
build a path (U42) - a name is user input, and user input does not choose where
the server writes.

Inspection runs on a background thread because ``inspect_step`` +
``extract_shell_skins`` + a preview mesh take seconds to minutes on real CAD;
the POST answers ``202 importing`` and the browser polls ``GET /api/assets/{id}``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from ..errors import OptionalDependencyError
from . import capabilities
from .errors import UiError, code_for_exception
from .storage import write_json_atomic

#: Target element size of the preview mesh [mm]. Display only - the analysis
#: mesh comes from the case, and the response says so with ``lod``.
PREVIEW_TARGET_MM = 3.0
PREVIEW_LOD = "preview_3mm"

#: A cell is somewhere between a coin cell and a big prismatic module. Outside
#: this range the STEP was almost certainly exported in another unit (§2.2).
PLAUSIBLE_MIN_MM = 5.0
PLAUSIBLE_MAX_MM = 500.0


def _sanitise_display_name(name: str | None) -> str:
    """The file name as a label only - path separators stripped (U42)."""
    raw = (name or "geometry.stp").replace("\\", "/").split("/")[-1].strip()
    return raw[:120] or "geometry.stp"


class AssetStore:
    """The ``runs/_ui/assets`` directory, with inspection as a background job."""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.root = self.base / "runs" / "_ui" / "assets"
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    # -- paths ---------------------------------------------------------------

    def dir_for(self, asset_id: str) -> Path:
        if not asset_id or not asset_id.isalnum():
            raise UiError("NOT_FOUND", f"형상 자산을 찾을 수 없습니다: {asset_id}")
        return self.root / asset_id

    def record_path(self, asset_id: str) -> Path:
        return self.dir_for(asset_id) / "inspect.json"

    def step_path(self, asset_id: str) -> Path:
        return self.dir_for(asset_id) / "original.stp"

    # -- write ---------------------------------------------------------------

    def create(self, data: bytes, display_name: str | None, *, background: bool = True) -> dict[str, Any]:
        """Validate and store an upload, then start (or run) the inspection.

        Raises:
            UiError: ``FILE_TOO_LARGE`` or ``UNSUPPORTED_FILE_TYPE``.
        """
        name = _sanitise_display_name(display_name)
        if not name.lower().endswith(capabilities.UPLOAD_EXTENSIONS):
            raise UiError(
                "UNSUPPORTED_FILE_TYPE",
                detail=f"{name} (허용: {', '.join(capabilities.UPLOAD_EXTENSIONS)})",
                field_path="file",
            )
        if len(data) > capabilities.MAX_UPLOAD_BYTES:
            raise UiError(
                "FILE_TOO_LARGE",
                detail=f"{len(data)} bytes > {capabilities.MAX_UPLOAD_BYTES}",
                field_path="file",
            )
        if not data:
            raise UiError("UNSUPPORTED_FILE_TYPE", "빈 파일입니다.", field_path="file")
        digest = hashlib.sha256(data).hexdigest()
        asset_id = digest[:12]
        target = self.root / asset_id
        target.mkdir(parents=True, exist_ok=True)
        step = target / "original.stp"
        if not step.is_file():
            step.write_bytes(data)
        existing = self.read_record(asset_id)
        if existing and existing.get("status") == "ready":
            return existing
        record = {
            "id": asset_id,
            "display_name": name,
            "content_hash": f"sha256:{digest}",
            "bytes": len(data),
            "status": "importing",
            "error": None,
            "units": {"declared": "unknown", "confirmed": False, "plausible": True},
            "dimensions_mm": None,
            "parts": [],
            "diagnostics": [],
            "preview": None,
        }
        self.write_record(asset_id, record)
        if background:
            self._start(asset_id)
        else:
            inspect_asset(self, asset_id)
            return self.read_record(asset_id) or record
        return record

    def _start(self, asset_id: str) -> None:
        with self._lock:
            thread = self._threads.get(asset_id)
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=inspect_asset, args=(self, asset_id), daemon=True, name=f"asset-{asset_id}"
            )
            self._threads[asset_id] = thread
        thread.start()

    def write_record(self, asset_id: str, record: dict[str, Any]) -> None:
        # Atomic: the browser polls this file while the inspector rewrites it.
        write_json_atomic(self.record_path(asset_id), record)

    def read_record(self, asset_id: str) -> dict[str, Any] | None:
        path = self.record_path(asset_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    # -- read ----------------------------------------------------------------

    def get(self, asset_id: str) -> dict[str, Any]:
        """The asset record (UI_002 §2.2), or a 404 in the error contract."""
        record = self.read_record(asset_id)
        if record is None:
            raise UiError("NOT_FOUND", f"형상 자산을 찾을 수 없습니다: {asset_id}")
        return record

    def preview(self, asset_id: str) -> dict[str, Any]:
        """The display mesh, or a 409 while the import is still running."""
        path = self.dir_for(asset_id) / "preview.json"
        if not path.is_file():
            record = self.get(asset_id)
            raise UiError(
                "ASSET_NOT_READY",
                "미리보기가 아직 준비되지 않았습니다.",
                detail=f"status={record.get('status')}",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def content_hash(self, asset_id: str) -> str | None:
        record = self.read_record(asset_id)
        return record.get("content_hash") if record else None


def _role_suggestion(kind: str, volume: float, volumes: list[float]) -> str:
    """A first guess at what a solid is for - the user confirms it (§5)."""
    if kind == "plate":
        return "cap"
    return "can" if volumes and volume >= max(volumes) else "part"


def inspect_asset(store: AssetStore, asset_id: str) -> dict[str, Any]:
    """Inspect one stored asset and rewrite its record. Never raises.

    Runs three independent steps; a failure of the *first* is fatal for the
    asset (no topology means nothing else is meaningful), while a failed skin
    extraction or preview mesh only adds a diagnostic - the units and extents
    are still worth showing.
    """
    record = store.read_record(asset_id) or {"id": asset_id}
    step = store.step_path(asset_id)
    diagnostics: list[dict[str, Any]] = []

    try:
        from ..geometry.step_inspect import inspect_step  # noqa: PLC0415 - pulls OCP

        summary = inspect_step(step)
    except OptionalDependencyError as exc:
        record.update(
            status="failed",
            error={
                "code": "CAD_BACKEND_MISSING",
                "message": "이 서버에는 STEP 처리 기능(CAD 백엔드)이 설치되어 있지 않습니다.",
                "detail": str(exc),
            },
        )
        store.write_record(asset_id, record)
        return record
    except Exception as exc:  # noqa: BLE001 - any read failure belongs to the asset
        record.update(
            status="failed",
            error={
                "code": code_for_exception(exc),
                "message": "이 형상 파일을 읽지 못했습니다.",
                "detail": str(exc),
            },
        )
        store.write_record(asset_id, record)
        return record

    extents = [float(e) for e in summary.extents]
    declared = summary.header.declared_unit if summary.header else "unknown"
    plausible = PLAUSIBLE_MIN_MM <= max(extents) <= PLAUSIBLE_MAX_MM
    record["units"] = {"declared": declared, "confirmed": False, "plausible": bool(plausible)}
    record["dimensions_mm"] = extents
    record["topology"] = {
        "solids": summary.solids,
        "shells": summary.shells,
        "faces": summary.faces,
        "bbox_min_mm": list(summary.bbox_min),
        "bbox_max_mm": list(summary.bbox_max),
    }
    if summary.solids == 1:
        diagnostics.append(
            {
                "code": "SINGLE_SOLID",
                "severity": "info",
                "message": "솔리드 1개입니다. 캔 본체로 해석합니다.",
            }
        )
    elif summary.solids > 1:
        diagnostics.append(
            {
                "code": "MULTI_SOLID",
                "severity": "info",
                "message": f"솔리드 {summary.solids}개입니다. 각 부품의 역할을 확인하세요.",
            }
        )
    if not plausible:
        diagnostics.append(
            {
                "code": "UNIT_CONFIRMATION_REQUIRED",
                "severity": "block",
                "message": (
                    f"읽은 최대 크기가 {max(extents):.1f} mm입니다. "
                    "예상한 셀 크기와 맞나요? 단위를 확인하세요."
                ),
                "actions": ["confirm_units"],
            }
        )
    for warning in summary.warnings:
        diagnostics.append({"code": "STEP_WARNING", "severity": "review", "message": warning})

    parts: list[dict[str, Any]] = []
    try:
        from ..geometry.skin import extract_shell_skins  # noqa: PLC0415 - pulls OCP

        skins = extract_shell_skins(step, store.dir_for(asset_id) / "skin.brep")
    except Exception as exc:  # noqa: BLE001 - the asset is still usable without part roles
        skins = []
        diagnostics.append(
            {
                "code": "SKIN_EXTRACTION_FAILED",
                "severity": "review",
                "message": "부품별 벽두께를 측정하지 못했습니다. 두께를 직접 입력하세요.",
                "detail": str(exc),
            }
        )
    volumes = [float(s.volume_mm3) for s in skins]
    for skin in skins:
        parts.append(
            {
                "index": skin.index,
                "kind": skin.kind,
                "volume_mm3": float(skin.volume_mm3),
                "wall_thickness_mm": float(skin.wall_thickness_mm),
                "uniformity": float(skin.uniformity),
                "role_suggestion": _role_suggestion(
                    skin.kind, float(skin.volume_mm3), volumes
                ),
            }
        )
    record["parts"] = parts

    # The preview mesh runs in a SUBPROCESS, not in this thread: Gmsh installs
    # a SIGINT handler in gmsh.initialize() and dies with "signal only works in
    # main thread of the main interpreter" on any worker thread (measured while
    # writing this module). The mesher owns that call and is off-limits here,
    # and a subprocess also keeps a native Gmsh crash away from the server.
    failure = _run_preview_subprocess(store.dir_for(asset_id))
    preview_path = store.dir_for(asset_id) / "preview.json"
    if failure is None and preview_path.is_file():
        try:
            triangles = int(json.loads(preview_path.read_text(encoding="utf-8"))["triangles"])
        except (json.JSONDecodeError, KeyError, ValueError):
            triangles = 0
        record["preview"] = {
            "url": f"/api/assets/{asset_id}/preview",
            "triangles": triangles,
            "lod": PREVIEW_LOD,
        }
    else:
        record["preview"] = None
        diagnostics.append(
            {
                "code": "PREVIEW_UNAVAILABLE",
                "severity": "info",
                "message": "미리보기 메쉬를 만들지 못했습니다. 치수와 부품 정보만 표시합니다.",
                "detail": failure or "preview.json was not written",
            }
        )

    record["diagnostics"] = diagnostics
    record["status"] = "ready"
    record["error"] = None
    store.write_record(asset_id, record)
    return record


def _run_preview_subprocess(asset_dir: Path, *, timeout: float = 600.0) -> str | None:
    """Mesh the asset for display in a child process. Returns an error, or None."""
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "crushsim.ui.assets", str(asset_dir)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"preview meshing timed out after {timeout:.0f} s"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return tail[-1] if tail else f"preview meshing exited {proc.returncode}"
    return None


def build_preview(asset_dir: str | Path) -> dict[str, Any]:
    """Mesh ``original.stp`` (or its extracted skin) and write ``preview.json``.

    Runs in the child process started by :func:`_run_preview_subprocess`, i.e.
    on a main thread, which is what Gmsh requires.
    """
    from ..meshing.mesher import mesh_step_surfaces  # noqa: PLC0415 - pulls gmsh

    directory = Path(asset_dir)
    skin = directory / "skin.brep"
    source = skin if skin.is_file() else directory / "original.stp"
    result = mesh_step_surfaces(
        source,
        target_size=PREVIEW_TARGET_MM,
        recombine=False,
        enforce=False,  # a display mesh is never gated
        name=f"preview_{directory.name}",
    )
    payload = _preview_payload(result.mesh)
    write_json_atomic(directory / "preview.json", payload, indent=None)
    return payload


def _preview_payload(mesh: Any) -> dict[str, Any]:
    """Flat positions/indices for the WebGL preview (triangles only)."""
    import numpy as np  # noqa: PLC0415

    nodes = np.asarray(mesh.nodes, dtype=float)
    index_of = {int(node_id): i for i, node_id in enumerate(np.asarray(mesh.node_ids).ravel())}
    tris: list[list[int]] = []
    for quad in np.asarray(mesh.quads).reshape(-1, 4):
        a, b, c, d = (index_of[int(v)] for v in quad)
        tris.append([a, b, c])
        tris.append([a, c, d])
    for tri in np.asarray(mesh.tris).reshape(-1, 3):
        tris.append([index_of[int(v)] for v in tri])
    flat_tris = [i for tri in tris for i in tri]
    return {
        "lod": PREVIEW_LOD,
        "positions": [round(float(v), 4) for v in nodes.ravel()],
        "indices": flat_tris,
        "bbox": [
            [float(v) for v in nodes.min(axis=0)],
            [float(v) for v in nodes.max(axis=0)],
        ],
        "triangles": len(tris),
    }


__all__ = [
    "PREVIEW_LOD",
    "PREVIEW_TARGET_MM",
    "AssetStore",
    "build_preview",
    "inspect_asset",
]


if __name__ == "__main__":  # pragma: no cover - the preview child process
    import sys

    build_preview(sys.argv[1])
