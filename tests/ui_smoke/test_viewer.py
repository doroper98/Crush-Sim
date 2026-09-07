"""Viewer smoke tests: the generated standalone HTML opened in a real browser.

These check the things a unit test cannot see (UI_002 §3 WP3 완료 조건):

* the result header appears in the fixed order of UI_001 §10.1,
* clicking an event moves the frame slider and the curve cursor together (U35),
* the page makes **no external request** and stays under 16 MB (U39),
* WebGL missing falls back to the last computed frame, not a blank canvas (U38),
* at 390 px the controls are reachable and do not overlap (U37).

Skipped when Playwright, its browser, or a run with VTK frames is missing -
``runs/`` is git-ignored, so CI has no run to view. Point ``CRUSHSIM_RUNS`` at
a directory of runs to use one from elsewhere (a worktree has no ``runs/``).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path(os.environ.get("CRUSHSIM_RUNS") or (ROOT / "runs"))
#: The 30-minute preset run first; any other v5 vent run otherwise.
_PREFERRED = "lc6_pris_vent_burst_v5_preset"
_BROWSERS = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/opt/pw-browsers")
VIEWER_MAX_BYTES = 16 * 1024 * 1024
SCREENSHOT_DIR = ROOT / "docs" / "ui_screens"


def _chromium_executable() -> str | None:
    """The pinned Chromium in the image, whatever build Playwright expects."""
    for candidate in sorted(_BROWSERS.glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    return None


def _run_dir() -> Path | None:
    if not RUNS.is_dir():
        return None
    preferred = RUNS / _PREFERRED
    candidates = [preferred] if preferred.is_dir() else []
    candidates += sorted(RUNS.glob("lc6_pris_vent_burst_v5*"))
    for candidate in candidates:
        if (candidate / "vtk").is_dir() and any((candidate / "vtk").glob("*.vtk")):
            return candidate
    return None


@pytest.fixture(scope="module")
def viewer_html(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A viewer generated from a real run, once for the module."""
    run = _run_dir()
    if run is None:
        pytest.skip(f"no run with VTK frames under {RUNS}")
    pytest.importorskip("pyvista", reason="pyvista is not installed")
    from crushsim.ui.viewergen import generate_viewer  # noqa: PLC0415

    target = tmp_path_factory.mktemp("viewer") / "viewer.html"
    return generate_viewer(run, title=run.name, out_path=target)


@pytest.fixture(scope="module")
def browser():
    executable = _chromium_executable()
    with sync_playwright() as play:
        try:
            instance = play.chromium.launch(executable_path=executable)
        except Exception as exc:  # noqa: BLE001 - a missing browser is a skip
            pytest.skip(f"chromium unavailable: {exc}")
        yield instance
        instance.close()


def _page(browser, **kwargs):
    """A page that fails the test on any request leaving the file (U39)."""
    context = browser.new_context(**kwargs)
    page = context.new_page()
    external: list[str] = []
    page.on("request", lambda req: external.append(req.url) if not req.url.startswith("file:") else None)
    page.external_requests = external  # type: ignore[attr-defined]
    return page


def test_viewer_is_standalone_and_under_16_mb(viewer_html: Path, browser) -> None:
    """U39: opens offline, no external request, ≤ 16 MB."""
    size = viewer_html.stat().st_size
    assert size <= VIEWER_MAX_BYTES, f"{size / 1024 / 1024:.2f} MB > 16 MB"
    assert "fonts.googleapis.com" not in viewer_html.read_text(encoding="utf-8")
    page = _page(browser)
    page.goto(viewer_html.as_uri())
    page.wait_for_selector("#hdrJudgement")
    assert page.external_requests == []
    page.close()


def test_header_is_in_the_fixed_order(viewer_html: Path, browser) -> None:
    """UI_001 §10.1: name/time/changed → judgement → target+trust → metrics → 3D → links."""
    page = _page(browser)
    page.goto(viewer_html.as_uri())
    page.wait_for_selector("#hdrMetrics li")
    order = page.evaluate(
        """() => {
            const ids = ["hdrTitle","hdrTime","hdrChanged","hdrJudgement","hdrTarget",
                         "hdrValidity","hdrScope","hdrMetrics","gl","chart","links"];
            const nodes = ids.map(id => document.getElementById(id));
            return nodes.map((n, i) => n ? i : -1).filter(i => i >= 0).map(i => {
                const n = nodes[i];
                let top = 0, el = n;
                while (el) { top += el.offsetTop || 0; el = el.offsetParent; }
                return [ids[i], top];
            });
        }"""
    )
    seen = [name for name, _ in order]
    assert seen == [
        "hdrTitle", "hdrTime", "hdrChanged", "hdrJudgement", "hdrTarget",
        "hdrValidity", "hdrScope", "hdrMetrics", "gl", "chart", "links",
    ]
    tops = [top for _, top in order]
    assert tops == sorted(tops), f"header elements out of visual order: {order}"
    # The judgement is a sentence from the server, never an invented one.
    assert page.text_content("#hdrJudgement").strip()
    for badge in ("#hdrTarget", "#hdrValidity", "#hdrScope"):
        assert page.text_content(badge).strip()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / "viewer_header.png"))
    page.close()


def test_clicking_an_event_moves_frame_and_curve_together(viewer_html: Path, browser) -> None:
    """U35: one click, both cursors."""
    page = _page(browser)
    page.goto(viewer_html.as_uri())
    page.wait_for_selector("#eventList button")
    before = page.eval_on_selector("#time", "el => el.value")
    frame_before = page.text_content("#frameno")
    buttons = page.query_selector_all("#eventList button:not([disabled])")
    assert buttons, "no jumpable event marker"
    buttons[-1].click()
    after = page.eval_on_selector("#time", "el => el.value")
    assert after != before, "the time slider did not move"
    assert page.text_content("#frameno") != frame_before
    # The curve readout follows the same instant.
    assert "ms" in page.text_content("#readout")
    page.screenshot(path=str(SCREENSHOT_DIR / "viewer_event.png"))
    page.close()


def test_frame_stepping_and_sample_labels(viewer_html: Path, browser) -> None:
    """§10.3: a stepped frame is a computed sample and says so."""
    page = _page(browser)
    page.goto(viewer_html.as_uri())
    page.wait_for_selector("#next")
    page.click("#next")
    assert "계산 샘플" in page.text_content("#sampleTag")
    page.eval_on_selector("#time", "el => { el.value = 7; el.dispatchEvent(new Event('input')); }")
    assert "보간" in page.text_content("#sampleTag")
    assert "변형 1×" in page.text_content("#scaleTag")
    page.close()


def test_webgl_unavailable_falls_back_to_the_last_frame(viewer_html: Path, browser) -> None:
    """U38: a still frame, the metrics and the report link - never a blank canvas."""
    page = _page(browser)
    page.add_init_script(
        """
        const real = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
            // "experimental-webgl" is the second thing render3d.js tries, so
            // matching only a leading "webgl" left a real context behind.
            if (String(kind).indexOf('webgl') >= 0) return null;
            return real.call(this, kind, ...rest);
        };
        """
    )
    page.goto(viewer_html.as_uri())
    page.wait_for_selector(".r3d-unavailable")
    assert page.is_visible(".r3d-unavailable img")
    # The judgement and the metrics are still the first thing on the page.
    assert page.text_content("#hdrJudgement").strip()
    assert page.query_selector_all("#hdrMetrics li")
    # The report link the fallback text points at must actually be there, and
    # the controls that need the renderer must not sit there doing nothing.
    assert page.query_selector_all("#links a")
    assert not page.is_visible("#timeSec")
    assert not page.is_visible("#partsSec")
    assert page.external_requests == []
    page.screenshot(path=str(SCREENSHOT_DIR / "viewer_no_webgl.png"))
    page.close()


def test_mobile_390px_controls_are_reachable(viewer_html: Path, browser) -> None:
    """U37: play, rotate, zoom and the curve on a 390 px phone, no overlap."""
    page = _page(browser, viewport={"width": 390, "height": 780}, is_mobile=False)
    page.goto(viewer_html.as_uri())
    page.wait_for_selector("#play")
    boxes = {}
    for selector in ("#play", "#next", "#rotL", "#zoomIn", "#fitView", "#time"):
        box = page.query_selector(selector).bounding_box()
        assert box is not None, f"{selector} is not laid out"
        assert box["width"] > 0 and box["height"] > 0
        assert box["x"] + box["width"] <= 390 + 1, f"{selector} runs off a 390 px screen"
        boxes[selector] = box
    # Buttons in the same row must not sit on top of each other.
    play, nxt = boxes["#play"], boxes["#next"]
    assert play["x"] + play["width"] <= nxt["x"] + 1 or play["y"] + play["height"] <= nxt["y"] + 1
    # The floating curve card must not sit on top of the header (U37).
    head = page.query_selector("#rhead").bounding_box()
    card = page.query_selector("#chartCard").bounding_box()
    assert card["y"] >= head["y"] + head["height"] - 1, "the curve card overlaps the header"
    # Shot before the clicks: Playwright scrolls a target into view, which
    # would leave the panel parked halfway down in the screenshot.
    page.screenshot(path=str(SCREENSHOT_DIR / "viewer_mobile_390.png"), full_page=False)
    page.click("#rotL")
    page.click("#zoomIn")
    page.close()


def test_embedded_result_matches_the_contract(viewer_html: Path) -> None:
    """The header data is the server's result contract, not a viewer invention."""
    html = viewer_html.read_text(encoding="utf-8")
    payload = re.search(
        r'<script type="application/json" id="simdata">(.*?)</script>', html, re.S
    )
    assert payload, "simdata payload missing"
    data = json.loads(payload.group(1))
    result = data.get("result")
    assert result is not None, "no result contract embedded"
    assert result["judgement"]["sentence"]
    assert result["validation"]["model_validity"] in {"valid", "invalid", "unknown"}
    assert result["validation"]["scope_status"] in {"verified", "unverified", "out_of_scope"}
    # U39: a reduced frame count is recorded, and the curves are untouched.
    reduction = data["meta"].get("frame_reduction")
    if reduction:
        assert reduction["kept_frames"] == data["meta"]["frames"]
        assert reduction["kept_frames"] < reduction["original_frames"]
    assert data["curve"]["t"], "the curve must survive any frame reduction"
