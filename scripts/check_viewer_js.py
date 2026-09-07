#!/usr/bin/env python3
"""Extract the viewer template's inline <script> bodies and run node --check.

The shipped viewer is one standalone HTML file (UI_002 §1.1), so its JS cannot
be linted in place. This pulls every inline script into a temp file - with the
``__RENDER3D__`` placeholder replaced by the real module, exactly as
``viewergen`` assembles it - and hands them to ``node --check``.

Usage:  python scripts/check_viewer_js.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "crushsim" / "ui" / "static"
TEMPLATE = STATIC / "viewer_template.html"
RENDER3D = STATIC / "render3d.js"

SCRIPT_RE = re.compile(r"<script(?![^>]*type=\"application/json\")[^>]*>(.*?)</script>", re.S)


def main() -> int:
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__RENDER3D__", RENDER3D.read_text(encoding="utf-8"))
    bodies = [body for body in SCRIPT_RE.findall(html) if body.strip()]
    if not bodies:
        print("no inline script found in", TEMPLATE)
        return 1
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for index, body in enumerate(bodies):
            # The generator substitutes these before a browser ever sees them;
            # for a syntax check they only have to be valid literals.
            body = body.replace("__DATA__", "{}").replace("__TITLE__", "t").replace("__NOTE__", "n")
            path = Path(tmp) / f"viewer_script_{index}.js"
            path.write_text(body, encoding="utf-8")
            proc = subprocess.run(
                ["node", "--check", str(path)], capture_output=True, text=True, check=False
            )
            if proc.returncode != 0:
                failures += 1
                print(f"script #{index} failed node --check:\n{proc.stderr}")
    if failures:
        return 1
    print(f"node --check OK: {len(bodies)} inline script(s) in {TEMPLATE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
