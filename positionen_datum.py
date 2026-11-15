"""Einheitliche, fail-closed Datumsnormalisierung fuer Positionsdaten.

Kalenderdaten werden ohne Zeitzone verarbeitet. ISO-Werte werden strikt
Jahr-Monat-Tag interpretiert; deutsche Werte day-first. Sheets-/Excel-
Seriennummern verwenden den Epoch 1899-12-30.
"""
from __future__ import annotations

import datetime as _dt
import math
import numbers
import re
from typing import Any

import pandas as pd

_SHEETS_EPOCH = _dt.datetime(1899, 12, 30)
_NUMERIC_TEXT_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")


def _invalid(field: str, value: Any, detail: str = "Ungueltiger Datumswert") -> ValueError:
    return ValueError(f"{detail} in {field}: {value!r}")


def _serial(number: float, *, field: str, original: Any) -> pd.Timestamp:
    if not math.isfinite(number) or not 20000 <= number <= 100000:
        raise _invalid(field, original, "Ungueltige Sheets-Datumsseriennummer")
    try:
        return pd.Timestamp(_SHEETS_EPOCH + _dt.timedelta(days=number))
    except (OverflowError, ValueError) as exc:
        raise _invalid(field, original, "Ungueltige Sheets-Datumsseriennummer") from exc


def parse_position_date(value: Any, *, field: str = "Datum") -> pd.Timestamp | None:
    """Parse position dates deterministically; never silently discard timezone."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid(field, value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, numbers.Real):
        return _serial(float(value), field=field, original=value)

    # Preserve native date/time objects without converting through locale parsing.
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise _invalid(field, value, "Zeitzonenangaben sind nicht zulaessig")
        return pd.Timestamp(value)
    if isinstance(value, _dt.date):
        return pd.Timestamp(value)

    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None

    if _NUMERIC_TEXT_RE.fullmatch(text):
        number = float(text.replace(',', '.'))
        # Numeric strings in the serial-date range are common in CSV/Sheets
        # exports. Small integers remain invalid rather than becoming 1900 dates.
        if number >= 20000:
            return _serial(number, field=field, original=value)
        raise _invalid(field, value, "Numerischer Text ist keine gueltige Datumsseriennummer")

    if _ISO_DATE_RE.fullmatch(text):
        try:
            return pd.Timestamp(_dt.datetime.strptime(text, "%Y-%m-%d"))
        except ValueError as exc:
            raise _invalid(field, value, "Ungueltiger ISO-Datumswert") from exc

    if _ISO_DATETIME_RE.match(text):
        try:
            parsed = _dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise _invalid(field, value, "Ungueltiger ISO-Datumswert") from exc
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            raise _invalid(field, value, "Zeitzonenangaben sind nicht zulaessig")
        return pd.Timestamp(parsed)

    # Explicit formats avoid pandas' locale heuristics and warnings.
    formats = [
        "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y",
        "%d.%m.%y", "%d/%m/%y", "%d-%m-%y",
    ]
    for fmt in formats:
        try:
            return pd.Timestamp(_dt.datetime.strptime(text, fmt))
        except ValueError:
            continue

    # Unambiguous US month-first dates (e.g. 7/15/2026); ambiguous dates
    # such as 07/08/2026 remain day-first for compatibility with German sheets.
    us_match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})", text)
    if us_match and int(us_match.group(2)) > 12:
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return pd.Timestamp(_dt.datetime.strptime(text, fmt))
            except ValueError:
                continue

    # Legacy day/month values without a year use the current calendar year,
    # matching the former tracker behavior; invalid values still fail closed.
    legacy = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text)
    if legacy:
        day, month = map(int, legacy.groups())
        try:
            return pd.Timestamp(_dt.datetime(_dt.datetime.now().year, month, day))
        except ValueError as exc:
            raise _invalid(field, value) from exc

    raise _invalid(field, value)


def position_date_to_sheets_serial(value: Any, *, field: str = "Datum") -> float | None:
    """Convert a parsed position calendar date to the Sheets epoch serial.

    Naive time components are preserved. Empty optional values remain None.
    """
    parsed = parse_position_date(value, field=field)
    if parsed is None:
        return None
    stamp = parsed.to_pydatetime()
    return (stamp - _SHEETS_EPOCH).total_seconds() / 86400.0
