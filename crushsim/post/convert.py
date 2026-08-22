"""FR-06 - result conversion through the OFFICIAL OpenRadioss converters.

Spec §4 FR-06 and §13.4: **writing a binary parser for TH or ANIM files is
forbidden.** This module only locates and invokes the official ``th_to_csv``
and ``anim_to_vtk`` executables shipped with the pinned OpenRadioss release,
and reports clearly when they are absent.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import SolverPaths, load_solver_config
from ..errors import PostProcessError
from ..solver.runner import _solver_env

_TH_CANDIDATES: tuple[str, ...] = (
    "th_to_csv",
    "th_to_csv.exe",
    "th_to_csv_linux64_gf",
    "th_to_csv_win64.exe",
)
_ANIM_CANDIDATES: tuple[str, ...] = (
    "anim_to_vtk",
    "anim_to_vtk.exe",
    "anim_to_vtk_linux64_gf",
    "anim_to_vtk_win64.exe",
)


@dataclass(slots=True)
class ConversionResult:
    """Files produced by one conversion step."""

    kind: str
    inputs: list[Path]
    outputs: list[Path] = field(default_factory=list)
    converter: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "kind": self.kind,
            "converter": str(self.converter) if self.converter else None,
            "inputs": [str(p) for p in self.inputs],
            "outputs": [str(p) for p in self.outputs],
        }


def resolve_converter(paths: SolverPaths, kind: str) -> Path:
    """Locate an official converter executable.

    Args:
        paths: Loaded solver configuration.
        kind: ``"th_to_csv"`` or ``"anim_to_vtk"``.

    Raises:
        PostProcessError: If the converter cannot be found. Writing a
            replacement parser is not an option (spec §13.4).
    """
    if kind == "th_to_csv":
        configured, candidates = paths.th_to_csv, _TH_CANDIDATES
    elif kind == "anim_to_vtk":
        configured, candidates = paths.anim_to_vtk, _ANIM_CANDIDATES
    else:
        raise PostProcessError(f"Unknown converter {kind!r}")

    # Always return absolute paths: the converters run with cwd set to the
    # output directory, where repo-relative paths do not resolve.
    if configured is not None:
        p = Path(configured)
        if p.is_file():
            return p.resolve()
        if p.name and (found := shutil.which(p.name)):
            return Path(found).resolve()
    if paths.install_root is not None:
        for name in candidates:
            for sub in ("", "exec", "bin", "tools"):
                candidate = Path(paths.install_root) / sub / name
                if candidate.is_file():
                    return candidate.resolve()
    for name in candidates:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    source = paths.source or "configs/solver.yaml"
    raise PostProcessError(
        f"Official converter {kind!r} not found.\n"
        f"  configured: {configured if configured else '<empty>'}\n"
        f"  searched install_root: {paths.install_root if paths.install_root else '<empty>'}\n"
        f"  searched PATH for: {', '.join(candidates)}\n"
        f"Fix: set executables.{kind} in {source} to the converter shipped with "
        f"the OpenRadioss release pinned as '{paths.version_tag}'. "
        "Writing an in-house binary parser is forbidden (spec §13.4)."
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdout_path: Path | None = None,
) -> str:
    """Run a converter, returning its stdout.

    Raises:
        PostProcessError: If the converter exits non-zero or cannot be started.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=False,
            check=False,
        )
    except OSError as exc:
        raise PostProcessError(f"Could not run converter {command[0]!r}: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise PostProcessError(
            f"Converter failed (exit {completed.returncode}): {' '.join(command)}\n{stderr[-2000:]}"
        )
    if stdout_path is not None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_bytes(completed.stdout)
    return completed.stdout.decode("utf-8", errors="replace")


def convert_time_history(
    th_file: str | Path,
    *,
    outdir: str | Path | None = None,
    solver_config: str | Path = "configs/solver.yaml",
) -> ConversionResult:
    """Convert a ``*T01`` time-history file to CSV with the official tool.

    Args:
        th_file: The solver's time-history file (``<run>T01``).
        outdir: Directory receiving the CSV; defaults to the file's directory.
        solver_config: Pinned solver configuration.

    Raises:
        PostProcessError: If the input or the converter is missing, or the
            converter produced no CSV (spec §13.5 - never a silent empty result).
    """
    src = Path(th_file)
    if not src.is_file():
        raise PostProcessError(f"Time-history file not found: {src}")
    paths = load_solver_config(solver_config)
    converter = resolve_converter(paths, "th_to_csv")
    work = Path(outdir) if outdir is not None else src.parent
    work.mkdir(parents=True, exist_ok=True)

    before = set(work.glob("*.csv"))
    # The converters need the same loader path as the solver (extlib DLLs).
    _run([str(converter), str(src.resolve())], cwd=work, env=_solver_env(paths, 1))
    produced = sorted(set(work.glob("*.csv")) - before)
    if not produced:
        # Some builds write next to the input instead of the working directory.
        produced = sorted(src.parent.glob(f"{src.name}*.csv"))
    if not produced:
        raise PostProcessError(
            f"th_to_csv produced no CSV for {src}. Check the converter output and "
            f"that the run actually wrote time-history data."
        )
    return ConversionResult(kind="th_to_csv", inputs=[src], outputs=produced, converter=converter)


def convert_animation(
    anim_files: list[str | Path] | str | Path,
    *,
    outdir: str | Path,
    solver_config: str | Path = "configs/solver.yaml",
) -> ConversionResult:
    """Convert ``*A###`` animation files to a ``.vtk`` sequence with the official tool.

    Args:
        anim_files: One animation file, or the list of them in time order.
        outdir: Directory receiving the ``.vtk`` sequence.
        solver_config: Pinned solver configuration.

    Raises:
        PostProcessError: If an input or the converter is missing, or a
            conversion produced an empty file.
    """
    if isinstance(anim_files, (str, Path)):
        inputs = [Path(anim_files)]
    else:
        inputs = [Path(f) for f in anim_files]
    if not inputs:
        raise PostProcessError("No animation files given to convert")
    missing = [str(p) for p in inputs if not p.is_file()]
    if missing:
        raise PostProcessError(f"Animation file(s) not found: {', '.join(missing)}")

    paths = load_solver_config(solver_config)
    converter = resolve_converter(paths, "anim_to_vtk")
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    env = _solver_env(paths, 1)
    outputs: list[Path] = []
    for index, src in enumerate(sorted(inputs)):
        target = out / f"{src.name}.vtk"
        # anim_to_vtk writes the VTK legacy file to stdout.
        _run([str(converter), str(src.resolve())], cwd=out, env=env, stdout_path=target)
        if not target.is_file() or target.stat().st_size == 0:
            raise PostProcessError(
                f"anim_to_vtk produced an empty file for {src} (frame {index}). "
                "Check the converter and the animation output settings."
            )
        outputs.append(target)
    return ConversionResult(
        kind="anim_to_vtk", inputs=inputs, outputs=outputs, converter=converter
    )


def find_result_files(run_dir: str | Path, run_name: str) -> dict[str, list[Path]]:
    """Locate the solver's raw TH and ANIM outputs in a run directory.

    Returns:
        ``{"time_history": [...], "animation": [...]}`` in sorted order. Empty
        lists mean the solver has not produced results yet.
    """
    d = Path(run_dir)
    th = sorted(p for p in d.glob(f"{run_name}T*") if p.is_file())
    anim = sorted(p for p in d.glob(f"{run_name}A*") if p.is_file() and p.suffix != ".vtk")
    return {"time_history": th, "animation": anim}
