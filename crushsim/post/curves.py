"""FR-06 - force/displacement curve analysis on the converted CSV (pandas).

Inputs are always CSV files produced by the official ``th_to_csv`` converter;
this module never reads a solver binary (spec §13.4).

Reported quantities (spec §4 FR-06):

* peak load [N] and the displacement at which it occurs [mm],
* absorbed energy [mJ] = integral of force over displacement,
* mean crush load [N] = absorbed energy / stroke,
* residual dent depth [mm] = permanent set after the tool unloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..errors import PostProcessError
from ..meshing.gates import GateResult, evaluate_solution_gate

_TIME_KEYS: tuple[str, ...] = ("time", "t", "tempo")
# The crushsim deck names its TH groups so the driven tool's columns are
# identifiable even though th_to_csv anonymises variables as "var NN": the
# first variable of REF_TOOL_MASTER (a /TH/NODE group) is DX and the first of
# REF_TOOL_RBODY (a /TH/RBODY group) is FX. Those specific keys come first;
# the generic ones keep foreign CSVs working.
_DISPLACEMENT_KEYS: tuple[str, ...] = (
    "ref_tool_master",
    "disp",
    "dx",
    "dy",
    "dz",
    "displacement",
    "stroke",
)
_FORCE_KEYS: tuple[str, ...] = (
    "toolcontact",
    "ref_tool_rbody",
    "force",
    "fx",
    "fy",
    "fz",
    "reaction",
    "rf",
)
_ENERGY_KEYS: dict[str, tuple[str, ...]] = {
    "internal": ("internal", "ie", "i-energy", "i_energy"),
    "kinetic": ("kinetic", "ke", "k-energy", "k_energy"),
    "hourglass": ("hourglass", "hg", "hourgl"),
    "contact": ("contact", "cont"),
    "external": ("external", "ext-work", "ext_work", "extwork"),
    "total": ("total",),
}


def _normalise(name: str) -> str:
    return "".join(ch for ch in str(name).strip().lower() if ch.isalnum() or ch in "._-")


def _match_column(columns: Iterable[str], keys: tuple[str, ...]) -> str | None:
    """Find the first column whose normalised name contains one of ``keys``."""
    normalised = {col: _normalise(col) for col in columns}
    for key in keys:
        for col, norm in normalised.items():
            if norm == key:
                return col
    for key in keys:
        for col, norm in normalised.items():
            if key in norm:
                return col
    return None


_VAR_SUFFIX = re.compile(r"^(?P<base>.*\bvar\s+)(?P<num>\d+)\s*$")


def _dominant_component(frame: pd.DataFrame, column: str) -> str:
    """Pick the drive-axis component of an anonymised ``var NN`` TH group.

    ``th_to_csv`` names a group's channels identically except for a trailing
    variable number, and the first three are the X/Y/Z components (DX/DY/DZ
    for a node group, FX/FY/FZ for a kinematic one). Name matching alone
    always lands on X, which is wrong for a Z-driven case, so among those
    three siblings the one with the largest data range - the loading axis -
    wins. Columns without the ``var NN`` suffix are returned unchanged.
    """
    match = _VAR_SUFFIX.match(column)
    if match is None:
        return column
    base = match.group("base")
    siblings: list[tuple[int, str]] = []
    for col in frame.columns:
        m = _VAR_SUFFIX.match(str(col))
        if m is not None and m.group("base") == base:
            siblings.append((int(m.group("num")), str(col)))
    siblings.sort()
    components = siblings[:3]
    if len(components) < 2:
        return column

    def span(col: str) -> float:
        series = pd.to_numeric(frame[col], errors="coerce").dropna()
        return float(series.max() - series.min()) if not series.empty else 0.0

    return max(components, key=lambda item: span(item[1]))[1]


@dataclass(slots=True)
class CurveMetrics:
    """Scalar results extracted from a force-displacement curve."""

    peak_load: float
    """Maximum |reaction force| [N]."""
    peak_displacement: float
    """Displacement at the peak load [mm]."""
    max_displacement: float
    """Maximum imposed displacement [mm]."""
    absorbed_energy: float
    """Integral of force over displacement [mJ]."""
    mean_crush_load: float
    """Absorbed energy divided by the stroke [N]."""
    residual_dent_depth: float
    """Permanent set once the reaction force returns to zero [mm]."""
    unloaded: bool
    """Whether the curve actually contains an unloading branch."""
    points: int

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "peak_load_N": self.peak_load,
            "peak_displacement_mm": self.peak_displacement,
            "max_displacement_mm": self.max_displacement,
            "absorbed_energy_mJ": self.absorbed_energy,
            "mean_crush_load_N": self.mean_crush_load,
            "residual_dent_depth_mm": self.residual_dent_depth,
            "unloaded": self.unloaded,
            "points": self.points,
        }


@dataclass(slots=True)
class EnergyBalance:
    """Energy balance of a run, with the §7 solution gate applied."""

    internal: float
    kinetic: float
    hourglass: float
    contact: float
    external: float
    added_mass_ratio: float
    energy_error: float
    gate: GateResult

    @property
    def hourglass_ratio(self) -> float:
        """Hourglass energy / internal energy."""
        return self.hourglass / self.internal if self.internal > 0.0 else 0.0

    @property
    def kinetic_ratio(self) -> float:
        """Kinetic energy / internal energy."""
        return self.kinetic / self.internal if self.internal > 0.0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "internal_energy_mJ": self.internal,
            "kinetic_energy_mJ": self.kinetic,
            "hourglass_energy_mJ": self.hourglass,
            "contact_energy_mJ": self.contact,
            "external_work_mJ": self.external,
            "hourglass_over_internal": self.hourglass_ratio,
            "kinetic_over_internal": self.kinetic_ratio,
            "added_mass_ratio": self.added_mass_ratio,
            "energy_error": self.energy_error,
            "gate": self.gate.to_dict(),
        }


def read_th_csv(path: str | Path) -> pd.DataFrame:
    """Read a ``th_to_csv`` output file into a DataFrame.

    Raises:
        PostProcessError: If the file is missing or contains no rows.
    """
    p = Path(path)
    if not p.is_file():
        raise PostProcessError(f"Converted time-history CSV not found: {p}")
    try:
        frame = pd.read_csv(p)
    except Exception as exc:  # noqa: BLE001 - pandas raises many parser errors
        raise PostProcessError(f"Could not read time-history CSV {p}: {exc}") from exc
    frame.columns = [str(c).strip() for c in frame.columns]
    if frame.empty:
        raise PostProcessError(f"Time-history CSV {p} has no rows")
    return frame


def extract_force_displacement(
    frame: pd.DataFrame,
    *,
    force_column: str | None = None,
    displacement_column: str | None = None,
    time_column: str | None = None,
) -> pd.DataFrame:
    """Reduce a time-history frame to ``time`` / ``displacement`` / ``force``.

    Column names differ between solver versions, so columns are matched by name
    fragment unless given explicitly. Displacement and force are returned as
    magnitudes (positive), which is what the crush curve is plotted against.

    Raises:
        PostProcessError: If a required column cannot be identified.
    """
    columns = list(frame.columns)
    time_col = time_column or _match_column(columns, _TIME_KEYS)
    disp_col = displacement_column or _match_column(columns, _DISPLACEMENT_KEYS)
    force_col = force_column or _match_column(columns, _FORCE_KEYS)
    if displacement_column is None and disp_col is not None:
        disp_col = _dominant_component(frame, disp_col)
    if force_column is None and force_col is not None:
        force_col = _dominant_component(frame, force_col)

    missing = [
        label
        for label, col in (("time", time_col), ("displacement", disp_col), ("force", force_col))
        if col is None
    ]
    if missing:
        raise PostProcessError(
            f"Could not identify column(s) {missing} in the time-history CSV. "
            f"Available columns: {columns}. "
            "Pass the column names explicitly (force_column/displacement_column)."
        )

    out = pd.DataFrame(
        {
            "time": pd.to_numeric(frame[time_col], errors="coerce"),
            "displacement": pd.to_numeric(frame[disp_col], errors="coerce").abs(),
            "force": pd.to_numeric(frame[force_col], errors="coerce"),
        }
    ).dropna()
    if out.empty:
        raise PostProcessError("No numeric rows remained after cleaning the time-history CSV")
    out = out.sort_values("time").reset_index(drop=True)

    # OpenRadioss /TH kinematic-entity "force" channels (RBODY, INTER, RWALL)
    # accumulate force x dt every cycle and are written UN-normalised, so the
    # stored series is the cumulative impulse integral(F dt) - confirmed in the
    # engine source (rgbodfp.F: FSAV += F*DT1; thkin.F writes FSAV raw) and
    # against ring theory (B-2). The physical force is its time derivative.
    force_norm = _normalise(force_col)
    is_kinematic_th = "var" in force_norm and any(
        key in force_norm for key in ("toolcontact", "rbody", "inter", "rwall")
    )
    if is_kinematic_th:
        t = out["time"].to_numpy()
        impulse = out["force"].to_numpy()
        force = np.gradient(impulse, t)
        # Smooth: the derivative amplifies elastic ringing and sampling noise.
        out["force"] = pd.Series(force).rolling(window=11, center=True, min_periods=1).mean()

    out["force"] = out["force"].abs()

    # Re-zero displacement at contact onset: the tool starts with a stand-off
    # gap, and dent depth / stiffness are measured from first contact.
    peak = float(out["force"].max())
    if peak > 0.0:
        engaged = out.index[out["force"] > 0.02 * peak]
        if len(engaged) > 0 and engaged[0] > 0:
            d0 = float(out.loc[engaged[0], "displacement"])
            out["displacement"] = (out["displacement"] - d0).clip(lower=0.0)
    return out


def compute_metrics(
    curve: pd.DataFrame,
    *,
    unload_threshold: float = 0.02,
) -> CurveMetrics:
    """Compute peak load, absorbed energy and residual dent depth.

    Args:
        curve: Frame with ``displacement`` [mm] and ``force`` [N] columns.
        unload_threshold: Fraction of the peak load below which the tool counts
            as unloaded when measuring the permanent set.

    Raises:
        PostProcessError: If required columns are missing or the curve is empty.
    """
    for column in ("displacement", "force"):
        if column not in curve.columns:
            raise PostProcessError(f"Curve is missing the {column!r} column")
    if curve.empty:
        raise PostProcessError("Curve has no points")

    disp = curve["displacement"].to_numpy(dtype=float)
    force = curve["force"].to_numpy(dtype=float)

    peak_index = int(np.argmax(force))
    peak_load = float(force[peak_index])
    peak_disp = float(disp[peak_index])
    max_disp = float(np.max(disp))

    # Energy is the work done along the actual path, so integrate in time order.
    absorbed = float(np.trapezoid(force, disp)) if disp.size > 1 else 0.0
    mean_load = absorbed / max_disp if max_disp > 0.0 else 0.0

    residual = max_disp
    unloaded = False
    if peak_load > 0.0:
        max_index = int(np.argmax(disp))
        after = np.arange(max_index, disp.size)
        low = after[force[after] <= unload_threshold * peak_load]
        if low.size:
            residual = float(disp[int(low[0])])
            unloaded = True

    return CurveMetrics(
        peak_load=peak_load,
        peak_displacement=peak_disp,
        max_displacement=max_disp,
        absorbed_energy=absorbed,
        mean_crush_load=mean_load,
        residual_dent_depth=residual,
        unloaded=unloaded,
        points=int(disp.size),
    )


def extract_energy_balance(
    frame: pd.DataFrame,
    *,
    added_mass_ratio: float = 0.0,
    energy_error: float | None = None,
) -> EnergyBalance:
    """Build the energy balance table and apply the §7 solution gate.

    Args:
        frame: Time-history frame containing energy columns.
        added_mass_ratio: Added mass / physical mass, read from the solver log.
        energy_error: Energy error fraction from the solver log; when omitted it
            is estimated from ``|external - (internal + kinetic)| / external``.

    Raises:
        PostProcessError: If no internal-energy column can be identified.
    """
    columns = list(frame.columns)
    values: dict[str, float] = {}
    for label, keys in _ENERGY_KEYS.items():
        col = _match_column(columns, keys)
        series = pd.to_numeric(frame[col], errors="coerce").dropna() if col else pd.Series(dtype=float)
        values[label] = float(series.iloc[-1]) if not series.empty else 0.0

    if values["internal"] <= 0.0:
        raise PostProcessError(
            f"No internal-energy column found in the time-history CSV. Columns: {columns}"
        )

    if energy_error is None:
        external = values["external"]
        if external > 0.0:
            energy_error = abs(external - (values["internal"] + values["kinetic"])) / external
        else:
            energy_error = 0.0

    # Quasi-static gate: judge the DEFORMABLE kinetic energy where the /TH
    # part group provides it ("CAN ... KE"). The global KE is dominated by the
    # driven rigid tool, whose steady translation says nothing about whether
    # the can response is quasi-static; the energy-error estimate above still
    # uses the global balance.
    deformable_ke = values["kinetic"]
    can_ke_col = _match_column(columns, ("canke",))
    if can_ke_col is not None:
        series = pd.to_numeric(frame[can_ke_col], errors="coerce").dropna()
        if not series.empty:
            deformable_ke = float(series.iloc[-1])

    gate = evaluate_solution_gate(
        energy_error=energy_error,
        hourglass_ratio=values["hourglass"] / values["internal"],
        kinetic_ratio=deformable_ke / values["internal"],
        added_mass_ratio=added_mass_ratio,
        info={"source": "time-history CSV"},
    )
    return EnergyBalance(
        internal=values["internal"],
        kinetic=values["kinetic"],
        hourglass=values["hourglass"],
        contact=values["contact"],
        external=values["external"],
        added_mass_ratio=added_mass_ratio,
        energy_error=float(energy_error),
        gate=gate,
    )


def plot_force_displacement(
    curve: pd.DataFrame,
    out_path: str | Path,
    *,
    metrics: CurveMetrics | None = None,
    title_text: str = "Force - displacement",
    unverified: bool = False,
) -> Path:
    """Render the force-displacement curve to PNG (headless Agg backend).

    Raises:
        PostProcessError: If matplotlib cannot write the file.
    """
    import matplotlib  # noqa: PLC0415 - imported lazily; sets a headless backend

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt  # noqa: PLC0415

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stats = metrics or compute_metrics(curve)

    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=140)
    ax.plot(curve["displacement"], curve["force"], linewidth=1.6, color="#1f4e79")
    ax.axvline(stats.peak_displacement, color="#c05621", linewidth=0.9, linestyle="--")
    ax.scatter([stats.peak_displacement], [stats.peak_load], s=28, color="#c05621", zorder=3)
    ax.annotate(
        f"peak {stats.peak_load:,.0f} N @ {stats.peak_displacement:.2f} mm",
        xy=(stats.peak_displacement, stats.peak_load),
        xytext=(6, -14),
        textcoords="offset points",
        fontsize=8,
        color="#c05621",
    )
    ax.set_xlabel("Displacement [mm]")
    ax.set_ylabel("Reaction force [N]")
    ax.set_title(title_text, fontsize=11)
    ax.grid(True, linewidth=0.4, alpha=0.4)
    ax.margins(x=0.01)
    if unverified:
        ax.text(
            0.5,
            0.5,
            "UNVERIFIED",
            transform=ax.transAxes,
            fontsize=42,
            color="#d00000",
            alpha=0.14,
            ha="center",
            va="center",
            rotation=24,
            fontweight="bold",
        )
    fig.tight_layout()
    try:
        fig.savefig(target)
    except Exception as exc:  # noqa: BLE001 - surface any backend failure
        raise PostProcessError(f"Could not write the curve PNG {target}: {exc}") from exc
    finally:
        plt.close(fig)
    return target
