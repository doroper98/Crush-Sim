"""UI_002 §2.4 - ``POST /api/preflights``: normalise, check, hash, estimate.

A preflight is the contract between "what the user has drawn" and "what the
server will run". It compiles the graph, resolves asset references, runs every
check the server can answer *before* the solver starts, hashes the normalised
input, and attaches a time estimate that only ever comes from a measured run.

Two rules from UI_001 §8 shape this module:

* only ``severity: "block"`` stops a run. A time-budget overrun is a warning
  (`review`), never a refusal.
* the hash is the identity of the submission. ``POST /api/executions`` recomputes
  it and answers ``409 STALE_PREFLIGHT`` when the draft moved on (U18).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

from ..config import load_case
from ..errors import ConfigError
from ..units import STEP_THICKNESS_MISMATCH_MAX
from . import capabilities, estimates, graphc
from .assets import AssetStore
from .errors import UiError
from .storage import write_json_atomic


def check(
    code: str,
    severity: str,
    message: str,
    *,
    node_id: str | None = None,
    field_path: str | None = None,
    actions: list[str] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """One preflight check entry (UI_002 §2.4 / §2.7 shape)."""
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "node_id": node_id,
        "field_path": field_path,
        "actions": list(actions or []),
        "detail": detail,
    }


def normalised_yaml(case: dict[str, Any]) -> str:
    """Deterministic YAML text of one case - the unit the input hash is built from."""
    return yaml.safe_dump(case, sort_keys=True, allow_unicode=True, default_flow_style=False)


def input_hash(cases: list[dict[str, Any]], asset_hashes: list[str]) -> str:
    """sha256(normalised case yaml + asset content hashes + policy version) (§2.4)."""
    digest = hashlib.sha256()
    for case in cases:
        digest.update(normalised_yaml(case).encode("utf-8"))
        digest.update(b"\x00")
    for content_hash in sorted(asset_hashes):
        digest.update(content_hash.encode("utf-8"))
        digest.update(b"\x00")
    digest.update(capabilities.POLICY_VERSION.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def exec_id_for(name: str, hash_value: str, solver_node_id: str | None = None) -> str:
    """``<case name>_<first 6 of the input hash (+ solver node)>`` (UI_002 §1.2).

    The solver node id is mixed in so that two solver nodes producing the same
    case name in one draft cannot collide onto one execution directory (the
    ``DUPLICATE_CASE_NAME`` check blocks that case anyway - this is the second
    lock on the same door).
    """
    material = hash_value.split(":")[-1]
    if solver_node_id:
        material = hashlib.sha256(f"{hash_value}|{solver_node_id}".encode()).hexdigest()
    return f"{name}_{material[:6]}"


class PreflightStore:
    """Preflights kept in memory and mirrored to ``runs/_ui/preflights``."""

    def __init__(self, base: Path, assets: AssetStore) -> None:
        self.base = Path(base)
        self.root = self.base / "runs" / "_ui" / "preflights"
        self.assets = assets
        self._memory: dict[str, dict[str, Any]] = {}

    def path_for(self, preflight_id: str) -> Path:
        try:
            uuid.UUID(preflight_id)
        except ValueError as exc:
            raise UiError("NOT_FOUND", f"사전 검사를 찾을 수 없습니다: {preflight_id}") from exc
        return self.root / f"{preflight_id}.json"

    def get(self, preflight_id: str) -> dict[str, Any]:
        cached = self._memory.get(preflight_id)
        if cached is not None:
            return cached
        path = self.path_for(preflight_id)
        if not path.is_file():
            raise UiError("NOT_FOUND", f"사전 검사를 찾을 수 없습니다: {preflight_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self._memory[preflight_id] = data
        return data

    def create(self, graph: dict[str, Any], solver_node_id: str | None = None) -> dict[str, Any]:
        """Run the checks and store the result."""
        result = build(self.base, graph, solver_node_id=solver_node_id, assets=self.assets)
        self.root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path_for(result["id"]), result)
        self._memory[result["id"]] = result
        return result


def resolve_assets(base: Path, graph: dict[str, Any], assets: AssetStore) -> dict[str, Any]:
    """Copy of the graph with ``asset_refs`` written onto the geometry nodes.

    The draft points a geometry node at an asset id; the case yaml needs a
    path. Doing it here (and not in :mod:`.graphc`) keeps the compiler free of
    the store layout, and keeps ``step_path`` out of the guided flow entirely -
    a user never types one (UI_002 §3 WP2.4).
    """
    refs = graph.get("asset_refs") or {}
    if not refs:
        return graph
    clone = json.loads(json.dumps(graph))
    for node in clone.get("nodes") or []:
        asset_id = refs.get(node.get("id"))
        if not asset_id:
            continue
        step = assets.step_path(str(asset_id))
        params = node.setdefault("params", {})
        params["kind"] = params.get("kind") or "step"
        try:
            params["step_path"] = str(step.relative_to(base))
        except ValueError:
            params["step_path"] = str(step)
    return clone


def _validate_case(case: dict[str, Any]) -> str | None:
    """Loader message when ``load_case`` rejects the compiled case, else None."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{case.get('name', 'case')}.yaml"
        path.write_text(normalised_yaml(case), encoding="utf-8")
        try:
            load_case(path)
        except ConfigError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - any loader failure blocks the run
            return str(exc)
    return None


def _vent_checks(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Vent geometry rules the case loader does not enforce (§2.4 INVALID_VALUE)."""
    vent = (case.get("geometry") or {}).get("vent") or {}
    out: list[dict[str, Any]] = []
    residual = vent.get("score_thickness")
    foil = vent.get("membrane_thickness")
    if residual is not None and foil is not None and float(residual) >= float(foil):
        out.append(
            check(
                "INVALID_VALUE",
                "block",
                "스코어 잔여 두께는 포일 두께보다 얇아야 합니다.",
                field_path="geometry.vent.score_thickness",
                actions=["fix_value"],
                detail=f"score_thickness={residual} >= membrane_thickness={foil}",
            )
        )
    return out


def _asset_checks(graph: dict[str, Any], assets: AssetStore) -> tuple[list[dict[str, Any]], list[str]]:
    """Asset readiness, unit confirmation - and the content hashes for the input hash."""
    out: list[dict[str, Any]] = []
    hashes: list[str] = []
    confirmations = graph.get("confirmations") or {}
    for node_id, asset_id in (graph.get("asset_refs") or {}).items():
        try:
            record = assets.get(str(asset_id))
        except UiError:
            out.append(
                check(
                    "ASSET_NOT_READY",
                    "block",
                    "형상 자산을 찾을 수 없습니다. 다시 가져오세요.",
                    node_id=node_id,
                    actions=["reimport_asset"],
                    detail=str(asset_id),
                )
            )
            continue
        hashes.append(str(record.get("content_hash") or asset_id))
        status = record.get("status")
        if status == "failed":
            # Forward the importer's own verdict: telling the user to wait for
            # an import that already failed (CAD_BACKEND_MISSING, a corrupt
            # STEP) sends them into a loop that can never end.
            error = record.get("error") or {}
            out.append(
                check(
                    str(error.get("code") or "ASSET_UNSUPPORTED"),
                    "block",
                    str(error.get("message") or "형상 파일을 읽지 못했습니다."),
                    node_id=node_id,
                    actions=list(error.get("actions") or ["choose_other_file"]),
                    detail=str(error.get("detail") or ""),
                )
            )
        elif status != "ready":
            out.append(
                check(
                    "ASSET_NOT_READY",
                    "block",
                    "형상 가져오기가 아직 끝나지 않았습니다.",
                    node_id=node_id,
                    actions=["wait_and_retry"],
                    detail=f"status={status}",
                )
            )
        units = record.get("units") or {}
        if not units.get("plausible", True) and not (
            units.get("confirmed") or confirmations.get("units")
        ):
            out.append(
                check(
                    "UNIT_CONFIRMATION_REQUIRED",
                    "block",
                    "읽은 크기가 예상한 셀 크기와 맞나요? 단위를 확인하세요.",
                    node_id=node_id,
                    actions=["confirm_units"],
                    detail=f"dimensions_mm={record.get('dimensions_mm')}",
                )
            )
    return out, hashes


def _thickness_check(
    entry: dict[str, Any], graph: dict[str, Any], assets: AssetStore
) -> list[dict[str, Any]]:
    """STEP case thickness vs the gauged wall thickness (``STEP_THICKNESS_MISMATCH_MAX``).

    Only against the asset **this case's own geometry node** references: a
    draft with two chains (say a 0.4 mm can and a 0.1 mm foil sample) used to
    flag both cases against both assets, so one honest case was blocked by the
    other case's geometry.
    """
    case = entry["yaml"]
    geometry = case.get("geometry") or {}
    if geometry.get("kind") != "step":
        return []
    thickness = geometry.get("thickness")
    if thickness is None:
        return []
    node_id = entry.get("geometry_node_id")
    asset_id = (graph.get("asset_refs") or {}).get(node_id)
    if not asset_id:
        return []
    record = assets.read_record(str(asset_id))
    if not record or record.get("status") != "ready":
        return []
    parts = [p for p in record.get("parts") or [] if p.get("wall_thickness_mm")]
    if not parts:
        return []
    gauged = float(max(parts, key=lambda p: p.get("volume_mm3") or 0.0)["wall_thickness_mm"])
    if gauged <= 0:
        return []
    error = abs(float(thickness) - gauged) / gauged
    if error <= STEP_THICKNESS_MISMATCH_MAX:
        return []
    return [
        check(
            "THICKNESS_MISMATCH",
            "block",
            (
                f"입력한 두께 {thickness} mm가 측정된 벽두께 {gauged:.3f} mm와 "
                f"{error * 100:.0f} % 다릅니다."
            ),
            node_id=str(node_id),
            field_path="geometry.thickness",
            actions=["use_measured_thickness", "fix_value"],
            detail=f"limit={STEP_THICKNESS_MISMATCH_MAX}",
        )
    ]


def _material_checks(base: Path, case: dict[str, Any]) -> list[dict[str, Any]]:
    """Unverified material cards - a review item, never a block (UI_001 §8.3)."""
    out: list[dict[str, Any]] = []
    keys = [
        ("material.key", (case.get("material") or {}).get("key")),
        (
            "geometry.vent.material",
            ((case.get("geometry") or {}).get("vent") or {}).get("material"),
        ),
    ]
    for field_path, key in keys:
        if not key:
            continue
        entry = capabilities.material(base, str(key))
        if entry is None:
            out.append(
                check(
                    "INVALID_VALUE",
                    "block",
                    f"재료 카드를 찾을 수 없습니다: {key}",
                    field_path=field_path,
                    actions=["choose_material"],
                )
            )
        elif not entry.get("verified"):
            out.append(
                check(
                    "MATERIAL_NOT_VALIDATED",
                    "review",
                    f"재료 '{entry.get('name', key)}'는 검증되지 않았습니다. 결과는 경향 참고용입니다.",
                    field_path=field_path,
                    actions=["choose_material", "show_material_card"],
                    detail=str(entry.get("source")),
                )
            )
    return out


def _purpose_checks(graph: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Purpose availability and geometry support (UI_002 §2.4)."""
    purpose_id = graph.get("purpose")
    if not purpose_id:
        return []
    entry = capabilities.purpose(str(purpose_id))
    if entry is None:
        return [
            check(
                "PURPOSE_UNAVAILABLE",
                "block",
                f"알 수 없는 해석 목적입니다: {purpose_id}",
                actions=["choose_purpose"],
            )
        ]
    if not entry.get("available"):
        return [
            check(
                "PURPOSE_UNAVAILABLE",
                "block",
                str(entry.get("unavailable_reason") or "이 목적은 아직 실행할 수 없습니다."),
                actions=["choose_purpose"],
            )
        ]
    out: list[dict[str, Any]] = []
    supported = entry.get("supported_geometry") or []
    for case in cases:
        kind = (case.get("yaml", {}).get("geometry") or {}).get("kind")
        if kind and supported and kind not in supported:
            out.append(
                check(
                    "GEOMETRY_UNSUPPORTED",
                    "block",
                    f"'{entry['title']}' 목적은 {kind} 형상을 아직 지원하지 않습니다.",
                    actions=["choose_geometry", "choose_purpose"],
                    detail=f"supported={supported}",
                )
            )
            break
    return out


def _combinations(graph: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, int]:
    """The sweep table shown next to the run button."""
    solvers = {c.get("solver_node_id") for c in cases}
    mesh = mat = load = 0
    for solver_id in solvers:
        if not solver_id:
            continue
        mesh += len(graphc.sources(graph, solver_id, "mesh"))
        mat += len(graphc.sources(graph, solver_id, "mat"))
        load += len(graphc.sources(graph, solver_id, "load"))
    return {"mesh": mesh, "material": mat, "load": load, "total": len(cases)}


def build(
    base: Path,
    graph: dict[str, Any],
    *,
    solver_node_id: str | None = None,
    assets: AssetStore,
) -> dict[str, Any]:
    """Compile, check and estimate one graph draft. Returns the §2.4 body."""
    resolved = resolve_assets(base, graph, assets)
    compiled = graphc.compile_graph(resolved, solver_node_id=solver_node_id)
    checks: list[dict[str, Any]] = list(compiled["errors"])
    cases = compiled["cases"]

    asset_checks, asset_hashes = _asset_checks(resolved, assets)
    checks.extend(asset_checks)
    checks.extend(_purpose_checks(resolved, cases))

    for entry in cases:
        case = entry["yaml"]
        message = _validate_case(case)
        if message:
            checks.append(
                check(
                    "INVALID_VALUE",
                    "block",
                    "케이스 설정에 오류가 있습니다.",
                    actions=["fix_value"],
                    detail=message,
                )
            )
        checks.extend(_vent_checks(case))
        checks.extend(_thickness_check(entry, resolved, assets))
        checks.extend(_material_checks(base, case))

    # Two cases with one name would share an execution directory and the
    # second submit would overwrite the first snapshot - the user would see
    # one run where two were promised. Blocked before it can happen.
    names = [c["name"] for c in cases]
    for name in sorted({n for n in names if names.count(n) > 1}):
        checks.append(
            check(
                "DUPLICATE_CASE_NAME",
                "block",
                f"케이스 이름이 겹칩니다: {name}. 브랜치 태그나 케이스 접두어를 다르게 하세요.",
                actions=["rename_case", "set_branch_tag"],
                detail=f"name={name}",
            )
        )

    # De-duplicate: a sweep repeats the same material/vent finding per case.
    seen: set[tuple[Any, ...]] = set()
    unique_checks: list[dict[str, Any]] = []
    for entry in checks:
        key = (entry["code"], entry["message"], entry.get("node_id"), entry.get("field_path"))
        if key in seen:
            continue
        seen.add(key)
        unique_checks.append(entry)
    checks = unique_checks

    # One submission gets one estimate: a sweep that mixes presets has no
    # single measured sample, so it reports "추정 불가" rather than a blend.
    # The same applies when the user's own value replaced part of the preset -
    # the measured 14.4 minutes then describes a different model.
    preset_ids = {c.get("preset_id") for c in cases}
    sample_id = next(iter(preset_ids)) if len(preset_ids) == 1 else None
    overridden = {k: v for c in cases for k, v in (c.get("preset_overridden") or {}).items()}
    if overridden:
        sample_id = None
    estimate = estimates.estimate(sample_id, count=max(len(cases), 1))
    if estimate["available"]:
        if estimate["over_budget"]:
            checks.append(
                check(
                    "TIME_BUDGET_EXCEEDED",
                    "review",
                    (
                        f"예상 {estimate['per_run_min'][0]}–{estimate['per_run_min'][1]}분으로 "
                        "기본 시간 목표(30분)를 넘길 수 있습니다."
                    ),
                    actions=["show_30min_preset"],
                )
            )
    elif cases:
        checks.append(
            check(
                "ESTIMATE_UNAVAILABLE",
                "info",
                "이 형상의 실행 시간은 아직 추정할 수 없습니다.",
                actions=["show_reason"],
                detail=(
                    f"프리셋 값을 사용자 값이 대체했습니다: {overridden}"
                    if overridden
                    else str(estimate["reason"])
                ),
            )
        )
    applied = {k: v for c in cases for k, v in (c.get("preset_applied") or {}).items()}
    if applied:
        preset = capabilities.mesh_preset(
            resolved.get("purpose"), cases[0].get("preset_id")
        )
        checks.append(
            check(
                "PRESET_APPLIED",
                "info",
                (
                    f"'{preset['label']}' 설정을 적용했습니다. {preset.get('badge', '')}".strip()
                    if preset
                    else "검증된 템플릿의 추천값을 적용했습니다."
                ),
                actions=["show_preset_evidence"],
                detail=json.dumps(
                    {"applied": applied, "overridden": overridden}, ensure_ascii=False
                ),
            )
        )

    hash_value = input_hash([c["yaml"] for c in cases], asset_hashes)
    runnable = bool(cases) and not any(c["severity"] == "block" for c in checks)
    preflight_id = str(uuid.uuid4())
    return {
        "id": preflight_id,
        "input_hash": hash_value,
        "runnable": runnable,
        "purpose": resolved.get("purpose"),
        "preset_id": sample_id,
        "graph_revision": resolved.get("revision"),
        "targets": list(resolved.get("targets") or []),
        "cases": [
            {
                "exec_id": exec_id_for(c["name"], hash_value, c.get("solver_node_id")),
                "name": c["name"],
                "file": c["file"],
                "solver_node_id": c.get("solver_node_id"),
                "geometry_node_id": c.get("geometry_node_id"),
                "preset_id": c.get("preset_id"),
                "preset_applied": c.get("preset_applied") or {},
                "preset_overridden": c.get("preset_overridden") or {},
                "yaml": c["yaml"],
                "changed_vs_first": c.get("changed_vs_first") or {},
            }
            for c in cases
        ],
        "combinations": _combinations(resolved, cases),
        "checks": checks,
        "estimate": estimate,
        "policy_version": capabilities.POLICY_VERSION,
    }


__all__ = [
    "PreflightStore",
    "build",
    "check",
    "exec_id_for",
    "input_hash",
    "normalised_yaml",
    "resolve_assets",
]
