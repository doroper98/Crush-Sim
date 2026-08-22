"""FR-07 - offscreen animation rendering (PyVista + imageio/ffmpeg).

Three fixed camera presets are rendered per run (iso / front / section) and the
colormap range is locked across the whole sequence so colours mean the same
thing in every frame (spec §4 FR-07: "no automatic rescaling").

Rendering is strictly offscreen. On a headless machine PyVista needs an
software OpenGL implementation (OSMesa) or an X server such as ``xvfb-run``;
:func:`rendering_available` probes for it so the CLI and the tests can skip
rendering with a precise reason instead of crashing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..errors import PostProcessError

CAMERA_PRESETS: dict[str, dict[str, Any]] = {
    "iso": {"position": (1.0, -1.0, 0.7), "up": (0.0, 0.0, 1.0), "section": False},
    "front": {"position": (1.0, 0.0, 0.0), "up": (0.0, 0.0, 1.0), "section": False},
    "section": {"position": (0.0, -1.0, 0.15), "up": (0.0, 0.0, 1.0), "section": True},
}
"""The three fixed camera presets. ``position`` is a direction from the centre."""

DEFAULT_SCALAR_CANDIDATES: tuple[str, ...] = (
    "von Mises",
    "VonMises",
    "vonmises",
    "Stress",
    "EPSP",
    "Plastic Strain",
    "Displacement",
)
"""Preferred scalar fields, in order, for the colour map."""


@dataclass(slots=True)
class RenderResult:
    """Files produced by a rendering pass."""

    videos: dict[str, Path] = field(default_factory=dict)
    gifs: dict[str, Path] = field(default_factory=dict)
    stills: dict[str, Path] = field(default_factory=dict)
    scalar: str | None = None
    scalar_range: tuple[float, float] | None = None
    frames: int = 0
    skipped_reason: str | None = None

    @property
    def rendered(self) -> bool:
        """Whether anything was actually rendered."""
        return bool(self.videos or self.gifs or self.stills)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for run summaries and reports."""
        return {
            "videos": {k: str(v) for k, v in self.videos.items()},
            "gifs": {k: str(v) for k, v in self.gifs.items()},
            "stills": {k: str(v) for k, v in self.stills.items()},
            "scalar": self.scalar,
            "scalar_range": list(self.scalar_range) if self.scalar_range else None,
            "frames": self.frames,
            "skipped_reason": self.skipped_reason,
        }


def _force_offscreen() -> None:
    """Set the environment PyVista needs before it creates a render window."""
    os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")
    os.environ.setdefault("PYVISTA_USE_IPYVTK", "false")


_RENDER_PROBE_SCRIPT = """
import os
os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")
os.environ.setdefault("PYVISTA_USE_IPYVTK", "false")
import pyvista as pv
pv.OFF_SCREEN = True
plotter = pv.Plotter(off_screen=True, window_size=(64, 64))
plotter.add_mesh(pv.Sphere())
image = plotter.screenshot(return_img=True)
plotter.close()
assert image is not None and getattr(image, "size", 0) > 0, "empty image"
print("CRUSHSIM_RENDER_OK")
"""
"""Probe executed in a child interpreter by :func:`rendering_available`."""


def rendering_available() -> tuple[bool, str]:
    """Probe whether an offscreen render actually works here.

    The probe runs in a subprocess: on machines without a usable OpenGL stack
    VTK can die with a native crash (e.g. an access violation on Windows),
    which no in-process ``except`` can contain. Isolating it keeps ``csim
    doctor`` and the test suite alive and turns the crash into a plain
    ``(False, reason)``.

    Returns:
        ``(True, "...")`` when a test render succeeded, otherwise
        ``(False, reason)`` with the reason to show the operator.
    """
    _force_offscreen()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
            [sys.executable, "-c", _RENDER_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "offscreen render probe timed out after 180 s"
    except OSError as exc:
        return False, f"could not start the render probe: {exc}"
    if proc.returncode == 0 and "CRUSHSIM_RENDER_OK" in proc.stdout:
        return True, "offscreen rendering works"
    detail_lines = (proc.stderr or proc.stdout).strip().splitlines()
    detail = detail_lines[-1] if detail_lines else f"exit code {proc.returncode}"
    return False, (
        f"offscreen render failed: {detail}. Install a software OpenGL stack "
        "(Linux: libosmesa6, or run under xvfb-run; Windows: OSMesa from "
        "mesa-dist-win on PATH)."
    )


def ffmpeg_available() -> tuple[bool, str]:
    """Whether an ffmpeg binary usable by imageio is present."""
    try:
        import imageio_ffmpeg  # noqa: PLC0415
    except ImportError as exc:
        return False, f"imageio-ffmpeg is not installed: {exc}"
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - imageio raises a bare RuntimeError
        return False, f"ffmpeg binary unavailable: {exc}"
    return True, str(exe)


def _pick_scalar(mesh: Any, preferred: str | None) -> str | None:
    """Choose the scalar array to colour by."""
    names = list(mesh.point_data.keys()) + list(mesh.cell_data.keys())
    if preferred and preferred in names:
        return preferred
    for candidate in DEFAULT_SCALAR_CANDIDATES:
        for name in names:
            if candidate.lower() in str(name).lower():
                return str(name)
    return str(names[0]) if names else None


def _global_range(meshes: Sequence[Any], scalar: str) -> tuple[float, float]:
    """Compute the scalar range over the whole sequence (frame-fixed colormap)."""
    lo = float("inf")
    hi = float("-inf")
    for mesh in meshes:
        if scalar in mesh.point_data:
            values = mesh.point_data[scalar]
        elif scalar in mesh.cell_data:
            values = mesh.cell_data[scalar]
        else:
            continue
        if values.ndim > 1:
            import numpy as np  # noqa: PLC0415

            values = np.linalg.norm(values, axis=1)
        lo = min(lo, float(values.min()))
        hi = max(hi, float(values.max()))
    if lo > hi:
        return (0.0, 1.0)
    if lo == hi:
        return (lo, lo + 1.0)
    return (lo, hi)


def render_sequence(
    vtk_files: Sequence[str | Path],
    outdir: str | Path,
    *,
    scalar: str | None = None,
    presets: Sequence[str] = ("iso", "front", "section"),
    fps: int = 12,
    window_size: tuple[int, int] = (960, 720),
    cmap: str = "turbo",
    gif_max_frames: int = 40,
    strict: bool = True,
) -> RenderResult:
    """Render a converted VTK sequence to MP4 (plus a summary GIF) per preset.

    Args:
        vtk_files: The ``.vtk``/``.vtu`` sequence in time order.
        outdir: Directory receiving the videos and stills.
        scalar: Scalar field to colour by; auto-detected when omitted.
        presets: Which camera presets from :data:`CAMERA_PRESETS` to render.
        fps: Frames per second of the MP4.
        window_size: Render window size in pixels.
        cmap: Matplotlib colormap name.
        gif_max_frames: Frame cap for the shareable GIF.
        strict: Raise when rendering is unavailable instead of returning a
            result flagged as skipped.

    Raises:
        PostProcessError: If inputs are missing, or rendering is unavailable
            and ``strict`` is set.
    """
    files = [Path(f) for f in vtk_files]
    if not files:
        raise PostProcessError("No VTK files given to render")
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise PostProcessError(f"VTK file(s) not found: {', '.join(missing)}")
    unknown = [p for p in presets if p not in CAMERA_PRESETS]
    if unknown:
        raise PostProcessError(f"Unknown camera preset(s): {unknown}")

    ok, reason = rendering_available()
    if not ok:
        if strict:
            raise PostProcessError(f"Offscreen rendering is unavailable: {reason}")
        return RenderResult(skipped_reason=reason)

    _force_offscreen()
    import imageio.v2 as imageio  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import pyvista as pv  # noqa: PLC0415

    pv.OFF_SCREEN = True
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    meshes = [pv.read(str(f)) for f in files]
    field_name = _pick_scalar(meshes[0], scalar)
    clim = _global_range(meshes, field_name) if field_name else None

    result = RenderResult(scalar=field_name, scalar_range=clim, frames=len(meshes))
    video_ok, video_reason = ffmpeg_available()

    bounds = np.array(meshes[0].bounds, dtype=float)
    centre = np.array(
        [
            0.5 * (bounds[0] + bounds[1]),
            0.5 * (bounds[2] + bounds[3]),
            0.5 * (bounds[4] + bounds[5]),
        ]
    )
    diagonal = float(np.linalg.norm(bounds[1::2] - bounds[0::2])) or 1.0

    for preset in presets:
        spec = CAMERA_PRESETS[preset]
        direction = np.array(spec["position"], dtype=float)
        direction /= float(np.linalg.norm(direction))
        camera = (
            tuple(centre + direction * diagonal * 1.8),
            tuple(centre),
            tuple(spec["up"]),
        )
        frames: list[Any] = []
        for mesh in meshes:
            geometry = mesh
            if spec["section"]:
                try:
                    geometry = mesh.clip(normal="y", origin=tuple(centre), invert=True)
                except Exception:  # noqa: BLE001 - a clip failure must not kill the run
                    geometry = mesh
            plotter = pv.Plotter(off_screen=True, window_size=window_size)
            plotter.set_background("white")
            plotter.add_mesh(
                geometry,
                scalars=field_name,
                cmap=cmap,
                clim=clim,
                show_edges=False,
                scalar_bar_args={"title": field_name or "", "vertical": True},
                show_scalar_bar=field_name is not None,
            )
            plotter.camera_position = camera
            plotter.show(auto_close=False)
            frames.append(plotter.screenshot(return_img=True))
            plotter.close()

        still = out / f"{preset}_last.png"
        imageio.imwrite(still, frames[-1])
        result.stills[preset] = still

        step = max(1, len(frames) // max(1, gif_max_frames))
        gif = out / f"{preset}.gif"
        imageio.mimsave(gif, frames[::step], duration=1.0 / max(1, fps), loop=0)
        result.gifs[preset] = gif

        if video_ok:
            mp4 = out / f"{preset}.mp4"
            try:
                imageio.mimwrite(mp4, frames, fps=fps, codec="libx264", quality=7)
                result.videos[preset] = mp4
            except Exception as exc:  # noqa: BLE001 - keep the GIF even if MP4 fails
                result.skipped_reason = f"MP4 encoding failed: {exc}"
        else:
            result.skipped_reason = f"MP4 skipped: {video_reason}"

    return result
