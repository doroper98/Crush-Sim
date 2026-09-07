"""UI_002 §1.4 - the server-side graph compiler is the authoritative one.

These tests pin the semantics the browser's ``compileGraph()`` had, because
run names, case counts and estimates are now read from the server: a silent
change here would rename runs and invalidate every stored comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crushsim.ui.graphc import changed_vs_first, compile_graph

# ---------------------------------------------------------------------------
# Graph fixtures (built by hand: a graph is nodes + wires, nothing else)
# ---------------------------------------------------------------------------


def node(node_id: str, node_type: str, **params: Any) -> dict[str, Any]:
    """One graph node; ``params`` may include a geometry ``kind``."""
    return {"id": node_id, "type": node_type, "x": 0, "y": 0, "params": params}


def edge(src: str, dst: str, port: str) -> dict[str, Any]:
    return {"from": src, "to": dst, "port": port}


def vent_graph() -> dict[str, Any]:
    """캔 -> 캡 -> 벤트 -> 메쉬 -> 솔버, pressure-driven (LC-3)."""
    return {
        "schema_version": 2,
        "revision": 1,
        "purpose": "vent_burst",
        "nodes": [
            node(
                "n1",
                "geometry",
                kind="box_can",
                width=120.5,
                depth=13.1,
                height=65.0,
                thickness=0.6,
                closed_bottom=True,
            ),
            node("n2", "cap", note="merged nodes"),
            node(
                "n3",
                "vent",
                length=30.0,
                width=7.0,
                band=1.0,
                membrane_thickness=0.1,
                score_thickness=0.03,
                pattern="petal_x",
                material="al1050_foil",
                eps_p_max=0.4,
            ),
            node("n4", "mesh", target_size=1.2, tag="medium"),
            node("n5", "material", key="ni_plated_steel_can", eps_p_max=0.35),
            node("n6", "pressure", peak=0.85, rise=0.003, hold=0.0005, clamp_can_base=True),
            node("n7", "contact", friction=0.15),
            node(
                "n8",
                "solver",
                prefix="vent_case",
                load_case="LC-3",
                threads=4,
                animation_frames=40,
                render=True,
                report=True,
            ),
        ],
        "edges": [
            edge("n1", "n2", "geom"),
            edge("n2", "n3", "geom"),
            edge("n3", "n4", "geom"),
            edge("n4", "n8", "mesh"),
            edge("n5", "n8", "mat"),
            edge("n6", "n8", "load"),
            edge("n7", "n8", "contact"),
        ],
    }


def crush_graph() -> dict[str, Any]:
    """캔 -> 메쉬 -> 솔버 with a tool load (LC-2), no cap and no vent."""
    return {
        "nodes": [
            node("g", "geometry", kind="parametric_can", radius=33.0, height=115.0, thickness=0.1),
            node("m", "mesh", target_size=1.5),
            node("a", "material", key="aluminum_3003"),
            node("l", "loading", tool="platen", direction=[0, 0, -1], stroke=40.0, velocity=2.0),
            node("s", "solver", prefix="crush_case", load_case="LC-2", threads=4, render=False),
        ],
        "edges": [
            edge("g", "m", "geom"),
            edge("m", "s", "mesh"),
            edge("a", "s", "mat"),
            edge("l", "s", "load"),
        ],
    }


# ---------------------------------------------------------------------------
# Chain, branches, naming
# ---------------------------------------------------------------------------


def test_vent_chain_becomes_closed_top_plus_vent() -> None:
    result = compile_graph(vent_graph())
    assert result["errors"] == []
    (case,) = result["cases"]
    geometry = case["yaml"]["geometry"]
    # The 캡 node IS the closed top; the vent rides on it.
    assert geometry["closed_top"] is True
    assert geometry["vent"]["pattern"] == "petal_x"
    assert geometry["kind"] == "box_can"
    assert case["name"] == "vent_case"
    assert case["file"] == "vent_case.yaml"
    assert case["yaml"]["output"]["dir"] == "runs/vent_case"


def test_pressure_node_writes_pressure_and_disables_energy_stop() -> None:
    (case,) = compile_graph(vent_graph())["cases"]
    yaml_case = case["yaml"]
    assert yaml_case["loading"] == {"tool": "none", "clamp_can_base": True}
    assert yaml_case["pressure"] == {"peak": 0.85, "rise": 0.003, "hold": 0.0005}
    # Element deletion at burst removes internal energy from the balance.
    assert yaml_case["solver"]["stop_on_energy_error"] is False
    assert yaml_case["contact"] == {"friction": 0.15}


def test_tool_load_writes_loading_and_keeps_defaults_out() -> None:
    (case,) = compile_graph(crush_graph())["cases"]
    assert case["yaml"]["loading"]["tool"] == "platen"
    assert case["yaml"]["loading"]["stroke"] == 40.0
    assert "pressure" not in case["yaml"]
    assert case["yaml"]["solver"]["end_time"] == "auto"
    assert case["yaml"]["output"] == {"dir": "runs/crush_case", "render": False, "report": False}


def test_cartesian_product_names_cases_by_tag() -> None:
    graph = vent_graph()
    graph["nodes"].append(node("n9", "mesh", target_size=1.2, vent_size=0.3, tag="fine"))
    graph["nodes"].append(node("n10", "material", key="aluminum_3003"))
    graph["edges"].append(edge("n3", "n9", "geom"))
    graph["edges"].append(edge("n9", "n8", "mesh"))
    graph["edges"].append(edge("n10", "n8", "mat"))
    result = compile_graph(graph)
    names = [c["name"] for c in result["cases"]]
    # mesh x material, tag first then the fallback index for the untagged one.
    assert names == [
        "vent_case_medium_a1",
        "vent_case_medium_a2",
        "vent_case_fine_a1",
        "vent_case_fine_a2",
    ]
    assert len({c["yaml"]["output"]["dir"] for c in result["cases"]}) == 4


def test_changed_vs_first_reports_only_the_changed_condition() -> None:
    graph = vent_graph()
    graph["nodes"].append(node("n9", "mesh", target_size=1.2, vent_size=0.3, tag="fine"))
    graph["edges"].append(edge("n3", "n9", "geom"))
    graph["edges"].append(edge("n9", "n8", "mesh"))
    cases = compile_graph(graph)["cases"]
    assert cases[0]["changed_vs_first"] == {}
    # name and output.dir always differ in a sweep, so they are excluded.
    assert cases[1]["changed_vs_first"] == {"mesh.vent_size": [0.5, 0.3]}


def test_changed_vs_first_helper_ignores_identity_fields() -> None:
    a = {"name": "a", "output": {"dir": "runs/a"}, "mesh": {"target_size": 1.2}}
    b = {"name": "b", "output": {"dir": "runs/b"}, "mesh": {"target_size": 1.0}}
    assert changed_vs_first(a, b) == {"mesh.target_size": [1.2, 1.0]}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_default_preset_fills_vent_size_and_end_time() -> None:
    (case,) = compile_graph(vent_graph())["cases"]
    assert case["preset_id"] == "lc6_preset_30min"
    assert case["yaml"]["mesh"]["vent_size"] == 0.5
    assert case["yaml"]["solver"]["end_time"] == 0.0018
    assert case["preset_applied"] == {"mesh.vent_size": 0.5, "solver.end_time": 0.0018}
    assert case["preset_overridden"] == {}


def test_preset_never_overrides_a_value_the_user_set() -> None:
    graph = vent_graph()
    graph["nodes"][3]["params"]["vent_size"] = 0.3  # the mesh node
    (case,) = compile_graph(graph)["cases"]
    assert case["yaml"]["mesh"]["vent_size"] == 0.3
    assert case["preset_overridden"] == {"mesh.vent_size": [0.5, 0.3]}


def test_no_purpose_means_no_preset() -> None:
    graph = vent_graph()
    graph.pop("purpose")
    (case,) = compile_graph(graph)["cases"]
    assert case["preset_id"] is None
    assert case["yaml"]["solver"]["end_time"] == "auto"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_solver_without_material_is_graph_incomplete() -> None:
    graph = crush_graph()
    graph["edges"] = [e for e in graph["edges"] if e["port"] != "mat"]
    result = compile_graph(graph)
    assert result["cases"] == []
    assert result["errors"][0]["code"] == "GRAPH_INCOMPLETE"
    assert result["errors"][0]["node_id"] == "s"
    assert result["errors"][0]["severity"] == "block"


def test_vent_without_cap_is_rejected() -> None:
    graph = vent_graph()
    # Wire the vent straight onto the can, cutting the 캡 out of the chain.
    graph["edges"] = [e for e in graph["edges"] if e["to"] not in ("n2", "n3")]
    graph["edges"].append(edge("n1", "n3", "geom"))
    result = compile_graph(graph)
    assert result["cases"] == []
    assert "벤트" in result["errors"][0]["message"]


def test_chain_must_start_at_a_can() -> None:
    graph = vent_graph()
    graph["edges"] = [e for e in graph["edges"] if e["to"] != "n2"]
    result = compile_graph(graph)
    assert result["cases"] == []
    assert result["errors"][0]["code"] == "GRAPH_INCOMPLETE"


def test_solver_node_id_selects_one_solver() -> None:
    graph = vent_graph()
    assert compile_graph(graph, solver_node_id="n8")["cases"]
    missing = compile_graph(graph, solver_node_id="nope")
    assert missing["cases"] == []
    assert missing["errors"][0]["code"] == "GRAPH_INCOMPLETE"


# ---------------------------------------------------------------------------
# The shipped graphs
# ---------------------------------------------------------------------------


def test_shipped_graphs_compile_without_errors() -> None:
    for path in sorted(Path("configs/graphs").glob("*.json")):
        graph = json.loads(path.read_text(encoding="utf-8"))
        result = compile_graph(graph)
        assert result["errors"] == [], (path.name, result["errors"])
        assert result["cases"], path.name
