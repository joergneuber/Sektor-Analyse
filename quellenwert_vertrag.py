"""Gemeinsamer Datenvertrag fuer Quellenwerte der Tagesauswertung.

Kernregeln:
- Messwert, Einheit, Messart und Zeitbasis sind getrennte Merkmale.
- Live-/Intraday-Werte sind nicht automatisch mit Schlusskursen vergleichbar.
- 4W-Rendite: letzter abgeschlossener Tages-Schlusskurs gegen den Schlusskurs
  20 abgeschlossene Handelssitzungen zuvor (21 Schlusskurse fuer 20 Intervalle).
- Zahlenvergleiche beruecksichtigen die angezeigte Dezimalpraezision.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence


@dataclass(frozen=True)
class QuellenwertVertrag:
    key: str
    source: str
    observed_at: str
    unit: str
    measurement: str
    temporal_basis: str
    calculation: str = "direct"
    status: str = "REAL"


def decimal_places(raw: str) -> int:
    """Number of displayed decimal places in a numeric string."""
    s = str(raw).strip().replace(" ", "")
    if "." in s and "," in s:
        # The right-most separator is the decimal separator.
        sep = "." if s.rfind(".") > s.rfind(",") else ","
        return len(s.rsplit(sep, 1)[1])
    if "," in s:
        tail = s.rsplit(",", 1)[1]
        return len(tail) if tail.isdigit() else 0
    if "." in s:
        tail = s.rsplit(".", 1)[1]
        # A single dot with 3 trailing digits is usually a decimal in source
        # values used here; thousand grouping is handled by the numeric parser.
        return len(tail) if tail.isdigit() else 0
    return 0


def display_tolerance(actual_raw: str) -> float:
    """Half a unit in the last displayed decimal, with a tiny FP floor."""
    return max(1e-10, 0.5 * (10 ** (-decimal_places(actual_raw))))


def values_match_display(actual: float, expected: float, actual_raw: str) -> bool:
    """Compare to a source value using the precision actually shown in output."""
    return abs(float(actual) - float(expected)) <= display_tolerance(actual_raw) + 1e-12


def same_measurement_basis(
    left_measurement: str,
    left_temporal_basis: str,
    right_measurement: str,
    right_temporal_basis: str,
) -> bool:
    """Only compare values representing the same measurement and time basis."""
    return (
        str(left_measurement).strip().casefold() == str(right_measurement).strip().casefold()
        and str(left_temporal_basis).strip().casefold() == str(right_temporal_basis).strip().casefold()
    )


def four_week_return(closes: Sequence[float], sessions: int = 20) -> Optional[float]:
    """Return percentage over `sessions` completed intervals, using closes only.

    A 20-session return needs 21 closing prices: close[-1] / close[-21] - 1.
    Input must already contain completed daily bars in chronological order.
    """
    if sessions < 1 or len(closes) < sessions + 1:
        return None
    try:
        last = Decimal(str(closes[-1]))
        base = Decimal(str(closes[-(sessions + 1)]))
        if base == 0:
            return None
        return float((last / base - Decimal("1")) * Decimal("100"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def find_exact_labeled_line(source: str, label: str) -> Optional[str]:
    """Find a source row whose field label starts the row and matches exactly."""
    import re
    pattern = re.compile(r"(?i)^\s*(?:[-•]\s*)?" + re.escape(label) + r"\s*:")
    return next((line.strip() for line in str(source or "").splitlines() if pattern.match(line)), None)


def extract_four_week_percent(line: str) -> Optional[str]:
    """Return only the percentage explicitly attached to a 4W label.

    Percentages describing distance to a 52-week high/low are intentionally
    ignored, even when they occur on the same line.
    """
    import re
    patterns = (
        r"(?i)([-+]?\d[\d.,]*)\s*%\s*4W\b",
        r"(?i)\b4W\s*[:=]?\s*([-+]?\d[\d.,]*)\s*%",
        r"(?i)([-+]?\d[\d.,]*)\s*%\s*in\s+den\s+letzten\s+4\s+Wochen\b",
    )
    for pattern in patterns:
        match = re.search(pattern, str(line or ""))
        if match:
            return match.group(1)
    return None


def parse_localized_number(raw: str) -> Optional[float]:
    """Parse German/English decimal and thousands separators for source values."""
    s = str(raw or "").strip().strip(".,;:()[]{}")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def extract_metal_four_week_value(source: str, metal: str):
    """Return `(raw, numeric)` for the 4W field on the metal's own source row."""
    import re
    line = next((row.strip() for row in str(source or "").splitlines()
                 if re.match(r"(?i)^\s*(?:[-•]\s*)?" + re.escape(metal) + r"\s*:\s*Kurs\b", row)), None)
    if not line:
        return None
    raw = extract_four_week_percent(line)
    value = parse_localized_number(raw) if raw is not None else None
    return (raw, value) if raw is not None and value is not None else None
