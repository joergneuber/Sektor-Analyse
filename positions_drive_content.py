"""Stable value fingerprinting for Google Sheets positions masters.

Drive ``version`` can advance for metadata/formatting changes.  This helper
fingerprints every worksheet's cell values and formulas so callers can
distinguish harmless revisions from changes to the actual position data.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal, InvalidOperation
from numbers import Number
from typing import Any

from openpyxl.utils.datetime import to_excel


def _canonical_number(value: Any) -> str:
    try:
        number = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Nicht kanonisierbarer numerischer Zellwert: {value!r}") from exc
    if number == 0:
        return "0"
    return format(number, "f")


def _canonical_cell(value: Any):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return ["boolean", value]
    if isinstance(value, (dt.datetime, dt.date)):
        return ["number", _canonical_number(to_excel(value))]
    if isinstance(value, Number):
        return ["number", _canonical_number(value)]
    return ["string", str(value)]


def sheet_values_sha256(tabs: list[dict[str, Any]]) -> str:
    """Fingerprint a list of {title, values} tabs independent of blank tails/types."""
    normalized_tabs = []
    for tab in tabs:
        rows = []
        for row in tab.get("values", []):
            normalized = [_canonical_cell(value) for value in row]
            while normalized and normalized[-1] is None:
                normalized.pop()
            rows.append(normalized)
        while rows and not rows[-1]:
            rows.pop()
        normalized_tabs.append({"title": str(tab.get("title") or ""), "values": rows})
    canonical = json.dumps(
        normalized_tabs, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def google_sheet_content_sha256(sheets_service: Any, spreadsheet_id: str) -> str:
    """Return a deterministic SHA-256 over sheet titles, cell values and formulas.

    The complete workbook is covered (not only the first tab). Formatting,
    frozen rows and other presentation-only changes do not affect the digest.
    Formula expressions are retained so a newly inserted formula cannot masquerade
    as unchanged data. Number/date formatting is deliberately ignored.
    """
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(title,index)",
    ).execute()
    properties = [
        item.get("properties", {})
        for item in spreadsheet.get("sheets", [])
        if isinstance(item, dict) and isinstance(item.get("properties"), dict)
    ]
    properties.sort(key=lambda item: int(item.get("index", 0)))
    if not properties:
        raise RuntimeError(
            f"Google Sheet {spreadsheet_id} enthält kein verifizierbares Tabellenblatt."
        )

    ranges = []
    for item in properties:
        title = str(item.get("title") or "").strip()
        if not title:
            raise RuntimeError(
                f"Google Sheet {spreadsheet_id} enthält einen Tab ohne Namen."
            )
        escaped_title = title.replace("'", "''")
        ranges.append(f"'{escaped_title}'!A:ZZZ")

    response = sheets_service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=ranges,
        valueRenderOption="FORMULA",
        dateTimeRenderOption="SERIAL_NUMBER",
    ).execute()
    value_ranges = response.get("valueRanges")
    if not isinstance(value_ranges, list) or len(value_ranges) != len(properties):
        raise RuntimeError(
            f"Google Sheet {spreadsheet_id}: Zellwerte konnten nicht vollständig gelesen werden."
        )

    payload = []
    for item, value_range in zip(properties, value_ranges):
        if not isinstance(value_range, dict) or "values" not in value_range:
            # Empty worksheets are represented by an empty value array by the API.
            values = [] if isinstance(value_range, dict) else None
        else:
            values = value_range.get("values")
        if values is None or not isinstance(values, list):
            raise RuntimeError(
                f"Google Sheet {spreadsheet_id}: ungültige Zellwert-Antwort für Tab {item['title']!r}."
            )
        payload.append({"title": item["title"], "values": values})

    return sheet_values_sha256(payload)
