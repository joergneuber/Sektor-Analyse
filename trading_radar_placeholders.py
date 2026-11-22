"""Shared placeholder classification for Trading-Radar report normalization and audit."""
from __future__ import annotations
import re


_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:NICHT\s+(?:VERF(?:Ü|UE)GBAR|ANGEGEBEN|ERMITTELT|VORHANDEN|"
    r"VERIFIZIERBAR|BEKANNT|BELEGT|SEPARAT\s+AUSGEWIESEN)|"
    r"KEINE\s+(?:KONKRETEN\s+)?DATEN|FEHLT|UNBEKANNT|UNKLAR|"
    r"TBD|TBA|TODO|XXX|N/?A|NA|PLACEHOLDER|PLATZHALTER|NONE|NULL|[-–—])"
    r"(?:\s*\([^\r\n]*\))?$"
)


def is_placeholder_value(value: object) -> bool:
    """Return True for empty values and shared, optionally annotated placeholders."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().strip(". ")
    return not normalized or bool(_PLACEHOLDER_RE.fullmatch(normalized))
