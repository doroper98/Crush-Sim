"""UI_002 §1.4 - the authoritative graph compiler: GraphDraft -> case yaml.

This is the Python port of the browser's ``compileGraph()`` and, from WP1 on,
the definitive one: run names, combination counts and estimates all come from
the server (the browser copy stays only to draw the preview). The semantics are
deliberately identical to the JS:

* the geometry chain is walked backwards 캔 -> 캡 -> 벤트 -> 메쉬,
* mesh x material x load is a cartesian product (one case per combination),
* branch tags name the cases when more than one combination exists,
* a ``pressure`` load node writes ``pressure``/``loading.tool: none``, any
  other load node writes ``loading``,
* ``output.dir`` is ``runs/<name>`` - never ``configs/cases`` (U20).

Node params are used exactly as the graph file stores them; node-type defaults
are NOT merged in here, because the browser does not merge them when it loads a
saved graph either, and a compiler that silently invents a target size would
make the saved file and the executed case two different analyses.
"""

from __future__ import annotations

import re
from typing import Any

from . import capabilities

#: Graph draft schema this module writes (UI_002 §2.3). v1 drafts (no field)
#: still load - the missing keys simply read as their defaults.
GRAPH_SCHEMA_VERSION = 2

_NAME_SAFE = re.compile(r"[^A-Za-z0-9_\-]")
#: ``executions._EXEC_ID`` demands an alphanumeric first character, so a
#: prefix like "-draft" would compile into a runnable preflight and then 404
#: on submit. The two grammars are aligned here.
_NAME_LEAD = re.compile(r"^[^A-Za-z0-9]+")

#: Geometry keys copied from the 캔 node onto ``geometry``.
_GEOMETRY_KEYS = (
    "radius",
    "height",
    "thickness",
    "closed_bottom",
    "width",
    "depth",
    "step_path",
)
#: Keys copied from the 벤트 node onto ``geometry.vent``.
_VENT_KEYS = (
    "length",
    "width",
    "band",
    "membrane_thickness",
    "score_thickness",
    "pattern",
    "material",
    "eps_p_max",
    "arc_bulge",
)
#: Keys copied from a tool-load node onto ``loading``.
_LOADING_KEYS = (
    "tool",
    "direction",
    "stroke",
    "velocity",
    "ramp_fraction",
    "tool_gap",
    "tool_size",
    "indenter_radius",
    "height_frac",
    "support",
    "support_size",
    "clamp_can_base",
    "stage",
    "motion",
    "orbit_revs",
    "feed_revs",
    "extra_tools",
    "step_path",
)

_CHAIN_GUARD = 16  # same walk limit as the browser, so a cycle cannot hang us


def _error(code: str, message: str, *, node_id: str | None = None) -> dict[str, Any]:
    """One entry of the compiler's ``errors`` list, in the §2.7 shape."""
    return {
        "code": code,
        "message": message,
        "severity": "block",
        "field_path": None,
        "node_id": node_id,
        "actions": ["show_node"] if node_id else [],
        "detail": None,
    }


def _nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]


def sources(graph: dict[str, Any], node_id: str, port: str) -> list[dict[str, Any]]:
    """Upstream nodes wired into ``node_id``'s ``port``, in edge order."""
    by_id = {n["id"]: n for n in _nodes(graph)}
    out = []
    for edge in graph.get("edges") or []:
        if edge.get("to") == node_id and edge.get("port") == port:
            node = by_id.get(edge.get("from"))
            if node is not None:
                out.append(node)
    return out


def _params(node: dict[str, Any]) -> dict[str, Any]:
    params = node.get("params")
    return dict(params) if isinstance(params, dict) else {}


def _tag_of(node: dict[str, Any], fallback: str) -> str:
    tag = str(_params(node).get("tag") or "").strip()
    return tag or fallback


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted-path view of a case mapping, for diffs and stable hashing."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            out.update(flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
    else:
        out[prefix] = value
    return out


def changed_vs_first(first: dict[str, Any], other: dict[str, Any]) -> dict[str, list[Any]]:
    """``{path: [first_value, other_value]}`` for everything but name/output.dir.

    The identity fields always differ between two cases of one sweep, so
    showing them would bury the one condition the user actually changed.
    """
    skip = {"name", "output.dir"}
    a, b = flatten(first), flatten(other)
    diff: dict[str, list[Any]] = {}
    for path in sorted(set(a) | set(b)):
        if path in skip:
            continue
        if a.get(path) != b.get(path):
            diff[path] = [a.get(path), b.get(path)]
    return diff


def preset_for(graph: dict[str, Any], solver: dict[str, Any]) -> dict[str, Any] | None:
    """The mesh/end-time preset this solver node runs under, or None.

    Looked up on the solver node first (``params.mesh_preset``), then on the
    draft (``graph["mesh_preset"]``), then the purpose's default. UI_002 §2.3
    lists ``mesh_preset`` among the draft-level keys.
    """
    purpose_id = graph.get("purpose")
    chosen = _params(solver).get("mesh_preset") or graph.get("mesh_preset")
    if chosen:
        return capabilities.mesh_preset(purpose_id, str(chosen))
    return capabilities.default_mesh_preset(purpose_id)


def _apply_preset(case: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    """Fill the preset's mesh refinement and end time into a case mapping.

    A preset is a *default*, not an override: a value the user set in the graph
    wins and is reported back as ``overridden``. Silently replacing an explicit
    0.3 mm sweep branch with the preset's 0.5 mm collapsed two cases into one
    identical case in testing - and the estimate would then have claimed a
    measured 14.4 minutes for a model nobody measured.

    Returns:
        ``{"applied": {path: value}, "overridden": {path: [preset, user]}}``.
    """
    applied: dict[str, Any] = {}
    overridden: dict[str, Any] = {}
    mesh = case.setdefault("mesh", {})
    vent_size = preset.get("vent_size_mm")
    if vent_size is not None and case.get("geometry", {}).get("vent"):
        if mesh.get("vent_size") is None:
            mesh["vent_size"] = vent_size
            applied["mesh.vent_size"] = vent_size
        elif mesh["vent_size"] != vent_size:
            overridden["mesh.vent_size"] = [vent_size, mesh["vent_size"]]
    solver = case.setdefault("solver", {})
    end_time = preset.get("end_time_s")
    wanted = "auto" if end_time is None else end_time
    if solver.get("end_time") in (None, "auto"):
        solver["end_time"] = wanted
        applied["solver.end_time"] = wanted
    elif solver["end_time"] != wanted:
        overridden["solver.end_time"] = [wanted, solver["end_time"]]
    return {"applied": applied, "overridden": overridden}


def compile_graph(
    graph: dict[str, Any],
    *,
    solver_node_id: str | None = None,
    apply_presets: bool = True,
) -> dict[str, Any]:
    """Compile a GraphDraft into case mappings.

    Args:
        graph: A v1 or v2 graph draft (``nodes``/``edges`` plus the v2 keys).
        solver_node_id: Compile only this solver node; by default every solver
            node in the graph is compiled.
        apply_presets: Apply the purpose's mesh preset (UI_002 §3 WP1.2).

    Returns:
        ``{"cases": [{file, name, yaml, changed_vs_first, solver_node_id,
        preset_id}], "errors": [<§2.7 error>]}``. Errors are per solver node;
        a graph with one good and one broken solver still yields the good case.
    """
    cases: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    solvers = [n for n in _nodes(graph) if n.get("type") == "solver"]
    if solver_node_id is not None:
        solvers = [n for n in solvers if n["id"] == solver_node_id]
        if not solvers:
            errors.append(
                _error("GRAPH_INCOMPLETE", f"솔버 노드를 찾을 수 없습니다: {solver_node_id}")
            )
    for solver in solvers:
        meshes = sources(graph, solver["id"], "mesh")
        mats = sources(graph, solver["id"], "mat")
        loads = sources(graph, solver["id"], "load")
        contacts = sources(graph, solver["id"], "contact")
        if not meshes or not mats or not loads:
            errors.append(
                _error(
                    "GRAPH_INCOMPLETE",
                    "솔버 노드에 메쉬·재료·하중이 모두 연결되어야 합니다",
                    node_id=solver["id"],
                )
            )
            continue
        preset = preset_for(graph, solver) if apply_presets else None
        combos = len(meshes) * len(mats) * len(loads)
        sv = _params(solver)
        for mesh in meshes:
            for mat in mats:
                for load in loads:
                    built = _compile_one(
                        graph,
                        solver=solver,
                        sv=sv,
                        mesh=mesh,
                        mat=mat,
                        load=load,
                        contacts=contacts,
                        meshes=meshes,
                        mats=mats,
                        loads=loads,
                        combos=combos,
                        preset=preset,
                    )
                    if isinstance(built, dict) and "code" in built:
                        errors.append(built)
                        continue
                    cases.append(built)
    first = cases[0]["yaml"] if cases else None
    for entry in cases:
        entry["changed_vs_first"] = (
            {} if entry["yaml"] is first else changed_vs_first(first or {}, entry["yaml"])
        )
    return {"cases": cases, "errors": errors}


def _compile_one(
    graph: dict[str, Any],
    *,
    solver: dict[str, Any],
    sv: dict[str, Any],
    mesh: dict[str, Any],
    mat: dict[str, Any],
    load: dict[str, Any],
    contacts: list[dict[str, Any]],
    meshes: list[dict[str, Any]],
    mats: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    combos: int,
    preset: dict[str, Any] | None,
) -> dict[str, Any]:
    """One combination -> one case mapping, or one error dict."""
    geos = sources(graph, mesh["id"], "geom")
    if len(geos) != 1:
        return _error(
            "GRAPH_INCOMPLETE", "메쉬 노드마다 형상 1개가 연결되어야 합니다", node_id=mesh["id"]
        )
    chain: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = geos[0]
    guard = 0
    while cur is not None and guard < _CHAIN_GUARD:
        guard += 1
        chain.insert(0, cur)
        if cur.get("type") == "geometry":
            break
        up = sources(graph, cur["id"], "geom")
        cur = up[0] if up else None
    if not chain or chain[0].get("type") != "geometry":
        return _error(
            "GRAPH_INCOMPLETE", "형상 체인의 시작은 '형상(캔)' 노드여야 합니다", node_id=mesh["id"]
        )
    cap = next((n for n in chain if n.get("type") == "cap"), None)
    vent = next((n for n in chain if n.get("type") == "vent"), None)
    if vent is not None and cap is None:
        return _error("GRAPH_INCOMPLETE", "벤트 노드는 캡 노드 뒤에 연결하세요", node_id=vent["id"])

    geo = _params(chain[0])
    name = _NAME_LEAD.sub("", _NAME_SAFE.sub("_", str(sv.get("prefix") or "graph_case")))
    name = name or "graph_case"
    if combos > 1:
        parts = []
        if len(meshes) > 1:
            parts.append(_tag_of(mesh, f"m{meshes.index(mesh) + 1}"))
        if len(mats) > 1:
            parts.append(_tag_of(mat, f"a{mats.index(mat) + 1}"))
        if len(loads) > 1:
            parts.append(_tag_of(load, f"l{loads.index(load) + 1}"))
        name += "_" + "_".join(parts)

    case: dict[str, Any] = {"name": name, "load_case": str(sv.get("load_case") or "LC-1")}
    geometry: dict[str, Any] = {"kind": geo.get("kind")}
    for key in _GEOMETRY_KEYS:
        if geo.get(key) is not None:
            geometry[key] = geo[key]
    if cap is not None:  # the 캡 node IS the closed top; the vent rides on it
        geometry["closed_top"] = True
    if vent is not None:
        vp = _params(vent)
        vent_map = {k: vp[k] for k in _VENT_KEYS if vp.get(k) is not None and vp.get(k) != ""}
        if vent_map.get("length") and vent_map.get("width"):
            geometry["vent"] = vent_map
    case["geometry"] = geometry

    material: dict[str, Any] = {"key": _params(mat).get("key")}
    if _params(mat).get("eps_p_max") is not None:
        material["eps_p_max"] = _params(mat)["eps_p_max"]
    case["material"] = material

    mesh_params = _params(mesh)
    mesh_map: dict[str, Any] = {"recombine": True}
    if mesh_params.get("target_size") is not None:
        mesh_map["target_size"] = mesh_params["target_size"]
    if mesh_params.get("vent_size"):
        mesh_map["vent_size"] = mesh_params["vent_size"]
    if mesh_params.get("imperfection_mm") is not None:
        mesh_map["imperfection_mm"] = mesh_params["imperfection_mm"]
    case["mesh"] = mesh_map

    load_params = _params(load)
    solver_map: dict[str, Any] = {}
    if load.get("type") == "pressure":
        loading: dict[str, Any] = {"tool": "none"}
        if load_params.get("clamp_can_base"):
            loading["clamp_can_base"] = True
        if load_params.get("brace_walls"):
            loading["brace_walls"] = True
        case["loading"] = loading
        case["pressure"] = {k: load_params.get(k) for k in ("peak", "rise", "hold")}
        # Element deletion at burst removes internal energy from the balance;
        # the run must not stop on it (docs/VENT_BURST.md).
        solver_map["stop_on_energy_error"] = False
    else:
        case["loading"] = {k: load_params[k] for k in _LOADING_KEYS if load_params.get(k) is not None}

    if contacts:
        case["contact"] = {"friction": _params(contacts[0]).get("friction")}

    solver_out: dict[str, Any] = {"config": "configs/solver.yaml", "end_time": "auto"}
    solver_out.update(solver_map)
    if sv.get("threads"):
        solver_out["threads"] = sv["threads"]
    if sv.get("animation_frames"):
        solver_out["animation_frames"] = sv["animation_frames"]
    case["solver"] = solver_out
    case["output"] = {
        # Always POSIX: the case yaml is compared byte-for-byte across
        # machines, and a Windows backslash made the shipped-graph test fail
        # on the windows-latest CI leg.
        "dir": f"runs/{name}",
        "render": bool(sv.get("render")),
        "report": bool(sv.get("report")),
    }
    preset_result = (
        _apply_preset(case, preset) if preset is not None else {"applied": {}, "overridden": {}}
    )
    return {
        "file": f"{name}.yaml",
        "name": name,
        "yaml": case,
        "solver_node_id": solver["id"],
        "geometry_node_id": chain[0]["id"],
        "mesh_node_id": mesh["id"],
        "preset_id": preset["id"] if preset else None,
        "preset_applied": preset_result["applied"],
        "preset_overridden": preset_result["overridden"],
        "changed_vs_first": {},
    }


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "sources",
    "changed_vs_first",
    "compile_graph",
    "flatten",
    "preset_for",
]
