"""Parse the legacy ``MaterialModel.ts`` material library into YAML cards (FR-10).

Only the *data* of the legacy file is harvested. The legacy hardening code
(``flowStress`` etc.) is deliberately not ported - the solver's own material law
replaces it (spec §1.1, §1.3, §13.1).

Mapping (spec §4 FR-10):

===========================  ==============  ============================
legacy field                 YAML field      /MAT/LAW2
===========================  ==============  ============================
``youngsModulus`` [MPa]      ``E``           E
``poissonRatio``             ``nu``          nu
``density`` [kg/m^3]         ``rho``         rho, converted x1e-12
``yieldStress`` [MPa]        ``sigma_y``     A
K = (UTS - sy) / 0.3^n       ``K``           B
``hardeningExponent``        ``n``           n
``wallThickness`` [mm]       ``t_default``   shell thickness default
===========================  ==============  ============================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from ..errors import HarvestError
from ..units import (
    HARDENING_REFERENCE_STRAIN,
    UNIT_SYSTEM,
    kg_per_m3_to_tonne_per_mm3,
)

SOURCE_TAG: Final[str] = "legacy-can_crush_sim"
"""Provenance tag stamped on every harvested card (spec §4 FR-10)."""

_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*createMaterial\(\s*\{(?P<body>.*?)\}\s*\)",
    re.DOTALL,
)
"""Matches ``key: createMaterial({ ... })`` entries of the legacy record."""

_STRING_FIELD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<quote>['\"`])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)

_NUMBER_FIELD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
)

_NUMERIC_FIELDS: Final[tuple[str, ...]] = (
    "youngsModulus",
    "poissonRatio",
    "yieldStress",
    "uts",
    "density",
    "hardeningExponent",
    "wallThickness",
)


@dataclass(frozen=True, slots=True)
class HarvestedMaterial:
    """One legacy material converted to the crushsim unit system."""

    key: str
    name: str
    E: float
    """Young's modulus [MPa]."""
    nu: float
    """Poisson ratio [-]."""
    rho: float
    """Density [tonne/mm^3] (converted from the legacy kg/m^3)."""
    sigma_y: float
    """Initial yield stress [MPa]."""
    uts: float
    """Ultimate tensile strength [MPa]."""
    K: float
    """Hardening coefficient [MPa], (UTS - sigma_y) / 0.3^n."""
    n: float
    """Hardening exponent [-]."""
    t_default: float
    """Default shell thickness [mm]."""
    rho_kg_m3: float
    """Original legacy density [kg/m^3], kept for traceability."""

    def to_card(self) -> dict[str, Any]:
        """Render the YAML card body (spec §4 FR-10)."""
        return {
            "name": self.name,
            "E": self.E,
            "nu": self.nu,
            "rho": self.rho,
            "sigma_y": self.sigma_y,
            "uts": self.uts,
            "K": self.K,
            "n": self.n,
            "t_default": self.t_default,
            "law2": {"A": self.law2_A, "B": self.law2_B, "n": self.law2_n},
            "verified": False,
            "verification": "unverified",
            "source": SOURCE_TAG,
            "unit_system": UNIT_SYSTEM,
            "rho_source_kg_m3": self.rho_kg_m3,
        }

    @property
    def law2_A(self) -> float:
        """/MAT/LAW2 A term [MPa] - the initial yield stress."""
        return self.sigma_y

    @property
    def law2_B(self) -> float:
        """/MAT/LAW2 B term [MPa] - the hardening coefficient K."""
        return self.K

    @property
    def law2_n(self) -> float:
        """/MAT/LAW2 hardening exponent [-]."""
        return self.n


@dataclass(slots=True)
class HarvestReport:
    """Outcome of one harvest run."""

    source: Path
    materials: list[HarvestedMaterial] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(key, reason)`` pairs for entries that could not be converted."""

    @property
    def count(self) -> int:
        """Number of successfully harvested materials."""
        return len(self.materials)


def _hardening_coefficient(uts: float, sigma_y: float, n: float) -> float:
    """K = (UTS - sigma_y) / eps_ref**n, matching the legacy definition."""
    return (uts - sigma_y) / (HARDENING_REFERENCE_STRAIN**n)


def parse_material_source(text: str, *, source: str | Path = "<string>") -> list[HarvestedMaterial]:
    """Parse legacy ``MaterialModel.ts`` source text into harvested materials.

    Args:
        text: Contents of the legacy TypeScript file.
        source: Label used in error messages.

    Returns:
        Materials in source order; duplicate keys keep their first occurrence.

    Raises:
        HarvestError: If no ``createMaterial({...})`` entry is found at all
            (spec §13.5 - a silent empty result is a bug, not a result).
    """
    materials: list[HarvestedMaterial] = []
    seen: set[str] = set()

    for match in _ENTRY_RE.finditer(text):
        key = match.group("key")
        body = match.group("body")
        if key in seen:
            continue
        strings = {m.group("field"): m.group("value") for m in _STRING_FIELD_RE.finditer(body)}
        numbers = {m.group("field"): float(m.group("value")) for m in _NUMBER_FIELD_RE.finditer(body)}
        missing = [f for f in _NUMERIC_FIELDS if f not in numbers]
        if missing:
            raise HarvestError(
                f"{source}: material {key!r} is missing field(s) {missing}. "
                "The legacy schema changed - update crushsim.harvest.legacy_ts."
            )
        n = numbers["hardeningExponent"]
        sigma_y = numbers["yieldStress"]
        uts = numbers["uts"]
        rho_kg_m3 = numbers["density"]
        materials.append(
            HarvestedMaterial(
                key=key,
                name=strings.get("name", key),
                E=numbers["youngsModulus"],
                nu=numbers["poissonRatio"],
                rho=kg_per_m3_to_tonne_per_mm3(rho_kg_m3),
                sigma_y=sigma_y,
                uts=uts,
                K=_hardening_coefficient(uts, sigma_y, n),
                n=n,
                t_default=numbers["wallThickness"],
                rho_kg_m3=rho_kg_m3,
            )
        )
        seen.add(key)

    if not materials:
        raise HarvestError(
            f"{source}: no createMaterial({{...}}) entries found. "
            "Check that the path points at the legacy src/engine/MaterialModel.ts."
        )
    return materials


def write_cards(
    materials: list[HarvestedMaterial],
    outdir: str | Path,
    *,
    overwrite: bool = True,
) -> list[Path]:
    """Write one YAML card per material into ``outdir``.

    Raises:
        HarvestError: If a card already exists and ``overwrite`` is False.
    """
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for material in materials:
        path = out / f"{material.key}.yaml"
        if path.exists() and not overwrite:
            raise HarvestError(f"Refusing to overwrite existing card: {path}")
        header = (
            f"# Harvested from {SOURCE_TAG} (FR-10). Data only - no legacy code was reused.\n"
            f"# Units: {UNIT_SYSTEM}. UNVERIFIED: literature cross-check required "
            "before production use.\n"
        )
        body = yaml.safe_dump(material.to_card(), sort_keys=False, allow_unicode=True)
        path.write_text(header + body, encoding="utf-8")
        written.append(path)
    return written


def harvest_file(
    input_path: str | Path,
    outdir: str | Path,
    *,
    overwrite: bool = True,
) -> HarvestReport:
    """Harvest the legacy material library at ``input_path`` into ``outdir``.

    Raises:
        HarvestError: If the input file does not exist or contains no entries.
    """
    src = Path(input_path)
    if not src.is_file():
        raise HarvestError(
            f"Legacy material file not found: {src}. "
            "Expected the legacy repository's src/engine/MaterialModel.ts."
        )
    text = src.read_text(encoding="utf-8", errors="replace")
    materials = parse_material_source(text, source=src)
    written = write_cards(materials, outdir, overwrite=overwrite)
    return HarvestReport(source=src, materials=materials, written=written)
