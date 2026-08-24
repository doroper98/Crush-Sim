"""FR-01 - CATPart -> STEP AP214 batch conversion through CATIA V5 COM.

Windows only: the conversion drives a running CATIA V5 through ``pywin32``.
On any other platform, or without the ``windows`` extra, every entry point
raises :class:`~crushsim.errors.OptionalDependencyError` naming the install
command - it never pretends to have converted anything.

Batch rule (spec §4 FR-01): one failing file must not kill the batch, and a
``conversion_log.csv`` recording file name, result and error is mandatory.
"""

from __future__ import annotations

import csv
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CrushSimError, OptionalDependencyError

STEP_FORMAT: str = "stp"
"""CATIA export format token for STEP."""

DEFAULT_PATTERNS: tuple[str, ...] = ("*.CATPart", "*.CATProduct")
"""File patterns converted by default: parts and assemblies alike."""


@dataclass(slots=True)
class ConversionRecord:
    """One file's conversion outcome, as written to ``conversion_log.csv``."""

    source: str
    target: str
    status: str
    """``"ok"``, ``"failed"`` or ``"skipped"``."""
    error: str = ""

    def to_row(self) -> dict[str, str]:
        """Row form for the CSV log."""
        return {
            "source": self.source,
            "target": self.target,
            "status": self.status,
            "error": self.error,
        }


def catia_available() -> tuple[bool, str]:
    """Whether CATIA COM automation can be attempted here (never raises)."""
    if platform.system() != "Windows":
        return False, f"CATIA COM requires Windows; this is {platform.system()}"
    try:
        import win32com.client  # noqa: F401, PLC0415
    except ImportError as exc:
        return False, f"pywin32 is not installed ({exc}); pip install crushsim[windows]"
    return True, "pywin32 present"


def _require_catia() -> Any:
    """Import the CATIA COM client or raise a clear, actionable error.

    Raises:
        OptionalDependencyError: If pywin32 is missing.
        CrushSimError: If the platform is not Windows.
    """
    if platform.system() != "Windows":
        raise CrushSimError(
            f"CATPart conversion requires Windows with CATIA V5 installed; "
            f"this machine reports {platform.system()}. Convert on the Windows "
            "workstation and copy the STEP files over."
        )
    try:
        import win32com.client  # noqa: PLC0415
    except ImportError as exc:
        raise OptionalDependencyError(
            "pywin32", "windows", purpose="CATIA V5 COM automation"
        ) from exc
    return win32com.client


def write_conversion_log(records: list[ConversionRecord], path: str | Path) -> Path:
    """Write ``conversion_log.csv`` (spec §4 FR-01, mandatory)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source", "target", "status", "error"])
        writer.writeheader()
        for record in records:
            writer.writerow(record.to_row())
    return target


def batch_convert(
    indir: str | Path,
    outdir: str | Path,
    *,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    log_path: str | Path | None = None,
) -> list[ConversionRecord]:
    """Convert every CATPart/CATProduct in ``indir`` to STEP AP214 in ``outdir``.

    CATIA's ``ExportData`` exports a Product document as one STEP assembly,
    so both parts and products go through the same call. A failure on one
    file is recorded and the batch continues.

    Raises:
        OptionalDependencyError: If pywin32 is missing.
        CrushSimError: If not running on Windows, or the input folder is absent.
    """
    source_dir = Path(indir)
    if not source_dir.is_dir():
        raise CrushSimError(f"Input folder not found: {source_dir}")
    sources = sorted({path for pattern in patterns for path in source_dir.glob(pattern)})
    client = _require_catia()  # pragma: no cover - Windows only

    target_dir = Path(outdir)  # pragma: no cover - Windows only
    target_dir.mkdir(parents=True, exist_ok=True)
    catia = client.Dispatch("CATIA.Application")
    catia.Visible = False

    records: list[ConversionRecord] = []
    claimed: set[str] = set()
    for source in sources:
        # A same-stem part and product would both target <stem>.stp; the
        # later one gets its source extension appended instead of clobbering.
        stem = source.stem
        if stem.lower() in claimed:
            stem = f"{source.stem}_{source.suffix.lstrip('.').lower()}"
        claimed.add(stem.lower())
        target = target_dir / f"{stem}.stp"
        document = None
        try:
            document = catia.Documents.Open(str(source))
            document.ExportData(str(target), STEP_FORMAT)
            records.append(ConversionRecord(str(source), str(target), "ok"))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            records.append(ConversionRecord(str(source), str(target), "failed", str(exc)))
        finally:
            if document is not None:
                try:
                    document.Close()
                except Exception:  # noqa: BLE001 - closing failures are non-fatal
                    pass

    write_conversion_log(records, log_path or target_dir / "conversion_log.csv")
    return records
