"""FR-05 solver wrapper tests: log parsing plus the missing-executable path.

No solver is installed in CI, so the tests focus on (a) parsing real-shaped log
text and (b) proving that a missing binary produces a loud, actionable error
rather than a silent fallback (ADR-01, spec §13.5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from crushsim.config import load_solver_config
from crushsim.errors import ConfigError, SolverError
from crushsim.solver.logparse import parse_log, tail
from crushsim.solver.runner import (
    RunResult,
    StageResult,
    _last_cycle_line,
    read_engine_end_time,
    resolve_executable,
    run_solver,
    solver_status,
    write_run_summary,
)

ENGINE_LOG = """
    OPENRADIOSS ENGINE
 CYCLE    TIME    TIME-STEP  ELEMENT      ERROR I-ENERGY  K-ENERGY  EXT-WORK  MAS.ERR
       0  0.0000E+00 0.9500E-07 SHELL   12   0.0%  0.000E+00  0.000E+00  0.000E+00   0.1%
     100  0.9500E-05 0.9400E-07 SHELL   12  -0.3%  1.250E+02  4.100E+00  1.290E+02   0.4%
     200  0.1900E-04 0.9300E-07 SHELL   40  -1.1%  4.480E+02  9.700E+00  4.570E+02   0.9%
 ** WARNING MINIMUM TIME STEP REACHED
     300  0.2800E-04 0.9200E-07 SHELL   40  -2.4%  8.900E+02  1.200E+01  9.010E+02   1.4%
 NORMAL TERMINATION
"""

FAILING_LOG = """
 CYCLE    TIME    TIME-STEP  ELEMENT      ERROR I-ENERGY
       0  0.0000E+00 0.1000E-06 SHELL   12   0.0%  0.000E+00
     100  0.1000E-04 0.1000E-09 SHELL   77  -9.7%  1.000E+02
 *** ERROR NEGATIVE VOLUME IN SOLID ELEMENT 991
 ERROR TERMINATION
"""


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def test_parse_log_collects_cycles() -> None:
    summary = parse_log(ENGINE_LOG)
    assert len(summary.cycles) == 4
    assert summary.cycles[0].cycle == 0
    assert summary.cycles[-1].cycle == 300


def test_parse_log_reads_time_and_timestep() -> None:
    summary = parse_log(ENGINE_LOG)
    assert summary.final_time == pytest.approx(2.8e-5)
    assert summary.min_timestep == pytest.approx(9.2e-8)


def test_parse_log_converts_percentages_to_fractions() -> None:
    summary = parse_log(ENGINE_LOG)
    assert summary.final_energy_error == pytest.approx(-0.024)
    assert summary.max_energy_error == pytest.approx(0.024)
    assert summary.max_mass_error == pytest.approx(0.014)


def test_parse_log_detects_normal_termination() -> None:
    assert parse_log(ENGINE_LOG).normal_termination is True


def test_parse_log_collects_warnings() -> None:
    summary = parse_log(ENGINE_LOG)
    assert summary.warnings
    assert "MINIMUM TIME STEP" in summary.warnings[0]


def test_parse_log_detects_negative_volume_and_errors() -> None:
    summary = parse_log(FAILING_LOG)
    assert summary.negative_volume is True
    assert summary.errors
    assert summary.normal_termination is False


def test_parse_log_detects_timestep_collapse() -> None:
    assert parse_log(FAILING_LOG).timestep_collapse is True
    assert parse_log(ENGINE_LOG).timestep_collapse is False


def test_parse_log_handles_fortran_d_exponents() -> None:
    summary = parse_log("      10  0.1000D-04 0.9500D-07 SHELL 1   0.5%  1.0D+02")
    assert summary.cycles[0].time == pytest.approx(1.0e-5)
    assert summary.cycles[0].timestep == pytest.approx(9.5e-8)


def test_parse_log_on_empty_text_is_empty_but_valid() -> None:
    summary = parse_log("")
    assert summary.cycles == []
    assert summary.max_energy_error is None


def test_log_summary_serialises() -> None:
    payload = parse_log(ENGINE_LOG).to_dict()
    assert payload["cycles_parsed"] == 4
    assert payload["normal_termination"] is True


def test_tail_returns_the_last_lines() -> None:
    text = "\n".join(str(i) for i in range(100))
    assert tail(text, 3).splitlines() == ["97", "98", "99"]


# ---------------------------------------------------------------------------
# Configuration and executable resolution
# ---------------------------------------------------------------------------


def test_repo_solver_config_is_pinned(repo_root: Path) -> None:
    """configs/solver.yaml must carry a pinned version tag (spec §6)."""
    paths = load_solver_config(repo_root / "configs" / "solver.yaml")
    assert paths.version_tag
    assert paths.version_tag != ""


def test_solver_config_without_version_tag_raises(tmp_path: Path) -> None:
    path = tmp_path / "solver.yaml"
    path.write_text(yaml.safe_dump({"executables": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="version_tag"):
        load_solver_config(path)


def test_missing_solver_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_solver_config(tmp_path / "nope.yaml")


def _empty_solver_config(tmp_path: Path) -> Path:
    path = tmp_path / "solver.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version_tag": "openradioss-test", "install_root": "", "executables": {}}
        ),
        encoding="utf-8",
    )
    return path


def test_missing_executable_raises_with_actionable_message(tmp_path: Path) -> None:
    paths = load_solver_config(_empty_solver_config(tmp_path))
    with pytest.raises(SolverError) as excinfo:
        resolve_executable(paths, "starter")
    message = str(excinfo.value)
    assert "starter" in message
    assert "openradioss-test" in message
    assert "executables.starter" in message


def test_resolve_rejects_unknown_stage(tmp_path: Path) -> None:
    paths = load_solver_config(_empty_solver_config(tmp_path))
    with pytest.raises(SolverError, match="Unknown solver stage"):
        resolve_executable(paths, "postprocessor")


def test_run_solver_missing_deck_raises(tmp_path: Path) -> None:
    with pytest.raises(SolverError, match="starter deck not found"):
        run_solver(
            tmp_path / "a_0000.rad",
            tmp_path / "a_0001.rad",
            solver_config=_empty_solver_config(tmp_path),
        )


def test_run_solver_without_binaries_raises_loudly(tmp_path: Path) -> None:
    """ADR-01: no solver means no result - never an approximation."""
    starter = tmp_path / "case_0000.rad"
    engine = tmp_path / "case_0001.rad"
    starter.write_text("/BEGIN\n", encoding="utf-8")
    engine.write_text("/RUN\n", encoding="utf-8")
    with pytest.raises(SolverError) as excinfo:
        run_solver(starter, engine, solver_config=_empty_solver_config(tmp_path))
    assert "not found" in str(excinfo.value)


def test_solver_status_reports_without_raising(tmp_path: Path) -> None:
    status = solver_status(_empty_solver_config(tmp_path))
    assert status["configured"] is True
    assert status["starter"] is None
    assert "starter_reason" in status


def test_solver_status_on_missing_config_reports_reason(tmp_path: Path) -> None:
    status = solver_status(tmp_path / "absent.yaml")
    assert status["configured"] is False
    assert status["reason"]


# ---------------------------------------------------------------------------
# run_summary.json
# ---------------------------------------------------------------------------


def test_run_summary_is_written(tmp_path: Path) -> None:
    result = RunResult(
        run_name="case",
        run_dir=tmp_path,
        solver_version_tag="openradioss-test",
        stages=[
            StageResult(
                stage="engine",
                command=["engine", "-i", "case_0001.rad"],
                returncode=0,
                duration_s=12.5,
                log_path=tmp_path / "case.engine.log",
                log=parse_log(ENGINE_LOG),
            )
        ],
    )
    path = write_run_summary(result)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_name"] == "case"
    assert payload["solver_version_tag"] == "openradioss-test"
    assert payload["ok"] is True
    assert payload["stages"][0]["log"]["cycles_parsed"] == 4
    assert payload["unit_system"] == "mm-s-tonne-N-MPa"


def test_read_engine_end_time(tmp_path: Path) -> None:
    deck = tmp_path / "case_0001.rad"
    deck.write_text("#RADIOSS ENGINE\n/VERS/2022\n/RUN/case/1\n        0.0157894737\n/END\n")
    assert read_engine_end_time(deck) == pytest.approx(0.0157894737)
    assert read_engine_end_time(tmp_path / "missing.rad") is None


def test_last_cycle_line_parses_the_engine_listing() -> None:
    text = (
        "   CYCLE    TIME      TIME-STEP  ELEMENT          ERROR  I-ENERGY\n"
        "  110900  0.1448E-01  0.1405E-06 NODE        2276  -5.1%   6435.\n"
        "  111000  0.1450E-01  0.1405E-06 NODE        2276  -5.1%   6440.\n"
    )
    assert _last_cycle_line(text) == (111000, pytest.approx(0.0145), "-5.1%")


def test_last_cycle_line_ignores_non_cycle_text() -> None:
    assert _last_cycle_line("RESTART FILES WRITTEN\n** COMPUTE COMPLETE **\n") is None


def test_run_result_is_not_ok_when_a_stage_errored() -> None:
    result = RunResult(
        run_name="case",
        run_dir=Path("."),
        stages=[
            StageResult(
                stage="engine",
                command=[],
                returncode=1,
                duration_s=1.0,
                log_path=None,
                log=parse_log(FAILING_LOG),
            )
        ],
    )
    assert result.ok is False
    assert result.engine_log is not None
    assert result.engine_log.negative_volume is True
