"""UI_002 §2.1 - ``GET /api/capabilities``: what this server can actually do.

This module is the single source for the purpose list, the load-case ids, the
upload limits and the mesh presets. The browser hard-codes none of it (UI_002
§2.1): a purpose the backend cannot run appears here with ``available: false``
and a reason, so the front-end can grey the card out and say why instead of
failing at submit time.

Material cards are read from the three shipped roots with the same loader the
pipeline uses, so ``verified`` here is the same bit the report watermarks on.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..config import LOAD_CASES, load_material_card
from . import estimates

SCHEMA_VERSION = 1

#: Policy stamp carried by preflights and results (UI_002 §2.4/§2.6). Bump it
#: when the check list or the estimate table changes, so a stored preflight
#: from an older policy is recognisable.
POLICY_VERSION = "2026-09-07"

MAX_UPLOAD_BYTES = 52_428_800  # 50 MiB
UPLOAD_EXTENSIONS = (".stp", ".step")

#: Default thread count for one run. Solvers run strictly serially
#: (CLAUDE.md), so this is the whole machine's budget, not a per-user one.
DEFAULT_THREADS = 4

#: Material roots, most trustworthy first - the same order as
#: :func:`crushsim.config.find_material_card`.
MATERIAL_ROOTS = ("verified", "literature", "harvested")

#: Mesh/end-time presets offered for a purpose. The numbers are the case
#: settings; the timing evidence comes from :mod:`.estimates` so a measured
#: minute is never written down twice.
MESH_PRESETS: dict[str, list[dict[str, Any]]] = {
    "vent_burst": [
        {
            "id": "lc6_preset_30min",
            "label": "30분 목표 설정",
            "badge": "30분 목표 설정 · 컨테이너 4코어 실측 14분 · 기준 노트북 미교정",
            "vent_size_mm": 0.5,
            "end_time_s": 0.0018,
            "default": True,
        },
        {
            "id": "lc6_full_ramp",
            "label": "개방 이후 찢김까지 계산",
            "badge": "전구간(3.5 ms) · 컨테이너 4코어 실측 41분",
            "vent_size_mm": 0.5,
            "end_time_s": None,
            "default": False,
        },
    ]
}

_PURPOSES: list[dict[str, Any]] = [
    {
        "id": "lateral_crush",
        "title": "눌렀을 때의 반력",
        "load_case": "LC-2",
        "available": True,
        "template_id": "lateral_platen_v1",
        "template_version": 1,
        "supported_geometry": ["parametric_can", "box_can", "step"],
        "outputs": ["peak_load_N", "load_at_stroke_N", "absorbed_energy_J"],
    },
    {
        "id": "axial_crush",
        "title": "축방향 압궤·강도",
        "load_case": "LC-1",
        "available": True,
        "template_id": "axial_platen_v1",
        "template_version": 1,
        "supported_geometry": ["parametric_can", "step"],
        "outputs": ["peak_load_N", "mean_load_N", "absorbed_energy_J"],
    },
    {
        "id": "vent_burst",
        "title": "벤트 개방",
        "load_case": "LC-3",
        "available": True,
        "template_id": "box_vent_petal_v5",
        "template_version": 5,
        "supported_geometry": ["box_can"],
        "outputs": [
            "vent_initiation_pressure",
            "vent_opening_pressure",
            "vent_open_area_curve",
        ],
    },
    {
        "id": "resistance",
        "title": "전기저항",
        "available": False,
        "unavailable_reason": "백엔드 미구현 — 전기 전도도·접촉저항·단자 조건이 필요합니다",
    },
    {
        "id": "thermal",
        "title": "열 응답",
        "available": False,
        "unavailable_reason": "백엔드 미구현 — 열물성·발열·주변 온도·냉각 조건이 필요합니다",
    },
]

#: Tool kinds ``loading.tool`` accepts (crushsim.config._EXTRA_TOOL_KINDS).
TOOLS = ("platen", "jig_plane", "v_block", "indenter", "cylinder", "bead_roller")

FEATURES = {
    "ab_compare": True,
    "keyboard_graph_edit": False,
    "revision_conflict": "detect_only",
}

_material_cache: dict[str, list[dict[str, Any]]] = {}
_material_lock = threading.Lock()


def purposes() -> list[dict[str, Any]]:
    """The purpose list with its mesh presets and measured evidence attached."""
    out: list[dict[str, Any]] = []
    for row in _PURPOSES:
        entry = dict(row)
        presets = MESH_PRESETS.get(entry["id"])
        if presets:
            entry["mesh_presets"] = [
                {
                    **preset,
                    "evidence": estimates.evidence(preset["id"]),
                    "over_budget": estimates.estimate(preset["id"])["over_budget"],
                }
                for preset in presets
            ]
        out.append(entry)
    return out


def purpose(purpose_id: str | None) -> dict[str, Any] | None:
    """One purpose entry by id, presets included, or None."""
    if not purpose_id:
        return None
    return next((p for p in purposes() if p["id"] == purpose_id), None)


def mesh_preset(purpose_id: str | None, preset_id: str | None) -> dict[str, Any] | None:
    """One mesh preset of a purpose by id, or None."""
    if not preset_id:
        return None
    for preset in MESH_PRESETS.get(purpose_id or "", []):
        if preset["id"] == preset_id:
            return dict(preset)
    return None


def default_mesh_preset(purpose_id: str | None) -> dict[str, Any] | None:
    """The preset applied when the user has not chosen one."""
    for preset in MESH_PRESETS.get(purpose_id or "", []):
        if preset.get("default"):
            return dict(preset)
    return None


def materials(base: Path) -> list[dict[str, Any]]:
    """Every material card under ``configs/materials/{verified,literature,harvested}``.

    A card that fails to load is reported with its loader message rather than
    dropped - a silently missing material is how a case ends up pointing at a
    key that does not exist.
    """
    key = str(base)
    with _material_lock:
        cached = _material_cache.get(key)
    if cached is not None:
        return cached
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root_name in MATERIAL_ROOTS:
        root = base / "configs" / "materials" / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yaml")):
            if path.stem in seen:  # verified beats literature beats harvested
                continue
            seen.add(path.stem)
            try:
                card = load_material_card(path)
            except Exception as exc:  # noqa: BLE001 - one broken card must not hide the rest
                out.append({"key": path.stem, "root": root_name, "error": str(exc)})
                continue
            out.append(
                {
                    "key": card.key,
                    "name": card.name,
                    "verified": bool(card.is_verified),
                    "verification": card.verification,
                    "source": card.source,
                    "root": root_name,
                    "t_default_mm": card.t_default,
                    "sigma_y_MPa": card.sigma_y,
                    "uts_MPa": card.uts,
                }
            )
    with _material_lock:
        _material_cache[key] = out
    return out


def material(base: Path, key: str | None) -> dict[str, Any] | None:
    """One material entry by key, or None."""
    if not key:
        return None
    return next((m for m in materials(base) if m.get("key") == key), None)


def server_version(base: Path) -> str:
    """``<package version>+<git short hash>`` when the checkout is a git tree."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        pkg = version("crushsim")
    except Exception:  # noqa: BLE001 - a source checkout without metadata still serves
        pkg = "0"
    head = base / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref:"):
            ref = (base / ".git" / text.split(" ", 1)[1].strip()).read_text(encoding="utf-8")
            return f"{pkg}+{ref.strip()[:7]}"
        return f"{pkg}+{text[:7]}"
    except Exception:  # noqa: BLE001 - version is informational
        return pkg


def capabilities(base: Path) -> dict[str, Any]:
    """The full ``GET /api/capabilities`` body."""
    return {
        "schema_version": SCHEMA_VERSION,
        "server_version": server_version(base),
        "policy_version": POLICY_VERSION,
        "upload": {"max_bytes": MAX_UPLOAD_BYTES, "extensions": list(UPLOAD_EXTENSIONS)},
        "threads": DEFAULT_THREADS,
        "load_cases": list(LOAD_CASES),
        "purposes": purposes(),
        "materials": materials(base),
        "tools": list(TOOLS),
        "features": dict(FEATURES),
    }


__all__ = [
    "DEFAULT_THREADS",
    "MATERIAL_ROOTS",
    "MAX_UPLOAD_BYTES",
    "MESH_PRESETS",
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "TOOLS",
    "UPLOAD_EXTENSIONS",
    "capabilities",
    "default_mesh_preset",
    "material",
    "materials",
    "mesh_preset",
    "purpose",
    "purposes",
    "server_version",
]
