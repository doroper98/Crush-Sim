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


CURVE_PANEL_WIDTH_PX: int = 480
"""Width of the synced force-displacement panel beside the animation."""

RIGID_TOOL_OPACITY: float = 0.3
"""Opacity of rigid tool parts so the crushing part stays visible through them."""

RIGID_TOOL_COLOR: str = "#8a929c"
"""Neutral steel-grey for rigid tools (they carry no stress field of interest)."""


def _split_rigid(mesh: Any, rigid_part_ids: Sequence[int] | None) -> tuple[Any, Any | None]:
    """Split a frame into (deformable, rigid-tools) by the ``PART_ID`` array.

    Returns the whole mesh and ``None`` when no split is possible - unknown
    part ids, no ``PART_ID`` array (older converters), or everything rigid.
    """
    if not rigid_part_ids:
        return mesh, None
    part_ids = mesh.cell_data.get("PART_ID") if hasattr(mesh, "cell_data") else None
    if part_ids is None:
        return mesh, None
    import numpy as np  # noqa: PLC0415

    mask = np.isin(np.asarray(part_ids), list(rigid_part_ids))
    if not mask.any() or mask.all():
        return mesh, None
    return mesh.extract_cells(~mask), mesh.extract_cells(mask)


def _curve_panel_frames(curve: Any, n_frames: int, height: int) -> list[Any]:
    """Render one force-displacement panel per animation frame.

    The full curve is drawn faintly, the already-crushed portion is traced
    solid with a marker at the frame's time, and the running time / deflection
    / force values are printed - so the video answers "how hard is it pushing
    right now" without a second window. Frame times assume the solver's
    uniform animation interval over the curve's time span.
    """
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    t = curve["time"].to_numpy(dtype=float)
    d = curve["displacement"].to_numpy(dtype=float)
    f = curve["force"].to_numpy(dtype=float)

    dpi = 100
    fig, ax = plt.subplots(figsize=(CURVE_PANEL_WIDTH_PX / dpi, height / dpi), dpi=dpi)
    ax.plot(d, f, color="#9aa7b8", linewidth=1.0)
    trace = ax.plot([], [], color="#c0392b", linewidth=2.0)[0]
    marker = ax.plot([], [], "o", color="#c0392b", markersize=7)[0]
    label = ax.text(
        0.03, 0.97, "", transform=ax.transAxes, va="top", ha="left", fontsize=9, family="monospace"
    )
    ax.set_xlabel("displacement [mm]")
    ax.set_ylabel("force [N]")
    ax.set_title("Force - displacement", fontsize=10)
    ax.margins(x=0.05, y=0.08)
    fig.tight_layout()

    frames: list[Any] = []
    t_end = float(t[-1]) if t.size else 1.0
    for index in range(n_frames):
        t_i = t_end * (index / (n_frames - 1)) if n_frames > 1 else t_end
        d_i = float(np.interp(t_i, t, d))
        f_i = float(np.interp(t_i, t, f))
        done = t <= t_i
        trace.set_data(d[done], f[done])
        marker.set_data([d_i], [f_i])
        label.set_text(f"t {t_i * 1e3:7.2f} ms\nd {d_i:7.2f} mm\nF {f_i:7.1f} N")
        fig.canvas.draw()
        rgba = np.asarray(fig.canvas.buffer_rgba())
        frames.append(rgba[:, :, :3].copy())
    plt.close(fig)
    return frames


def _beside(shot: Any, panel: Any) -> Any:
    """Place the curve panel beside a rendered frame, padding height with white."""
    import numpy as np  # noqa: PLC0415

    height = max(shot.shape[0], panel.shape[0])

    def pad(image: Any) -> Any:
        if image.shape[0] == height:
            return image
        extra = np.full((height - image.shape[0], image.shape[1], 3), 255, dtype=image.dtype)
        return np.vstack([image, extra])

    return np.hstack([pad(shot), pad(panel)])


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
    curve: Any | None = None,
    rigid_part_ids: Sequence[int] | None = None,
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
        curve: Optional force-displacement frame (``time`` / ``displacement``
            / ``force`` columns): when given, every video/GIF/still carries a
            synced curve panel beside the animation.
        rigid_part_ids: ``PART_ID`` values of the rigid tools; those cells are
            drawn semi-transparent grey so the crushing part stays visible
            through the platen/jig.

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

    panel_frames: list[Any] | None = None
    if curve is not None and len(meshes) > 0:
        try:
            panel_frames = _curve_panel_frames(curve, len(meshes), window_size[1])
        except Exception as exc:  # noqa: BLE001 - the panel must never kill the render
            result.skipped_reason = f"curve panel skipped: {exc}"

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
            deformable, rigid = _split_rigid(geometry, rigid_part_ids)
            plotter = pv.Plotter(off_screen=True, window_size=window_size)
            plotter.set_background("white")
            plotter.add_mesh(
                deformable,
                scalars=field_name,
                cmap=cmap,
                clim=clim,
                show_edges=False,
                scalar_bar_args={"title": field_name or "", "vertical": True},
                show_scalar_bar=field_name is not None,
            )
            if rigid is not None and rigid.n_cells:
                plotter.add_mesh(
                    rigid,
                    color=RIGID_TOOL_COLOR,
                    opacity=RIGID_TOOL_OPACITY,
                    show_edges=False,
                    show_scalar_bar=False,
                )
            plotter.camera_position = camera
            plotter.show(auto_close=False)
            shot = plotter.screenshot(return_img=True)
            plotter.close()
            if panel_frames is not None:
                shot = _beside(shot, panel_frames[len(frames)])
            frames.append(shot)

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
