"""UI_002 §2.6 - the result contract: metrics, target, validity, judgement.

Three things are kept strictly apart (UI_001 §10.2):

* **metric** - the raw number, its unit and the definition it was measured by,
* **target** - whether that raw number meets the design target,
* **validation** - whether the calculation may be believed at all.

A result can be inside the target and still be ``unverified``; an unverified
material never turns a number into a pass, and an unopened vent is never
reported as "opening pressure 0" (U29) - it is ``null`` with
``NOT_REACHED_AT_MAX_PRESSURE``.

The judgement sentence is built here, on the server, so the browser never
invents wording for a number it does not understand (UI_002 §3 WP2 금지).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..post.vent_metrics import OPENING_AREA_FRACTION
from ..units import (
    ADDED_MASS_MAX,
    ENERGY_ERROR_MAX,
    KINETIC_TO_INTERNAL_MAX,
)
from . import capabilities, estimates

SCHEMA_VERSION = 1

#: UI_002 §2.6 metric table. ``source`` is where the value comes from in
#: ``pipeline_summary.json`` (or which file, for the curves).
METRIC_DEFS: dict[str, dict[str, Any]] = {
    "vent_initiation_pressure": {
        "label": "파단 개시 압력",
        "unit": "MPa",
        "definition_id": "vent_first_rupture_v1",
        "definition_version": 1,
        "kind": "scalar",
        "source": "post.vent.initiation_MPa",
    },
    "vent_opening_pressure": {
        "label": "개방 압력",
        "unit": "MPa",
        "definition_id": "vent_open_area_25pct_v1",
        "definition_version": 1,
        "kind": "scalar",
        "source": "post.vent.opening_MPa",
        "criterion": f"개구 면적 / 벤트 면적 >= {OPENING_AREA_FRACTION:.0%}",
    },
    "vent_open_area_curve": {
        "label": "개구 면적–시간",
        "unit": "mm² vs s",
        "definition_id": "vent_open_area_v1",
        "definition_version": 1,
        "kind": "curve",
        "source": "vent_metrics().area_curve",
        "x_unit": "s",
        "y_unit": "mm²",
    },
    "peak_load_N": {
        "label": "피크 하중",
        "unit": "N",
        "definition_id": "peak_load_v1",
        "definition_version": 1,
        "kind": "scalar",
        "source": "post.metrics.peak_load_N",
    },
    "absorbed_energy_J": {
        "label": "흡수 에너지",
        "unit": "J",
        "definition_id": "absorbed_energy_full_stroke_v1",
        "definition_version": 1,
        "kind": "scalar",
        "source": "post.metrics.absorbed_energy_mJ / 1000",
    },
    "load_displacement_curve": {
        "label": "하중–변위",
        "unit": "N vs mm",
        "definition_id": "load_displacement_v1",
        "definition_version": 1,
        "kind": "curve",
        "source": "force_displacement.csv",
        "x_unit": "mm",
        "y_unit": "N",
    },
    "pressure_time_curve": {
        "label": "압력–시간",
        "unit": "MPa vs s",
        "definition_id": "pressure_ramp_v1",
        "definition_version": 1,
        "kind": "curve",
        "source": "deck FUNCT CAN_PRESSURE_RAMP",
        "x_unit": "s",
        "y_unit": "MPa",
    },
}

#: Benchmarks the vent/crush models were compared against (UI_001 §10.2).
BENCHMARK_REFERENCES = (
    {"id": "B-1", "title": "축방향 압궤 벤치마크", "path": "docs/SPEC-v2.1.md §8"},
    {"id": "B-2", "title": "측면 압착 벤치마크", "path": "docs/SPEC-v2.1.md §8"},
    {"id": "B-3", "title": "메쉬 수렴 (0.3/0.5/0.65 mm)", "path": "docs/VENT_BURST.md"},
)

_NOT_REACHED = "NOT_REACHED_AT_MAX_PRESSURE"


def _num(value: float) -> str:
    """3 significant digits, trailing zeros dropped - the display rule (§10.4)."""
    text = f"{value:.3g}"
    if "e" in text or "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def _bound(value: float) -> str:
    """Target bounds and differences read better with two decimals below 10."""
    return f"{value:.2f}" if abs(value) < 10 else _num(value)


def _dig(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _metric(
    key: str,
    value: Any,
    *,
    unavailable_reason: str | None = None,
    points: list[list[float]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = METRIC_DEFS[key]
    out = {
        "key": key,
        "label": row["label"],
        "value": value,
        "unit": row["unit"],
        "kind": row["kind"],
        "definition_id": row["definition_id"],
        "definition_version": row["definition_version"],
        "unavailable_reason": unavailable_reason,
    }
    if row["kind"] == "curve":
        out["points"] = points or []
        out["x_unit"] = row.get("x_unit")
        out["y_unit"] = row.get("y_unit")
    if row.get("criterion"):
        out["criterion"] = row["criterion"]
    if extra:
        out.update(extra)
    return out


def _load_curve_csv(path: Path) -> list[list[float]]:
    """``force_displacement.csv`` -> [[displacement_mm, force_N], ...]."""
    if not path.is_file():
        return []
    import csv  # noqa: PLC0415

    points: list[list[float]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                points.append([float(row["displacement"]), float(row["force"])])
            except (KeyError, TypeError, ValueError):
                continue
    return points


def _curves(run_dir: Path, summary: dict[str, Any]) -> dict[str, list[list[float]]]:
    """Pressure and open-area curves, read from the deck/listing (cached).

    Parsing a multi-megabyte starter on every poll is what the pipeline cache
    exists to avoid, so the parsed curves are written next to the run once.
    """
    cache = run_dir / "ui_curves.json"
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    out: dict[str, list[list[float]]] = {"pressure_time_curve": [], "vent_open_area_curve": []}
    try:
        from .viewergen import _pressure_curve  # noqa: PLC0415 - pulls numpy/pyvista

        curve = _pressure_curve(run_dir, summary)
    except Exception:  # noqa: BLE001 - a missing deck must not fail the result
        curve = None
    if curve:
        out["pressure_time_curve"] = [
            [float(t), float(p)] for t, p in zip(curve.get("t", []), curve.get("y", []))
        ]
        area = curve.get("area") or {}
        vent_area = (_dig(summary, "post.vent.vent_area_mm2") or 0.0) / 100.0
        if area and vent_area:
            # The viewer curve carries % of vent area; the metric is mm².
            out["vent_open_area_curve"] = [
                [float(t), float(pct) * vent_area]
                for t, pct in zip(area.get("t", []), area.get("y", []))
            ]
    if out["pressure_time_curve"] or out["vent_open_area_curve"]:
        try:
            cache.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out


def evaluate_target(value: Any, target: dict[str, Any] | None) -> str:
    """``within | below | above | none | unavailable`` on the raw value (U30)."""
    if not target:
        return "none"
    if value is None:
        return "unavailable"
    lower, upper = target.get("lower"), target.get("upper")
    inclusive = bool(target.get("inclusive", True))
    if lower is not None:
        if value < lower or (not inclusive and value == lower):
            return "below"
    if upper is not None:
        if value > upper or (not inclusive and value == upper):
            return "above"
    return "within"


def judgement(
    metric: dict[str, Any] | None,
    target: dict[str, Any] | None,
    status: str,
    validation: dict[str, Any],
    *,
    max_pressure: float | None = None,
) -> dict[str, Any]:
    """The one-sentence answer to the design question (UI_001 §10.1 table)."""
    caveat = None
    if "MATERIAL_NOT_VALIDATED" in [d["code"] for d in validation.get("diagnostics", [])]:
        caveat = "재료 모델이 검증되지 않아 참고용으로 표시합니다."
    if validation.get("model_validity") == "invalid":
        return {
            "sentence": "모델 연결 검사가 실패하여 설계 판단에 사용할 수 없습니다.",
            "status": "unavailable",
            "caveat": caveat,
        }
    if metric is None:
        return {
            "sentence": "표시할 지표가 아직 없습니다.",
            "status": "unavailable",
            "caveat": caveat,
        }
    label, unit = metric["label"], metric["unit"]
    if metric.get("value") is None:
        if metric.get("unavailable_reason") == _NOT_REACHED and max_pressure is not None:
            return {
                "sentence": f"최대 가압 압력 {_bound(max_pressure)} {unit}까지 개방 기준에 도달하지 않았습니다.",
                "status": "unavailable",
                "caveat": caveat,
            }
        return {
            "sentence": f"{label}을(를) 이 실행에서 얻지 못했습니다.",
            "status": "unavailable",
            "caveat": caveat,
        }
    value = float(metric["value"])
    shown = _num(value)
    if status == "within" and target:
        lower, upper = target.get("lower"), target.get("upper")
        span = f"{_bound(lower)}–{_bound(upper)}" if lower is not None and upper is not None else (
            f"{_bound(lower)} 이상" if lower is not None else f"{_bound(upper)} 이하"
        )
        sentence = f"{label}은 {shown} {unit}로, 목표 {span} {unit} 안에 있습니다."
    elif status == "above" and target and target.get("upper") is not None:
        gap = _bound(value - float(target["upper"]))
        sentence = f"{label}은 {shown} {unit}로, 목표 상한보다 {gap} {unit} 높습니다."
    elif status == "below" and target and target.get("lower") is not None:
        gap = _bound(float(target["lower"]) - value)
        sentence = f"{label}은 {shown} {unit}로, 목표 하한보다 {gap} {unit} 낮습니다."
    else:
        sentence = f"{label}은 {shown} {unit}입니다. 목표 범위를 입력하면 비교할 수 있습니다."
    return {"sentence": sentence, "status": status, "caveat": caveat}


def _diag(code: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **extra}


def validation_block(
    base: Path,
    summary: dict[str, Any] | None,
    snapshot: dict[str, Any],
    state: dict[str, Any],
    *,
    execution_minutes: float | None = None,
) -> dict[str, Any]:
    """``model_validity`` / ``scope_status`` / diagnostics / references (§2.6)."""
    diagnostics: list[dict[str, Any]] = []
    if summary is None:
        model_validity = "unknown"
    else:
        gates_failed = [
            name
            for name, entry in (summary.get("meshes") or {}).items()
            if (entry.get("gate") or {}).get("passed") is False
        ]
        solver_ok = (summary.get("solver") or {}).get("ok")
        energy = (summary.get("post") or {}).get("energy") or {}
        error = energy.get("energy_error")
        nan = error is not None and error != error  # NaN
        model_validity = "valid"
        if gates_failed:
            model_validity = "invalid"
            diagnostics.append(
                _diag(
                    "IDEALISATION_GATE_FAILED",
                    "block",
                    f"이상화 게이트 실패: {', '.join(gates_failed)}",
                    parts=gates_failed,
                )
            )
        if solver_ok is False or state.get("state") in ("failed", "interrupted"):
            model_validity = "invalid"
            diagnostics.append(
                _diag("SOLVER_FAILED", "block", "솔버가 정상 종료하지 않았습니다.")
            )
        if nan:
            model_validity = "invalid"
            diagnostics.append(_diag("NAN_IN_RESULT", "block", "결과에 NaN이 있습니다."))
        # Non-fatal gates: shown with their value, never silently swallowed and
        # never enough on their own to call the model wrong (UI_001 §10.2).
        if error is not None and not nan and abs(float(error)) > ENERGY_ERROR_MAX:
            diagnostics.append(
                _diag(
                    "ENERGY_ERROR_ABOVE_GATE",
                    "review",
                    f"에너지 오차 {float(error) * 100:.1f} % (게이트 {ENERGY_ERROR_MAX:.0%})",
                    value=float(error),
                    limit=ENERGY_ERROR_MAX,
                )
            )
        added = energy.get("added_mass_ratio")
        if added is not None and float(added) > ADDED_MASS_MAX:
            diagnostics.append(
                _diag(
                    "ADDED_MASS_ABOVE_GATE",
                    "review",
                    f"부가 질량 {float(added) * 100:.1f} % (게이트 {ADDED_MASS_MAX:.0%})",
                    value=float(added),
                    limit=ADDED_MASS_MAX,
                )
            )
        kinetic = energy.get("kinetic_over_internal")
        if kinetic is not None and float(kinetic) > KINETIC_TO_INTERNAL_MAX:
            diagnostics.append(
                _diag(
                    "KINETIC_ABOVE_GATE",
                    "review",
                    f"운동/내부 에너지 {float(kinetic) * 100:.1f} % (게이트 {KINETIC_TO_INTERNAL_MAX:.0%})",
                    value=float(kinetic),
                    limit=KINETIC_TO_INTERNAL_MAX,
                )
            )

    material_key = (summary or {}).get("material") or _dig(snapshot, "material.key")
    material_verified = (summary or {}).get("material_verified")
    if material_verified is None and material_key:
        entry = capabilities.material(base, str(material_key))
        material_verified = bool(entry and entry.get("verified"))
    if material_key and not material_verified:
        diagnostics.append(
            _diag(
                "MATERIAL_NOT_VALIDATED",
                "review",
                f"재료 '{material_key}'는 검증되지 않았습니다. 결과는 경향 참고용입니다.",
                material=material_key,
            )
        )
    if execution_minutes is not None and execution_minutes > estimates.TIME_BUDGET_MIN:
        diagnostics.append(
            _diag(
                "TIME_BUDGET_EXCEEDED_ACTUAL",
                "review",
                f"실측 실행 {execution_minutes:.0f}분으로 기본 시간 목표(30분)를 넘었습니다.",
                value=execution_minutes,
            )
        )

    preset_id = snapshot.get("preset_id")
    evidence = estimates.evidence(str(preset_id)) if preset_id else None
    purpose_entry = capabilities.purpose(snapshot.get("purpose"))
    if purpose_entry is not None and not purpose_entry.get("available"):
        scope_status = "out_of_scope"
    elif material_verified and evidence:
        scope_status = "verified"
    else:
        scope_status = "unverified"

    references: list[dict[str, Any]] = []
    if evidence:
        references.append(
            {
                "id": str(preset_id),
                "title": "메쉬·종료 시점 프리셋 실측",
                "detail": (
                    f"{evidence['run']}: 엔진 {evidence['engine_min']}분 / 총 "
                    f"{evidence['total_min']}분 ({evidence['hardware']}, {evidence['measured_on']})"
                ),
            }
        )
    references.extend(dict(row) for row in BENCHMARK_REFERENCES)

    return {
        "model_validity": model_validity,
        "scope_status": scope_status,
        "diagnostics": diagnostics,
        "references": references,
        "policy_version": capabilities.POLICY_VERSION,
    }


def _metrics_for(run_dir: Path, summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The metrics this run actually produced, in display order."""
    if summary is None:
        return []
    out: list[dict[str, Any]] = []
    vent = _dig(summary, "post.vent")
    post_metrics = _dig(summary, "post.metrics") or {}
    is_vent = vent is not None or _dig(summary, "geometry.box.vent") is not None
    if is_vent:
        initiation = (vent or {}).get("initiation_MPa")
        out.append(
            _metric(
                "vent_initiation_pressure",
                initiation,
                unavailable_reason=None if initiation is not None else _NOT_REACHED,
            )
        )
        opening = (vent or {}).get("opening_MPa")
        out.append(
            _metric(
                "vent_opening_pressure",
                opening,
                # U29: an unopened vent is null with a reason, never 0.
                unavailable_reason=None if opening is not None else _NOT_REACHED,
                extra={
                    "vent_area_mm2": (vent or {}).get("vent_area_mm2"),
                    "opening_area_fraction": (vent or {}).get(
                        "opening_area_fraction", OPENING_AREA_FRACTION
                    ),
                },
            )
        )
    if post_metrics.get("peak_load_N") is not None:
        out.append(_metric("peak_load_N", float(post_metrics["peak_load_N"])))
    if post_metrics.get("absorbed_energy_mJ") is not None:
        out.append(
            _metric("absorbed_energy_J", float(post_metrics["absorbed_energy_mJ"]) / 1000.0)
        )
    curves = _curves(run_dir, summary)
    if is_vent:
        out.append(
            _metric("pressure_time_curve", None, points=curves["pressure_time_curve"])
        )
        out.append(
            _metric(
                "vent_open_area_curve",
                None,
                points=curves["vent_open_area_curve"],
                unavailable_reason=None if curves["vent_open_area_curve"] else _NOT_REACHED,
            )
        )
    load_curve = _load_curve_csv(run_dir / "force_displacement.csv")
    if load_curve:
        out.append(_metric("load_displacement_curve", None, points=load_curve))
    return out


def _timing(
    state: dict[str, Any], status: dict[str, Any], run_dir: Path, summary: dict[str, Any] | None
) -> dict[str, Any]:
    """Queue/execution/stage seconds - from the state file, then from mtimes."""
    timing = dict(status.get("timing") or {})
    stage_seconds = dict(timing.get("stage_seconds") or {})
    if summary is not None:
        for stage in (summary.get("solver") or {}).get("stages") or []:
            stage_seconds[str(stage.get("stage"))] = round(float(stage.get("duration_s") or 0.0), 1)
    execution_seconds = timing.get("execution_seconds")
    if execution_seconds is None:
        # Legacy runs (no state file of ours): bracket the run by file mtimes.
        summary_path = run_dir / "pipeline_summary.json"
        deck = sorted((run_dir / "deck").glob("*_0000.rad"))
        if summary_path.is_file() and deck:
            execution_seconds = round(
                summary_path.stat().st_mtime - min(p.stat().st_mtime for p in deck), 1
            )
    return {
        "queue_seconds": timing.get("queue_seconds"),
        "execution_seconds": execution_seconds,
        "stage_seconds": stage_seconds,
        "threads": _dig(summary or {}, "deck.threads") or capabilities.DEFAULT_THREADS,
        "estimate_min": (status.get("snapshot") or {}).get("estimate_min"),
    }


def build(base: Path, exec_id: str, manager: Any) -> dict[str, Any]:
    """The UI_002 §2.6 result body for one execution."""
    state = manager.read_state(exec_id)
    snapshot = manager.read_snapshot(exec_id)
    status = manager.status(exec_id)
    run_dir = Path(base) / state.get("run_dir", f"runs/{exec_id}")
    summary_path = run_dir / "pipeline_summary.json"
    summary: dict[str, Any] | None = None
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None

    metrics = _metrics_for(run_dir, summary)
    execution_seconds = (status.get("timing") or {}).get("execution_seconds")
    validation = validation_block(
        Path(base),
        summary,
        snapshot,
        state,
        execution_minutes=(execution_seconds / 60.0) if execution_seconds else None,
    )
    targets = snapshot.get("targets") or []
    by_key = {m["key"]: m for m in metrics}
    target = next((t for t in targets if t.get("metric_key") in by_key), None)
    if target is None and targets:
        target = targets[0]
    metric = by_key.get(target.get("metric_key")) if target else None
    if metric is None:
        metric = next((m for m in metrics if m["kind"] == "scalar"), None)
    target_status = evaluate_target(metric.get("value") if metric else None, target)
    max_pressure = _dig(summary or {}, "post.vent.max_pressure_MPa") or snapshot.get(
        "pressure_peak_MPa"
    )
    if max_pressure is None:
        case_path = manager.case_path(exec_id)
        if case_path.is_file():
            import yaml  # noqa: PLC0415

            case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
            max_pressure = (case.get("pressure") or {}).get("peak")

    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": exec_id,
        "state": state.get("state"),
        "case_name": state.get("case_name"),
        "snapshot_hash": state.get("input_hash"),
        "changed_vs_first": snapshot.get("changed_vs_first") or {},
        "metrics": metrics,
        "target": target,
        "target_status": target_status,
        "judgement": judgement(
            metric, target, target_status, validation, max_pressure=max_pressure
        ),
        "validation": validation,
        "timing": _timing(state, status, run_dir, summary),
        "artifacts": status.get("artifacts"),
        "preset_id": snapshot.get("preset_id"),
        "purpose": snapshot.get("purpose"),
        "policy_version": capabilities.POLICY_VERSION,
    }


__all__ = [
    "BENCHMARK_REFERENCES",
    "METRIC_DEFS",
    "SCHEMA_VERSION",
    "build",
    "evaluate_target",
    "judgement",
    "validation_block",
]
