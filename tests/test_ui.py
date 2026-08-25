"""FR-11 web UI tests: API surface and part identification (no browser needed)."""

from __future__ import annotations

import json
from pathlib import Path

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
