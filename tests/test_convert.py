"""FR-01 CATPart/CATProduct batch conversion.

The real conversion needs Windows + CATIA V5, so these tests drive
``batch_convert`` with a faked COM client: the batch rules (both extensions
by default, one failure never stops the batch, same-stem collision handling,
mandatory ``conversion_log.csv``) are all platform-independent logic.
"""

from __future__ import annotations

import csv
import platform
from pathlib import Path

import pytest

from crushsim.converter.catia import (
    DEFAULT_PATTERNS,
    ConversionRecord,
    batch_convert,
    write_conversion_log,
)
from crushsim.errors import CrushSimError


class _FakeDocument:
    def __init__(self, path: str) -> None:
        self._path = path
        self.closed = False

    def ExportData(self, target: str, fmt: str) -> None:  # noqa: N802 - COM casing
        assert fmt == "stp"
        Path(target).write_text(f"STEP from {self._path}")

    def Close(self) -> None:  # noqa: N802 - COM casing
        self.closed = True


class _FakeDocuments:
    def Open(self, path: str) -> _FakeDocument:  # noqa: N802 - COM casing
        if "broken" in path:
            raise RuntimeError("CATIA could not open the document")
        return _FakeDocument(path)


class _FakeCatia:
    def __init__(self) -> None:
        self.Documents = _FakeDocuments()
        self.Visible = True


class _FakeClient:
    @staticmethod
    def Dispatch(name: str) -> _FakeCatia:  # noqa: N802 - COM casing
        assert name == "CATIA.Application"
        return _FakeCatia()


@pytest.fixture
def catia_stub(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("crushsim.converter.catia._require_catia", lambda: _FakeClient())


def test_default_patterns_cover_parts_and_products() -> None:
    assert DEFAULT_PATTERNS == ("*.CATPart", "*.CATProduct")


def test_batch_convert_requires_the_input_folder(tmp_path: Path) -> None:
    with pytest.raises(CrushSimError, match="Input folder not found"):
        batch_convert(tmp_path / "missing", tmp_path / "out")


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX platform-guard path")
def test_batch_convert_refuses_non_windows(tmp_path: Path) -> None:
    with pytest.raises(CrushSimError, match="requires Windows"):
        batch_convert(tmp_path, tmp_path / "out")


def test_batch_convert_handles_both_extensions_and_failures(
    tmp_path: Path, catia_stub: None
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "jig.CATPart").write_text("part")
    (src / "assembly.CATProduct").write_text("product")
    (src / "broken.CATPart").write_text("bad")
    (src / "notes.txt").write_text("ignored")
    out = tmp_path / "out"

    records = batch_convert(src, out)

    by_source = {Path(r.source).name: r for r in records}
    assert set(by_source) == {"jig.CATPart", "assembly.CATProduct", "broken.CATPart"}
    assert by_source["jig.CATPart"].status == "ok"
    assert by_source["assembly.CATProduct"].status == "ok"
    assert by_source["broken.CATPart"].status == "failed"
    assert "could not open" in by_source["broken.CATPart"].error
    assert (out / "jig.stp").is_file()
    assert (out / "assembly.stp").is_file()

    log_rows = list(csv.DictReader((out / "conversion_log.csv").open()))
    assert len(log_rows) == 3
    assert {row["status"] for row in log_rows} == {"ok", "failed"}


def test_batch_convert_same_stem_part_and_product_do_not_clobber(
    tmp_path: Path, catia_stub: None
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "can.CATPart").write_text("part")
    (src / "can.CATProduct").write_text("product")
    out = tmp_path / "out"

    records = batch_convert(src, out)

    targets = sorted(Path(r.target).name for r in records)
    assert targets == ["can.stp", "can_catproduct.stp"]
    assert all(r.status == "ok" for r in records)


def test_write_conversion_log_round_trips(tmp_path: Path) -> None:
    log = write_conversion_log(
        [ConversionRecord("a.CATPart", "a.stp", "ok")], tmp_path / "log.csv"
    )
    rows = list(csv.DictReader(log.open()))
    assert rows == [{"source": "a.CATPart", "target": "a.stp", "status": "ok", "error": ""}]
