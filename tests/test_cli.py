"""CLI smoke tests (spec §13.5: every module is runnable on its own).

``csim doctor`` must run to completion on a machine with no solver installed,
and every failure path must exit non-zero with a readable message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from crushsim import __version__
from crushsim.cli import app

runner = CliRunner()


def test_help_lists_every_subcommand() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "convert",
        "harvest",
        "mesh",
        "deck",
        "run",
        "post",
        "report",
        "all",
        "doctor",
    ):
        assert command in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


@pytest.mark.parametrize(
    "command",
    ["convert", "harvest", "mesh", "deck", "run", "post", "report", "all", "doctor"],
)
def test_each_subcommand_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0


def test_doctor_runs_without_a_solver(tmp_path: Path) -> None:
    """The Phase-0 gate helper must never crash just because no solver exists."""
    config = tmp_path / "solver.yaml"
    config.write_text(
        yaml.safe_dump({"version_tag": "openradioss-test", "executables": {}}), encoding="utf-8"
    )
    result = runner.invoke(app, ["doctor", "--solver-config", str(config)])
    assert result.exit_code == 0, result.output
    assert "python" in result.output
    assert "gmsh mesh smoke test" in result.output
    assert "solver starter" in result.output


def test_doctor_with_a_missing_solver_config_still_runs(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--solver-config", str(tmp_path / "absent.yaml")])
    assert result.exit_code == 0
    assert "solver config" in result.output


def test_harvest_command(tmp_path: Path, legacy_material_file: Path) -> None:
    out = tmp_path / "cards"
    result = runner.invoke(
        app, ["harvest", "--input", str(legacy_material_file), "--outdir", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "verified: false" in result.output
    assert len(list(out.glob("*.yaml"))) > 200


def test_harvest_missing_input_exits_non_zero(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["harvest", "--input", str(tmp_path / "nope.ts"), "--outdir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "ERROR" in result.output


def test_mesh_command_on_the_parametric_can(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mesh",
            "--radius", "20",
            "--height", "40",
            "--thickness", "0.2",
            "--target-size", "8",
            "--outdir", str(tmp_path / "m"),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "m" / "mesh_summary.json").read_text(encoding="utf-8"))
    assert payload["can"]["gate"]["passed"] is True


def test_mesh_command_enforces_the_gate(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mesh",
            "--radius", "2",
            "--height", "3",
            "--thickness", "0.05",
            "--target-size", "0.25",
            "--outdir", str(tmp_path / "m"),
        ],
    )
    assert result.exit_code == 1
    assert "Mesh gate failed" in result.output


def test_mesh_command_can_skip_enforcement(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mesh",
            "--radius", "2",
            "--height", "3",
            "--thickness", "0.05",
            "--target-size", "0.25",
            "--no-gate",
            "--outdir", str(tmp_path / "m"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output


def test_post_without_csv_exits_non_zero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["post", "--outdir", str(tmp_path)])
    assert result.exit_code == 1
    assert "th_to_csv" in result.output


def test_run_without_arguments_exits_non_zero() -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "--config" in result.output


def test_deck_command_writes_files(repo_root: Path, tmp_path: Path) -> None:
    case = yaml.safe_load(
        (repo_root / "configs" / "cases" / "lc2_default.yaml").read_text(encoding="utf-8")
    )
    case["mesh"]["target_size"] = 8.0
    case["output"]["dir"] = str(tmp_path / "run")
    case["material"] = {
        "card": str(repo_root / "configs" / "materials" / "harvested" / "aluminum_3003.yaml")
    }
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")

    result = runner.invoke(app, ["deck", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "run" / "deck" / "lc2_default_0000.rad").is_file()
    assert (tmp_path / "run" / "deck" / "lc2_default_0001.rad").is_file()
    assert "validated against the pinned starter" in result.output


def test_all_command_with_skip_solver(repo_root: Path, tmp_path: Path) -> None:
    """`csim all --skip-solver` must complete geometry -> mesh -> deck -> report."""
    case = yaml.safe_load(
        (repo_root / "configs" / "cases" / "lc1_default.yaml").read_text(encoding="utf-8")
    )
    case["mesh"]["target_size"] = 8.0
    case["output"]["dir"] = str(tmp_path / "run")
    case["material"] = {
        "card": str(repo_root / "configs" / "materials" / "harvested" / "aluminum_3003.yaml")
    }
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")

    result = runner.invoke(app, ["all", "--config", str(path), "--skip-solver"])
    assert result.exit_code == 0, result.output
    run_dir = tmp_path / "run"
    assert (run_dir / "report.html").is_file()
    assert (run_dir / "pipeline_summary.json").is_file()
    assert (run_dir / "deck" / "lc1_default_0000.rad").is_file()

    summary = json.loads((run_dir / "pipeline_summary.json").read_text(encoding="utf-8"))
    assert summary["stages_completed"] == ["geometry", "meshing", "deck", "report"]
    assert summary["material_verified"] is False

    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "UNVERIFIED" in html


def test_all_command_without_a_solver_fails_loudly(repo_root: Path, tmp_path: Path) -> None:
    """Without --skip-solver and without OpenRadioss the run must abort (ADR-01)."""
    case = yaml.safe_load(
        (repo_root / "configs" / "cases" / "lc1_default.yaml").read_text(encoding="utf-8")
    )
    case["mesh"]["target_size"] = 8.0
    case["output"]["dir"] = str(tmp_path / "run")
    case["material"] = {
        "card": str(repo_root / "configs" / "materials" / "harvested" / "aluminum_3003.yaml")
    }
    case["solver"]["config"] = str(tmp_path / "solver.yaml")
    (tmp_path / "solver.yaml").write_text(
        yaml.safe_dump({"version_tag": "openradioss-test", "executables": {}}), encoding="utf-8"
    )
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")

    result = runner.invoke(app, ["all", "--config", str(path)])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_report_command_on_an_empty_run(repo_root: Path, tmp_path: Path) -> None:
    case = yaml.safe_load(
        (repo_root / "configs" / "cases" / "lc1_default.yaml").read_text(encoding="utf-8")
    )
    case["output"]["dir"] = str(tmp_path / "run")
    case["material"] = {
        "card": str(repo_root / "configs" / "materials" / "harvested" / "aluminum_3003.yaml")
    }
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")

    result = runner.invoke(app, ["report", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "run" / "report.html").is_file()
