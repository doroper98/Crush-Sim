"""FR-11 web UI tests: API surface and part identification (no browser needed)."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crushsim.ui.server import create_app  # noqa: E402
from crushsim.ui.viewergen import _canonical_parts  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: a minimal Crush-Sim checkout under tmp_path
# ---------------------------------------------------------------------------

_CASE_YAML = {
    "name": "ui_case",
    "load_case": "LC-1",
    "description": "UI smoke case",
    "geometry": {
        "kind": "parametric_can",
        "radius": 33.0,
        "height": 115.0,
        "thickness": 0.1,
    },
    "material": {"key": "aluminum_3003"},
    "loading": {"tool": "platen", "stroke": 40.0},
    "output": {"dir": "runs/ui_case"},
}


@pytest.fixture()
def ui_root(tmp_path: Path) -> Path:
    cases = tmp_path / "configs" / "cases"
    cases.mkdir(parents=True)
    # Capabilities and the preflight read the real material cards: an
    # unverified card is what MATERIAL_NOT_VALIDATED and the result caveat
    # are built from, so a fixture with invented cards would test nothing.
    shutil.copytree(Path("configs/materials"), tmp_path / "configs" / "materials")
    (cases / "ui_case.yaml").write_text(yaml.safe_dump(_CASE_YAML), encoding="utf-8")
    (cases / "broken.yaml").write_text("name: [unclosed", encoding="utf-8")

    run = tmp_path / "runs" / "done_run"
    run.mkdir(parents=True)
    (run / "pipeline_summary.json").write_text(
        json.dumps(
            {
                "case": "ui_case",
                "load_case": "LC-1",
                "stages_completed": ["mesh", "deck", "solve", "post"],
                "post": {
                    "metrics": {"peak_load_N": 1234.5, "absorbed_energy_mJ": 6789.0},
                    "energy": {"energy_error": 0.012, "gate": {"passed": True}},
                },
            }
        ),
        encoding="utf-8",
    )
    (run / "report.html").write_text("<html></html>", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client(ui_root: Path) -> TestClient:
    return TestClient(create_app(ui_root))


# ---------------------------------------------------------------------------
# /api/cases
# ---------------------------------------------------------------------------


def test_cases_lists_valid_and_broken_yaml(client: TestClient) -> None:
    cases = client.get("/api/cases").json()
    by_file = {c["file"]: c for c in cases}
    assert by_file["ui_case.yaml"]["name"] == "ui_case"
    assert by_file["ui_case.yaml"]["tool"] == "platen"
    assert by_file["ui_case.yaml"]["stroke_mm"] == 40.0
    # A broken yaml is reported as an error entry, never hides the rest.
    assert "error" in by_file["broken.yaml"]


# ---------------------------------------------------------------------------
# /api/runs
# ---------------------------------------------------------------------------


def test_runs_lists_finished_run_metrics(client: TestClient) -> None:
    data = client.get("/api/runs").json()
    assert data["active"] == []
    (run,) = data["finished"]
    assert run["name"] == "done_run"
    assert run["peak_load_N"] == 1234.5
    assert run["energy_error"] == 0.012
    assert run["gate_passed"] is True
    assert run["report"] is True


# ---------------------------------------------------------------------------
# Workflow editor: raw case read + validated save
# ---------------------------------------------------------------------------


def test_case_raw_returns_yaml_mapping(client: TestClient) -> None:
    data = client.get("/api/cases/ui_case.yaml/raw").json()
    assert data["geometry"]["radius"] == 33.0
    assert data["loading"]["tool"] == "platen"


def test_save_case_roundtrip(client: TestClient, ui_root: Path) -> None:
    data = client.get("/api/cases/ui_case.yaml/raw").json()
    data["loading"]["stroke"] = 55.0
    assert client.put("/api/cases/edited.yaml", json=data).status_code == 200
    saved = yaml.safe_load((ui_root / "configs" / "cases" / "edited.yaml").read_text())
    assert saved["loading"]["stroke"] == 55.0


def test_save_case_rejects_invalid_case(client: TestClient, ui_root: Path) -> None:
    data = client.get("/api/cases/ui_case.yaml/raw").json()
    data["loading"]["tool"] = "banana"
    response = client.put("/api/cases/bad.yaml", json=data)
    assert response.status_code == 422
    # The invalid graph never lands on disk, and no probe file is left behind.
    cases = ui_root / "configs" / "cases"
    assert not (cases / "bad.yaml").exists()
    assert not list(cases.glob(".*probe*"))


def test_save_case_rejects_bad_filename(client: TestClient) -> None:
    assert client.put("/api/cases/..%2Fevil.yaml", json={}).status_code == 404
    assert client.put("/api/cases/notyaml.txt", json={}).status_code in (404, 422)


def test_start_run_unknown_case_is_404(client: TestClient) -> None:
    assert client.post("/api/runs/nope.yaml").status_code == 404


def test_start_run_queues_and_cancel_unknown_is_404(client: TestClient) -> None:
    # Launch requests enter a serialised queue (two engines contending for
    # the same cores spin-lock each other); the response reports the slot.
    response = client.post("/api/runs/ui_case.yaml")
    assert response.status_code == 200
    body = response.json()
    assert body["queued"] == "ui_case.yaml"
    assert body["position"] >= 1
    # Unknown cancels are 404; the queued/started run itself is drained by
    # the worker (the subprocess exits fast against the fixture root).
    assert client.delete("/api/runs/nope.yaml").status_code == 404


def test_viewer_for_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/nope/viewer").status_code == 404


def test_run_dir_traversal_is_rejected(client: TestClient) -> None:
    assert client.get("/api/runs/../configs/viewer").status_code == 404


def test_index_serves_spa(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Crush-Sim" in response.text


# ---------------------------------------------------------------------------
# Viewer part identification (behaviour-based, deck numbering varies)
# ---------------------------------------------------------------------------


def test_canonical_parts_identifies_roles_by_behaviour() -> None:
    # 4 parts x 3 quads; node blocks of 4 per quad.
    quads = np.arange(48, dtype=np.int64).reshape(12, 4)
    part = np.repeat([10, 20, 30, 40], 3).astype(np.int32)
    first = np.zeros((48, 3), dtype=np.float32)
    # Part 10 = can (largest): give it one extra quad.
    quads = np.vstack([quads, [[0, 1, 2, 3]]])
    part = np.append(part, 10).astype(np.int32)
    # Part 20 = floor: flat (all z equal) and static.
    first[12:24, 2] = 0.0
    # Part 30 = tool: tall-ish, moves between first and last frame.
    first[24:36, 2] = np.linspace(0.0, 5.0, 12)
    # Part 40 = support: tall-ish, static.
    first[36:48, 2] = np.linspace(0.0, 5.0, 12)
    last = first.copy()
    last[24:36] += 30.0  # only the tool moves

    canon = _canonical_parts(quads, part, first, last)
    assert set(canon[part == 10]) == {1}  # CAN
    assert set(canon[part == 20]) == {2}  # FLOOR
    assert set(canon[part == 30]) == {3}  # TOOL
    assert set(canon[part == 40]) == {4}  # SUPPORT


def test_graph_save_and_load_roundtrip(client: TestClient) -> None:
    graph = {
        "nodes": [{"id": "n1", "type": "geometry", "x": 10, "y": 10, "params": {}}],
        "edges": [],
    }
    assert client.put("/api/graphs/demo.json", json=graph).status_code == 200
    assert "demo.json" in client.get("/api/graphs").json()
    loaded = client.get("/api/graphs/demo.json").json()
    assert loaded["nodes"][0]["id"] == "n1"
    # Name policy and shape validation.
    assert client.put("/api/graphs/..%2Fevil.json", json=graph).status_code == 404
    assert client.put("/api/graphs/bad.json", json={"nodes": []}).status_code == 422




def test_prebuilt_graphs_compile_to_valid_cases(tmp_path) -> None:
    """The shipped workflow graphs must describe runnable analyses.

    The graph is the UI's source of truth for what an analysis is made of -
    a crush case stops at the can, a vent case adds 캡 and 벤트 on top of the
    same can - so a broken graph is a broken story, not just a broken file.
    Since WP1 the server's compiler (:mod:`crushsim.ui.graphc`) is the one the
    run uses, so this drives that compiler and feeds every case it emits
    through ``load_case``.
    """
    from crushsim.config import load_case
    from crushsim.ui.graphc import compile_graph

    graphs = sorted(Path("configs/graphs").glob("*.json"))
    assert graphs, "no prebuilt graphs shipped"
    seen_vent = seen_plain = False
    for path in graphs:
        graph = json.loads(path.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in graph["nodes"]}
        assert {"solver", "result"} <= {n["type"] for n in graph["nodes"]}, path.name
        for edge in graph["edges"]:  # every wire lands on real nodes
            assert edge["from"] in nodes and edge["to"] in nodes, (path.name, edge)

        compiled = compile_graph(graph)
        assert compiled["errors"] == [], (path.name, compiled["errors"])
        assert compiled["cases"], path.name
        for case in compiled["cases"]:
            target = tmp_path / case["file"]
            target.write_text(
                yaml.safe_dump(case["yaml"], allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            loaded = load_case(target)
            assert loaded.name == case["name"], path.name
            assert str(loaded.output.dir) == f"runs/{case['name']}", path.name
            if (case["yaml"]["geometry"]).get("vent"):
                assert loaded.geometry.closed_top is True, path.name
                assert loaded.geometry.vent["pattern"] in ("perimeter", "petal_x")
                seen_vent = True
            else:
                seen_plain = True
    assert seen_vent and seen_plain, "expected both a vent graph and a plain-can graph"


# ---------------------------------------------------------------------------
# UI_002 §2.1 - capabilities
# ---------------------------------------------------------------------------


def test_capabilities_reports_purposes_presets_and_materials(client: TestClient) -> None:
    caps = client.get("/api/capabilities").json()
    assert caps["schema_version"] == 1
    assert caps["upload"]["extensions"] == [".stp", ".step"]
    assert caps["threads"] >= 1
    by_id = {p["id"]: p for p in caps["purposes"]}
    # Load-case ids live here and nowhere else (no hard-coding in the browser).
    assert by_id["vent_burst"]["load_case"] == "LC-3"
    assert by_id["vent_burst"]["supported_geometry"] == ["box_can"]
    # Electric/thermal are declared unavailable with a reason, not hidden.
    assert by_id["resistance"]["available"] is False
    assert by_id["resistance"]["unavailable_reason"]
    presets = {p["id"]: p for p in by_id["vent_burst"]["mesh_presets"]}
    assert presets["lc6_preset_30min"]["default"] is True
    assert presets["lc6_preset_30min"]["evidence"]["total_min"] == 14.4
    assert presets["lc6_preset_30min"]["evidence"]["laptop_calibrated"] is False
    assert presets["lc6_full_ramp"]["over_budget"] is True
    materials = {m["key"]: m for m in caps["materials"]}
    assert materials["ni_plated_steel_can"]["verified"] is False
    assert materials["aluminum_3003"]["t_default_mm"] > 0


# ---------------------------------------------------------------------------
# UI_002 §2.2 - assets
# ---------------------------------------------------------------------------


def test_upload_rejects_a_non_step_extension(client: TestClient) -> None:
    response = client.post(
        "/api/assets", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "UNSUPPORTED_FILE_TYPE"
    assert body["severity"] == "block"
    assert body["field_path"] == "file"


def test_upload_rejects_a_file_over_the_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crushsim.ui import capabilities as caps_mod

    monkeypatch.setattr(caps_mod, "MAX_UPLOAD_BYTES", 16)
    response = client.post(
        "/api/assets", files={"file": ("big.stp", b"x" * 64, "application/step")}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"


def test_unknown_asset_is_404_in_the_error_contract(client: TestClient) -> None:
    response = client.get("/api/assets/deadbeef")
    assert response.status_code == 404
    assert set(response.json()) == {
        "code",
        "message",
        "severity",
        "field_path",
        "node_id",
        "actions",
        "detail",
    }


@pytest.mark.slow
def test_uploaded_step_becomes_ready_with_dimensions_and_preview(client: TestClient) -> None:
    pytest.importorskip("OCP")
    source = Path("examples/step/cylin_can.stp")
    if not source.is_file():  # pragma: no cover - depends on the checkout
        pytest.skip("example STEP not present")
    response = client.post(
        "/api/assets",
        files={"file": ("cylin_can.stp", source.read_bytes(), "application/step")},
    )
    assert response.status_code == 202
    asset_id = response.json()["asset_id"]
    assert response.json()["status"] == "importing"
    deadline = time.time() + 240
    record: dict[str, Any] = {}
    while time.time() < deadline:
        record = client.get(f"/api/assets/{asset_id}").json()
        if record["status"] in ("ready", "failed"):
            break
        time.sleep(1.0)
    assert record["status"] == "ready", record.get("error")
    assert record["display_name"] == "cylin_can.stp"
    assert record["content_hash"].startswith("sha256:")
    assert record["units"]["declared"] == "mm"
    assert record["units"]["plausible"] is True
    assert max(record["dimensions_mm"]) == pytest.approx(95.0, abs=0.1)
    assert record["parts"][0]["wall_thickness_mm"] == pytest.approx(0.4, abs=0.05)
    assert record["parts"][0]["role_suggestion"] == "can"
    preview = client.get(f"/api/assets/{asset_id}/preview").json()
    assert preview["lod"] == "preview_3mm"
    assert len(preview["indices"]) == preview["triangles"] * 3


# ---------------------------------------------------------------------------
# UI_002 §2.4 - preflight
# ---------------------------------------------------------------------------


def _vent_graph() -> dict[str, Any]:
    graph = json.loads(Path("configs/graphs/vent_burst_study.json").read_text(encoding="utf-8"))
    graph.update(
        schema_version=2,
        revision=3,
        purpose="vent_burst",
        targets=[
            {
                "metric_key": "vent_opening_pressure",
                "lower": 0.3,
                "upper": 0.5,
                "unit": "MPa",
                "inclusive": True,
            }
        ],
    )
    return graph


def test_preflight_compiles_the_vent_study_graph(client: TestClient) -> None:
    body = client.post("/api/preflights", json={"graph": _vent_graph()}).json()
    assert body["runnable"] is True
    assert body["policy_version"]
    assert body["input_hash"].startswith("sha256:")
    assert body["combinations"] == {"mesh": 2, "material": 1, "load": 1, "total": 2}
    # Names come from the solver prefix + the branch tags, and the exec id
    # carries the first six characters of the input hash (UI_002 §1.2).
    short = body["input_hash"].split(":")[1][:6]
    assert [c["name"] for c in body["cases"]] == [
        "lc6_pris_vent_burst_v5_medium",
        "lc6_pris_vent_burst_v5_fine",
    ]
    assert body["cases"][0]["exec_id"] == f"lc6_pris_vent_burst_v5_medium_{short}"
    # The 30-minute preset fills the end time the graph leaves open, but the
    # user's own 0.3 mm branch is never overwritten by the preset's 0.5 mm.
    assert body["cases"][0]["yaml"]["solver"]["end_time"] == 0.0018
    assert body["cases"][1]["yaml"]["mesh"]["vent_size"] == 0.3
    assert body["cases"][1]["changed_vs_first"] == {"mesh.vent_size": [0.5, 0.3]}
    codes = {c["code"]: c for c in body["checks"]}
    assert codes["MATERIAL_NOT_VALIDATED"]["severity"] == "review"
    assert codes["PRESET_APPLIED"]["severity"] == "info"


def test_preflight_estimate_is_measured_or_absent(client: TestClient) -> None:
    graph = _vent_graph()
    # One branch only, and it leaves the preset's values in place -> the
    # measured lc6_preset_30min sample applies.
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "n5"]
    graph["edges"] = [e for e in graph["edges"] if e["from"] != "n5"]
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "n4"] + [
        {"id": "n4", "type": "mesh", "x": 0, "y": 0, "params": {"target_size": 1.2}}
    ]
    body = client.post("/api/preflights", json={"graph": graph}).json()
    estimate = body["estimate"]
    assert estimate["available"] is True
    assert estimate["per_run_min"] == [12, 17]
    assert estimate["total_min"] == [12, 17]
    assert estimate["hardware"] == "4-core container"
    assert estimate["over_budget"] is False
    assert "14.4" in estimate["basis"]

    # A graph with no purpose has no measured sample: no invented ETA.
    plain = _vent_graph()
    plain.pop("purpose")
    plain_body = client.post("/api/preflights", json={"graph": plain}).json()
    assert plain_body["estimate"]["available"] is False
    assert plain_body["estimate"]["per_run_min"] is None
    assert "ESTIMATE_UNAVAILABLE" in {c["code"] for c in plain_body["checks"]}


def test_preflight_blocks_an_unwired_solver(client: TestClient) -> None:
    graph = _vent_graph()
    graph["edges"] = [e for e in graph["edges"] if e["port"] != "mat"]
    body = client.post("/api/preflights", json={"graph": graph}).json()
    assert body["runnable"] is False
    assert body["cases"] == []
    assert body["checks"][0]["code"] == "GRAPH_INCOMPLETE"


def test_preflight_blocks_a_score_thicker_than_the_foil(client: TestClient) -> None:
    graph = _vent_graph()
    for node in graph["nodes"]:
        if node["type"] == "vent":
            node["params"]["score_thickness"] = 0.2  # foil is 0.1 mm
    body = client.post("/api/preflights", json={"graph": graph}).json()
    assert body["runnable"] is False
    blocked = [c for c in body["checks"] if c["severity"] == "block"]
    assert blocked[0]["code"] == "INVALID_VALUE"
    assert blocked[0]["field_path"] == "geometry.vent.score_thickness"


# ---------------------------------------------------------------------------
# UI_002 §2.5 - executions
# ---------------------------------------------------------------------------


def _fake_queue(client: TestClient, command: list[str]) -> None:
    """Point the queue at a harmless command - no solver ever runs in tests."""
    client.app.state.executions.command_builder = lambda exec_id: command


def _wait_for(client: TestClient, exec_id: str, states: set[str], timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    body: dict[str, Any] = {}
    while time.time() < deadline:
        body = client.get(f"/api/executions/{exec_id}").json()
        if body["state"] in states:
            return body
        time.sleep(0.05)
    return body


def test_execution_rejects_a_stale_input_hash(client: TestClient) -> None:
    _fake_queue(client, ["/bin/sh", "-c", "exit 0"])
    preflight = client.post("/api/preflights", json={"graph": _vent_graph()}).json()
    response = client.post(
        "/api/executions",
        json={
            "preflight_id": preflight["id"],
            "input_hash": "sha256:0000",
            "idempotency_key": "k",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "STALE_PREFLIGHT"
    assert response.json()["actions"] == ["rerun_preflight"]


def test_execution_is_idempotent_per_key(client: TestClient) -> None:
    _fake_queue(client, ["/bin/sh", "-c", "sleep 5"])
    preflight = client.post("/api/preflights", json={"graph": _vent_graph()}).json()
    payload = {
        "preflight_id": preflight["id"],
        "input_hash": preflight["input_hash"],
        "idempotency_key": "double-click",
    }
    first = client.post("/api/executions", json=payload)
    second = client.post("/api/executions", json=payload)
    assert first.status_code == 201
    ids = [e["exec_id"] for e in first.json()["executions"]]
    assert [e["exec_id"] for e in second.json()["executions"]] == ids
    # A repeated submit must not create a second copy of the same work
    # (the fixture's old run shows up too, marked legacy).
    items = client.get("/api/executions").json()["items"]
    assert sorted(i["exec_id"] for i in items if not i["legacy"]) == sorted(ids)
    assert [i["exec_id"] for i in items if i["legacy"]] == ["done_run"]
    for exec_id in ids:
        client.post(f"/api/executions/{exec_id}/cancel")


def test_execution_snapshot_is_frozen_and_written(client: TestClient, ui_root: Path) -> None:
    _fake_queue(client, ["/bin/sh", "-c", "sleep 5"])
    preflight = client.post("/api/preflights", json={"graph": _vent_graph()}).json()
    started = client.post(
        "/api/executions",
        json={
            "preflight_id": preflight["id"],
            "input_hash": preflight["input_hash"],
            "idempotency_key": "snap",
        },
    ).json()["executions"]
    exec_id = started[0]["exec_id"]
    snapshot_dir = ui_root / "runs" / "_ui" / "executions" / exec_id
    assert (snapshot_dir / "case.yaml").is_file()
    assert (snapshot_dir / "snapshot.json").is_file()
    case = yaml.safe_load((snapshot_dir / "case.yaml").read_text(encoding="utf-8"))
    # U20: the run writes to runs/<exec_id>, never back into configs/cases.
    assert case["output"]["dir"] == f"runs/{exec_id}"
    assert not (ui_root / "configs" / "cases" / f"{exec_id}.yaml").exists()
    snapshot = json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["input_hash"] == preflight["input_hash"]
    assert snapshot["targets"][0]["metric_key"] == "vent_opening_pressure"
    assert client.get(f"/api/executions/{exec_id}/case").status_code == 200
    for entry in started:
        client.post(f"/api/executions/{entry['exec_id']}/cancel")


def test_cancel_moves_running_to_cancelling_then_cancelled(client: TestClient) -> None:
    _fake_queue(client, ["/bin/sh", "-c", "sleep 30"])
    preflight = client.post("/api/preflights", json={"graph": _vent_graph()}).json()
    started = client.post(
        "/api/executions",
        json={
            "preflight_id": preflight["id"],
            "input_hash": preflight["input_hash"],
            "idempotency_key": "cancel",
        },
    ).json()["executions"]
    running = _wait_for(client, started[0]["exec_id"], {"running"})
    assert running["state"] == "running"
    cancelling = client.post(f"/api/executions/{started[0]['exec_id']}/cancel").json()
    # 'cancelled' is only confirmed once the process is really gone (§9.3).
    assert cancelling["state"] == "cancelling"
    done = _wait_for(client, started[0]["exec_id"], {"cancelled"})
    assert done["state"] == "cancelled"
    assert done["timestamps"]["finished"]
    # The second case never started: dequeuing is immediate.
    queued = client.post(f"/api/executions/{started[1]['exec_id']}/cancel").json()
    assert queued["state"] in ("cancelled", "cancelling")


def test_restart_marks_an_unfinished_execution_interrupted(ui_root: Path) -> None:
    """U25: a log file is never evidence that a run is still alive."""
    from crushsim.ui.executions import ExecutionManager

    exec_dir = ui_root / "runs" / "_ui" / "executions" / "ghost_run"
    exec_dir.mkdir(parents=True)
    (exec_dir / "log.txt").write_text("[   1s] [2/6] meshing\n", encoding="utf-8")
    (exec_dir / "state.json").write_text(
        json.dumps(
            {
                "exec_id": "ghost_run",
                "case_name": "ghost",
                "case_file": "ghost_run.yaml",
                "run_dir": "runs/ghost_run",
                "state": "running",
                "timestamps": {"submitted": None, "started": None, "finished": None},
            }
        ),
        encoding="utf-8",
    )
    manager = ExecutionManager(ui_root)
    assert manager.status("ghost_run")["state"] == "interrupted"
    assert manager.status("ghost_run")["diagnostics"][0]["code"] == "EXECUTION_INTERRUPTED"


# ---------------------------------------------------------------------------
# UI_002 §2.6 - results
# ---------------------------------------------------------------------------

_VENT_SUMMARY = {
    "case": "lc6_preset",
    "load_case": "LC-3",
    "material": "ni_plated_steel_can",
    "material_verified": False,
    "stages_completed": ["geometry", "meshing", "deck", "solver", "post", "report"],
    "meshes": {"can": {"gate": {"passed": True}}},
    "deck": {
        "parts": [
            {"name": "CAN", "role": "deformable", "part_id": 1},
            {"name": "VENT_MEMBRANE", "role": "deformable", "part_id": 2},
        ]
    },
    "solver": {
        "ok": True,
        "stages": [
            {"stage": "starter", "duration_s": 1.2},
            {"stage": "engine", "duration_s": 813.3},
        ],
    },
    "post": {
        # Measured on runs/lc6_pris_vent_burst_v5_preset (2026-09-07).
        "vent": {
            "initiation_MPa": 0.30345,
            "opening_MPa": 0.38533333333333336,
            "opening_area_fraction": 0.25,
            "vent_area_mm2": 233.6932565557797,
            "score_elements": 180,
            "score_ruptured": 179,
        },
        "energy": {
            "energy_error": 0.06661471589135248,
            "kinetic_over_internal": 0.0016772938053450604,
            "added_mass_ratio": 0.0,
            "gate": {"name": "solution", "passed": False},
        },
    },
}


def _finished_execution(
    root: Path, exec_id: str, summary: dict[str, Any], *, targets: list[dict[str, Any]]
) -> None:
    """A completed execution on disk: snapshot + state + run directory."""
    exec_dir = root / "runs" / "_ui" / "executions" / exec_id
    exec_dir.mkdir(parents=True)
    (exec_dir / "case.yaml").write_text(
        yaml.safe_dump({"name": exec_id, "pressure": {"peak": 0.5, "rise": 0.003}}),
        encoding="utf-8",
    )
    (exec_dir / "snapshot.json").write_text(
        json.dumps(
            {
                "exec_id": exec_id,
                "input_hash": "sha256:abc123",
                "purpose": "vent_burst",
                "preset_id": "lc6_preset_30min",
                "targets": targets,
                "case_name": exec_id,
            }
        ),
        encoding="utf-8",
    )
    (exec_dir / "state.json").write_text(
        json.dumps(
            {
                "exec_id": exec_id,
                "case_name": exec_id,
                "case_file": f"{exec_id}.yaml",
                "run_dir": f"runs/{exec_id}",
                "input_hash": "sha256:abc123",
                "state": "completed",
                "returncode": 0,
                "timestamps": {
                    "submitted": "2026-09-07T10:00:00+00:00",
                    "started": "2026-09-07T10:00:00+00:00",
                    "finished": "2026-09-07T10:14:24+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    run_dir = root / "runs" / exec_id
    run_dir.mkdir(parents=True)
    (run_dir / "pipeline_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")


_TARGET = [
    {
        "metric_key": "vent_opening_pressure",
        "lower": 0.3,
        "upper": 0.5,
        "unit": "MPa",
        "inclusive": True,
    }
]


def test_result_reports_metric_target_and_judgement(client: TestClient, ui_root: Path) -> None:
    _finished_execution(ui_root, "vent_done", _VENT_SUMMARY, targets=_TARGET)
    body = client.get("/api/executions/vent_done/result").json()
    metrics = {m["key"]: m for m in body["metrics"]}
    opening = metrics["vent_opening_pressure"]
    assert opening["value"] == pytest.approx(0.3853333, abs=1e-6)
    assert opening["unit"] == "MPa"
    assert opening["definition_id"] == "vent_open_area_25pct_v1"
    assert opening["unavailable_reason"] is None
    assert metrics["vent_initiation_pressure"]["value"] == pytest.approx(0.30345)
    # Target judged on the raw value, not on the rounded display value (U30).
    assert body["target_status"] == "within"
    assert body["judgement"]["sentence"] == (
        "개방 압력은 0.385 MPa로, 목표 0.30–0.50 MPa 안에 있습니다."
    )
    assert body["judgement"]["caveat"] == "재료 모델이 검증되지 않아 참고용으로 표시합니다."
    # Validity and target are independent axes (UI_001 §10.2).
    validation = body["validation"]
    assert validation["model_validity"] == "valid"
    assert validation["scope_status"] == "unverified"
    codes = {d["code"] for d in validation["diagnostics"]}
    assert "MATERIAL_NOT_VALIDATED" in codes
    assert "ENERGY_ERROR_ABOVE_GATE" in codes  # 6.7 % against a 5 % gate, non-fatal
    assert any(r["id"] == "lc6_preset_30min" for r in validation["references"])
    assert body["timing"]["stage_seconds"]["engine"] == 813.3
    assert body["timing"]["execution_seconds"] == pytest.approx(864.0)
    assert body["artifacts"]["report"] == "/api/runs/vent_done/report"


def test_result_of_an_unopened_vent_is_null_with_a_reason(
    client: TestClient, ui_root: Path
) -> None:
    summary = json.loads(json.dumps(_VENT_SUMMARY))
    summary["post"]["vent"]["opening_MPa"] = None
    _finished_execution(ui_root, "vent_shut", summary, targets=_TARGET)
    body = client.get("/api/executions/vent_shut/result").json()
    opening = {m["key"]: m for m in body["metrics"]}["vent_opening_pressure"]
    # U29: never "opening pressure 0", never an extrapolated pressure.
    assert opening["value"] is None
    assert opening["unavailable_reason"] == "NOT_REACHED_AT_MAX_PRESSURE"
    assert body["target_status"] == "unavailable"
    assert body["judgement"]["sentence"] == (
        "최대 가압 압력 0.50 MPa까지 개방 기준에 도달하지 않았습니다."
    )


def test_result_without_a_target_says_so(client: TestClient, ui_root: Path) -> None:
    _finished_execution(ui_root, "vent_notarget", _VENT_SUMMARY, targets=[])
    body = client.get("/api/executions/vent_notarget/result").json()
    assert body["target"] is None
    assert body["target_status"] == "none"
    assert "목표 범위를 입력하면" in body["judgement"]["sentence"]


def test_result_marks_a_failed_idealisation_gate_invalid(
    client: TestClient, ui_root: Path
) -> None:
    summary = json.loads(json.dumps(_VENT_SUMMARY))
    summary["meshes"]["can"]["gate"]["passed"] = False
    _finished_execution(ui_root, "vent_badmesh", summary, targets=_TARGET)
    body = client.get("/api/executions/vent_badmesh/result").json()
    assert body["validation"]["model_validity"] == "invalid"
    assert body["judgement"]["sentence"] == (
        "모델 연결 검사가 실패하여 설계 판단에 사용할 수 없습니다."
    )


def test_target_evaluation_uses_raw_values() -> None:
    from crushsim.ui.results import evaluate_target

    target = {"lower": 0.3, "upper": 0.5, "inclusive": True}
    assert evaluate_target(0.385, target) == "within"
    assert evaluate_target(0.56, target) == "above"
    assert evaluate_target(0.2, target) == "below"
    assert evaluate_target(0.5, target) == "within"
    assert evaluate_target(0.5, {**target, "inclusive": False}) == "above"
    assert evaluate_target(None, target) == "unavailable"
    assert evaluate_target(0.385, None) == "none"


# ---------------------------------------------------------------------------
# UI_002 §2.3 - graph drafts
# ---------------------------------------------------------------------------


def test_graph_save_detects_a_revision_conflict(client: TestClient) -> None:
    graph = {"nodes": [], "edges": [], "schema_version": 2, "revision": 4}
    graph["nodes"] = [{"id": "n1", "type": "geometry", "x": 0, "y": 0, "params": {}}]
    assert client.put("/api/graphs/draft.json", json=graph).json()["revision"] == 4
    stale = dict(graph, base_revision=2, revision=3)
    response = client.put("/api/graphs/draft.json", json=stale)
    assert response.status_code == 409
    assert response.json()["code"] == "REVISION_CONFLICT"
    # An up-to-date save still goes through and bumps the revision.
    fresh = dict(graph, base_revision=4, revision=5)
    assert client.put("/api/graphs/draft.json", json=fresh).json()["revision"] == 5
