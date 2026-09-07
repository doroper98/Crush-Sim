"""Atomic small-file writes for the UI's state files.

The UI writes state from background threads (the run worker, the asset
inspector) that request handlers read at the same time. ``Path.write_text``
truncates first, so a reader that lands in that window sees an empty file:
measured as an intermittent ``JSONDecodeError: Expecting value: line 1
column 1`` while reading ``state.json`` during a repeated submit.

Writing to a sibling temp file and ``os.replace``-ing it makes every read see
either the old file or the new one - never a half-written one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_text_atomic(path: str | Path, text: str) -> Path:
    """Replace ``path`` with ``text`` atomically (same filesystem)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def write_json_atomic(path: str | Path, data: Any, *, indent: int | None = 1) -> Path:
    """Serialise ``data`` as UTF-8 JSON and write it atomically."""
    return write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=indent))


__all__ = ["write_json_atomic", "write_text_atomic"]
