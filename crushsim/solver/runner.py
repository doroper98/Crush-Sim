"""FR-05 - OpenRadioss execution wrapper.

Runs the pinned starter and then the engine as subprocesses, tails their logs
into structured data (:mod:`crushsim.solver.logparse`), and writes
``run_summary.json``.

No solver is bundled and none is emulated (ADR-01). When an executable is
missing the wrapper raises :class:`~crushsim.errors.SolverError` naming the
configuration file, the key and the expected path - it never falls back to an
approximate answer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import SolverPaths, load_solver_config
from ..errors import SolverError
from ..units import UNIT_SYSTEM
from .logparse import LogSummary, parse_log, tail

_STARTER_CANDIDATES: tuple[str, ...] = (
    "starter_linux64_gf",
    "starter_win64.exe",
    "starter_linux64_gf_ompi",
)
_ENGINE_CANDIDATES: tuple[str, ...] = (
    "engine_linux64_gf",
    "engine_win64.exe",
    "engine_linux64_gf_ompi",
)


@dataclass(slots=True)
class StageResult:
    """Outcome of one solver stage (starter or engine)."""

    stage: str
    command: list[str]
    returncode: int
    duration_s: float
    log_path: Path | None
    log: LogSummary

    @property
    def ok(self) -> bool:
        """Whether the stage exited cleanly and reported no error."""
        return self.returncode == 0 and not self.log.errors

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for ``run_summary.json``."""
        return {
            "stage": self.stage,
            "command": list(self.command),
            "returncode": self.returncode,
            "duration_s": self.duration_s,
            "log_path": str(self.log_path) if self.log_path else None,
            "log": self.log.to_dict(),
            "ok": self.ok,
        }


@dataclass(slots=True)
class RunResult:
    """Outcome of a full starter + engine run."""

    run_name: str
    run_dir: Path
    stages: list[StageResult] = field(default_factory=list)
    summary_path: Path | None = None
    solver_version_tag: str = "unpinned"
    config_copy: Path | None = None
    git_commit: str | None = None

    @property
    def ok(self) -> bool:
        """Whether every stage succeeded."""
        return bool(self.stages) and all(s.ok for s in self.stages)

    @property
    def engine_log(self) -> LogSummary | None:
        """The engine stage log summary, if the engine ran."""
        for stage in self.stages:
            if stage.stage == "engine":
                return stage.log
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for ``run_summary.json`` (spec §4 FR-05)."""
        return {
            "run_name": self.run_name,
            "run_dir": str(self.run_dir),
            "unit_system": UNIT_SYSTEM,
            "solver_version_tag": self.solver_version_tag,
            "git_commit": self.git_commit,
            "config_copy": str(self.config_copy) if self.config_copy else None,
            "ok": self.ok,
            "stages": [s.to_dict() for s in self.stages],
        }


def _git_commit() -> str | None:
    """Best-effort short commit hash of the working tree (never raises)."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(
            [git, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    value = out.stdout.strip()
    return value or None


def resolve_executable(paths: SolverPaths, kind: str) -> Path:
    """Resolve the starter or engine executable.

    Resolution order: the explicit path in ``configs/solver.yaml``, then the
    conventional file names under ``install_root``, then ``PATH``.

    Args:
        paths: Loaded solver configuration.
        kind: ``"starter"`` or ``"engine"``.

    Raises:
        SolverError: If the executable cannot be found, with the exact keys to
            fill in.
    """
    if kind not in ("starter", "engine"):
        raise SolverError(f"Unknown solver stage {kind!r}")
    configured = paths.starter if kind == "starter" else paths.engine
    candidates = _STARTER_CANDIDATES if kind == "starter" else _ENGINE_CANDIDATES

    # Always return an absolute path: the solver subprocess runs with
    # cwd=run_dir, so a path relative to the repo root would not resolve there.
    if configured is not None:
        p = Path(configured)
        if p.is_file():
            return p.resolve()
        if p.name and (found := shutil.which(p.name)):
            return Path(found).resolve()

    if paths.install_root is not None:
        for name in candidates:
            for sub in ("", "exec", "bin"):
                candidate = Path(paths.install_root) / sub / name
                if candidate.is_file():
                    return candidate.resolve()

    for name in candidates:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    source = paths.source or "configs/solver.yaml"
    raise SolverError(
        f"OpenRadioss {kind} executable not found.\n"
        f"  configured: {configured if configured else '<empty>'}\n"
        f"  searched install_root: {paths.install_root if paths.install_root else '<empty>'}\n"
        f"  searched PATH for: {', '.join(candidates)}\n"
        f"Fix: install the OpenRadioss release pinned as version_tag "
        f"'{paths.version_tag}' and set executables.{kind} in {source}."
    )


def _solver_env(paths: SolverPaths, threads: int) -> dict[str, str]:
    """Build the subprocess environment (OpenMP threads plus configured vars).

    When ``install_root`` is set this reproduces what the official OpenRadioss
    run scripts export: ``RAD_CFG_PATH`` (starter configuration files),
    ``RAD_H3D_PATH`` (H3D writer library), and the ``extlib`` runtime-library
    directories on the loader path - PATH on Windows, LD_LIBRARY_PATH
    elsewhere. Without the loader-path entries the executables die at startup
    with STATUS_DLL_NOT_FOUND (exit 3221225781 / 0xC0000135) on Windows.
    """
    env = dict(os.environ)
    env.update(paths.env)
    # Cap at the machine's core count: oversubscribing the GNU OpenMP build
    # has no upside and has been observed to trigger sporadic engine
    # segfaults mid-run under contention.
    cpu_cap = os.cpu_count() or 1
    env["OMP_NUM_THREADS"] = str(max(1, min(threads, cpu_cap)))
    # Thread-stack size for both OpenMP runtimes (Intel and GNU builds).
    env.setdefault("KMP_STACKSIZE", "400m")
    env.setdefault("OMP_STACKSIZE", "400m")
    if paths.install_root is not None:
        # install_root is often given relative to the repo root, but the solver
        # subprocess runs with cwd=run_dir - every path handed to the loader
        # must therefore be absolute or the DLLs are silently not found.
        root = Path(paths.install_root).expanduser().resolve()
        env.setdefault("RAD_CFG_PATH", str(root / "hm_cfg_files"))
        arch_dirs = [
            root / "extlib" / "hm_reader" / "win64",
            root / "extlib" / "hm_reader" / "linux64",
            root / "extlib" / "h3d" / "lib" / "win64",
            root / "extlib" / "h3d" / "lib" / "linux64",
            # Intel oneAPI runtime (libiomp5md, libmmd, mkl_*, svml_dispmd)
            # bundled with the official release - required at load time.
            root / "extlib" / "intelOneAPI_runtime" / "win64",
            root / "extlib" / "intelOneAPI_runtime" / "linux64",
            root / "exec",
            root / "bin",
        ]
        lib_dirs = [d for d in arch_dirs if d.is_dir()]
        for d in lib_dirs:
            if d.parent.name == "h3d" or d.parent.parent.name == "h3d":
                env.setdefault("RAD_H3D_PATH", str(d))
                break
        if lib_dirs:
            joined = os.pathsep.join(str(d) for d in lib_dirs)
            env["PATH"] = joined + os.pathsep + env.get("PATH", "")
            if os.name != "nt":
                current = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = joined + (os.pathsep + current if current else "")
    return env


_CHILD_STACK_LIMIT_BYTES: int = 64 * 1024 * 1024


def _sane_child_rlimits() -> None:  # pragma: no cover - runs in the child
    """Restore a bounded stack limit in the solver child process.

    The Gmsh library raises the parent's RLIMIT_STACK soft limit to
    ``unlimited`` when it initialises; the solver child inherits that across
    ``exec`` and the GNU OpenMP runtime then lays out thread stacks in a way
    that crashes the OpenRadioss engine with SIGSEGV before cycle 0. A plain
    8 MB soft limit is known-good; 64 MB adds headroom.
    """
    import resource  # noqa: PLC0415 - POSIX-only module

    soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
    if soft == resource.RLIM_INFINITY:
        limit = _CHILD_STACK_LIMIT_BYTES
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_STACK, (limit, hard))


_RUN_END_TIME_RE = re.compile(r"^/RUN/.*\n\s*([0-9.Ee+-]+)", re.MULTILINE)

_ENGINE_POLL_INTERVAL_S: float = 15.0
"""How often the progress monitor re-reads the engine's ``*.out`` listing."""


def read_engine_end_time(engine_deck: str | Path) -> float | None:
    """Read the termination time from an engine deck's ``/RUN`` block."""
    try:
        match = _RUN_END_TIME_RE.search(Path(engine_deck).read_text(errors="replace"))
        return float(match.group(1)) if match else None
    except (OSError, ValueError):
        return None


def _last_cycle_line(text: str) -> tuple[int, float, str | None] | None:
    """Parse the last engine cycle line into ``(cycle, time, energy_error)``.

    Engine cycle lines start with the cycle count and the simulated time; the
    energy-balance error is the first ``%``-suffixed token on the line.
    """
    for line in reversed(text.splitlines()):
        tokens = line.split()
        if len(tokens) < 3 or not tokens[0].isdigit():
            continue
        try:
            sim_time = float(tokens[1])
        except ValueError:
            continue
        error = next((tok for tok in tokens[2:] if tok.endswith("%")), None)
        return int(tokens[0]), sim_time, error
    return None


def _watch_engine_progress(
    stop: threading.Event,
    *,
    run_dir: Path,
    deck_stem: str,
    end_time: float | None,
    progress: Callable[[str], None],
) -> None:
    """Poll the engine's ``*.out`` listing and report cycle progress.

    The engine only writes to its listing file, never to stdout while
    running, so this file poll is the sole live progress source.
    """
    last_cycle = -1
    while not stop.wait(_ENGINE_POLL_INTERVAL_S):
        parsed = None
        for out_file in sorted(run_dir.glob(f"{deck_stem}*.out")):
            try:
                text = out_file.read_text(errors="replace")[-6000:]
            except OSError:
                continue
            parsed = _last_cycle_line(text) or parsed
        if parsed is None or parsed[0] == last_cycle:
            continue
        last_cycle, sim_time, error = parsed
        if end_time and end_time > 0.0:
            prefix = f"engine {min(100.0, 100.0 * sim_time / end_time):5.1f}%"
        else:
            prefix = "engine"
        message = f"  {prefix}  t={sim_time:.4e} s  cycle {last_cycle:,}"
        if error is not None:
            message += f"  energy error {error}"
        progress(message)


def _run_stage(
    *,
    stage: str,
    executable: Path,
    deck: Path,
    run_dir: Path,
    threads: int,
    env: dict[str, str],
    timeout_s: float | None,
    progress: Callable[[str], None] | None = None,
    end_time: float | None = None,
) -> StageResult:
    """Run one solver stage and capture its log."""
    command = [str(executable), "-i", deck.name]
    if stage == "engine":
        command += ["-nt", str(max(1, threads))]
    else:
        command += ["-np", "1"]

    log_path = run_dir / f"{deck.stem}.{stage}.log"
    started = time.monotonic()
    monitor_stop: threading.Event | None = None
    monitor: threading.Thread | None = None
    if progress is not None and stage == "engine":
        monitor_stop = threading.Event()
        monitor = threading.Thread(
            target=_watch_engine_progress,
            args=(monitor_stop,),
            kwargs={
                "run_dir": run_dir,
                "deck_stem": deck.stem,
                "end_time": end_time,
                "progress": progress,
            },
            daemon=True,
        )
        monitor.start()
    try:
        completed = subprocess.run(
            command,
            cwd=str(run_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            preexec_fn=_sane_child_rlimits if os.name != "nt" else None,
        )
    except FileNotFoundError as exc:
        raise SolverError(f"Could not execute the {stage}: {executable} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise SolverError(
            f"The {stage} exceeded its timeout of {timeout_s} s. "
            "Raise solver.timeout_s or reduce the model size."
        ) from exc
    finally:
        if monitor_stop is not None:
            monitor_stop.set()
        if monitor is not None:
            monitor.join(timeout=1.0)
    duration = time.monotonic() - started

    text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    # OpenRadioss also writes its own listing files; fold them in when present.
    for extra in sorted(run_dir.glob(f"{deck.stem}*.out")):
        text += "\n" + extra.read_text(encoding="utf-8", errors="replace")
    log_path.write_text(text, encoding="utf-8")

    return StageResult(
        stage=stage,
        command=command,
        returncode=completed.returncode,
        duration_s=duration,
        log_path=log_path,
        log=parse_log(text),
    )


def run_solver(
    starter_deck: str | Path,
    engine_deck: str | Path,
    *,
    solver_config: str | Path = "configs/solver.yaml",
    threads: int = 4,
    timeout_s: float | None = None,
    stop_on_energy_error: bool = True,
    stop_on_negative_volume: bool = True,
    run_name: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> RunResult:
    """Run the starter then the engine, and write ``run_summary.json``.

    Args:
        starter_deck: Path to ``*_0000.rad``.
        engine_deck: Path to ``*_0001.rad``.
        solver_config: Path to the pinned solver configuration.
        threads: OpenMP threads for the engine.
        timeout_s: Per-stage timeout in seconds.
        stop_on_energy_error: Abort if the engine reports an energy error.
        stop_on_negative_volume: Abort if a negative volume is reported.
        run_name: Name recorded in the summary; defaults to the deck stem.
        progress: Optional sink for human-readable progress lines; the engine
            stage reports live cycle progress through it.

    Returns:
        A :class:`RunResult`; ``run_summary.json`` is written next to the decks.

    Raises:
        SolverError: If an executable is missing, a stage fails, or a
            monitored condition trips.
    """
    starter_path = Path(starter_deck)
    engine_path = Path(engine_deck)
    for label, deck in (("starter", starter_path), ("engine", engine_path)):
        if not deck.is_file():
            raise SolverError(f"{label} deck not found: {deck}")
    run_dir = starter_path.parent
    paths = load_solver_config(solver_config)

    # See _solver_env: oversubscription destabilises the engine.
    threads = max(1, min(threads, os.cpu_count() or 1))

    starter_exe = resolve_executable(paths, "starter")
    engine_exe = resolve_executable(paths, "engine")
    env = _solver_env(paths, threads)

    name = run_name or starter_path.stem.removesuffix("_0000")
    result = RunResult(
        run_name=name,
        run_dir=run_dir,
        solver_version_tag=paths.version_tag,
        git_commit=_git_commit(),
        config_copy=Path(paths.source) if paths.source else None,
    )

    if progress is not None:
        progress(f"  starter: {starter_exe.name}")
    starter_stage = _run_stage(
        stage="starter",
        executable=starter_exe,
        deck=starter_path,
        run_dir=run_dir,
        threads=1,
        env=env,
        timeout_s=timeout_s,
    )
    result.stages.append(starter_stage)
    if not starter_stage.ok:
        write_run_summary(result)
        raise SolverError(
            f"OpenRadioss starter failed (exit {starter_stage.returncode}).\n"
            f"Log: {starter_stage.log_path}\n"
            + tail("\n".join(starter_stage.log.errors) or "", 20)
        )

    end_time = read_engine_end_time(engine_path)
    if progress is not None:
        span = f", end time {end_time:g} s" if end_time else ""
        progress(
            f"  starter ok ({starter_stage.duration_s:.0f} s); "
            f"engine: {engine_exe.name} ({threads} threads{span})"
        )
    engine_stage = _run_stage(
        stage="engine",
        executable=engine_exe,
        deck=engine_path,
        run_dir=run_dir,
        threads=threads,
        env=env,
        timeout_s=timeout_s,
        progress=progress,
        end_time=end_time,
    )
    result.stages.append(engine_stage)
    summary_path = write_run_summary(result)
    result.summary_path = summary_path

    if not engine_stage.ok:
        raise SolverError(
            f"OpenRadioss engine failed (exit {engine_stage.returncode}).\n"
            f"Log: {engine_stage.log_path}\n"
            + ("\n".join(engine_stage.log.errors[:10]) or "(no error line parsed)")
        )
    if stop_on_negative_volume and engine_stage.log.negative_volume:
        raise SolverError(
            f"Negative volume detected during the run. Log: {engine_stage.log_path}. "
            "Refine the mesh or reduce the timestep scale factor."
        )
    if stop_on_energy_error:
        from ..units import ENERGY_ERROR_MAX  # local import keeps the limit in one place

        worst = _time_history_energy_error(run_dir, name, solver_config)
        source = "energy balance"
        if worst is None:
            worst = engine_stage.log.max_energy_error
            source = "engine console error"
        elif progress is not None:
            console = engine_stage.log.max_energy_error
            if console is not None and console > worst:
                progress(
                    f"  energy balance {worst:.1%} incl. contact energy "
                    f"(console error {console:.1%} counts friction as loss)"
                )
        if worst is not None and worst > ENERGY_ERROR_MAX:
            raise SolverError(
                f"Energy error {worst:.1%} ({source}) exceeds the limit "
                f"{ENERGY_ERROR_MAX:.0%} (units.ENERGY_ERROR_MAX). "
                f"Log: {engine_stage.log_path}"
            )
    return result


def _time_history_energy_error(
    run_dir: Path, run_name: str, solver_config: str | Path
) -> float | None:
    """The §7 energy error from the run's time history, or None.

    The engine console's ERROR column omits interface (contact) energy, so a
    healthy frictional run prints -8% while the true balance residual is below
    0.2%; the time history is authoritative. Best-effort: any failure returns
    None and the caller falls back to the console value.
    """
    try:
        # Local imports: post-processing is not otherwise a runner dependency.
        from ..post.convert import convert_time_history  # noqa: PLC0415
        from ..post.curves import balance_energy_error, read_th_csv  # noqa: PLC0415

        th = run_dir / f"{run_name}T01"
        if not th.is_file():
            candidates = sorted(run_dir.glob("*T01"))
            if not candidates:
                return None
            th = candidates[0]
        conversion = convert_time_history(th, solver_config=solver_config)
        return balance_energy_error(read_th_csv(conversion.outputs[0]))
    except Exception:  # noqa: BLE001 - best-effort; console error is the fallback
        return None


def write_run_summary(result: RunResult, path: str | Path | None = None) -> Path:
    """Write ``run_summary.json`` for a run (spec §4 FR-05)."""
    target = Path(path) if path is not None else result.run_dir / "run_summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    result.summary_path = target
    return target


def solver_status(solver_config: str | Path = "configs/solver.yaml") -> dict[str, Any]:
    """Report solver availability without raising - used by ``csim doctor``."""
    try:
        paths = load_solver_config(solver_config)
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return {"configured": False, "reason": str(exc)}
    status: dict[str, Any] = {
        "configured": True,
        "version_tag": paths.version_tag,
        "install_root": str(paths.install_root) if paths.install_root else None,
    }
    for kind in ("starter", "engine"):
        try:
            status[kind] = str(resolve_executable(paths, kind))
        except SolverError as exc:
            status[kind] = None
            status[f"{kind}_reason"] = str(exc).splitlines()[0]
    return status
