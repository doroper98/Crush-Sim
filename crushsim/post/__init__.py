"""FR-06 / FR-07 - post-processing: official converters, curves, rendering."""

from __future__ import annotations

from .convert import (
    ConversionResult,
    convert_animation,
    convert_time_history,
    find_result_files,
    resolve_converter,
)
from .curves import (
    CurveMetrics,
    EnergyBalance,
    compute_metrics,
    extract_energy_balance,
    extract_force_displacement,
    plot_force_displacement,
    read_th_csv,
)
from .render import (
    CAMERA_PRESETS,
    RenderResult,
    ffmpeg_available,
    render_sequence,
    rendering_available,
)

__all__ = [
    "CAMERA_PRESETS",
    "ConversionResult",
    "CurveMetrics",
    "EnergyBalance",
    "RenderResult",
    "compute_metrics",
    "convert_animation",
    "convert_time_history",
    "extract_energy_balance",
    "extract_force_displacement",
    "ffmpeg_available",
    "find_result_files",
    "plot_force_displacement",
    "read_th_csv",
    "render_sequence",
    "rendering_available",
    "resolve_converter",
]
