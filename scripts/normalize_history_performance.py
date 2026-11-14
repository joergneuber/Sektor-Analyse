#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Explizite, einmalige und fail-closed Performance-Einheiten-Normalisierung.

Dieses Modul wird ausschließlich über einen ausdrücklich gestarteten
Normalisierungs-Workflow aufgerufen. Der Read-only-Preflight schreibt nichts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
from dataclasses import dataclass
from typing import Any

import pandas as pd

import offene_positionen_check as opc
from scripts.restore_positions import (
    _canonical_recovery_key,
    _normalize_history_value,
    load_recovery_manifest,
)

PERFORMANCE_COLUMN = "Performance_Seit_Einstieg%"
PERFORMANCE_INDEX = opc.HISTORY_HEADERS.index(PERFORMANCE_COLUMN)
SHEET_TITLE = "Geschlossene Positionen"
TOLERANCE = 0.000001

# Explicit one-time repair authorization for the six discrepancies identified
# by the read-only preflight on 2026-11-14. Keys use the canonical four-part
# trade identity; values are the exact currently observed Sheet values in
# percentage points. A repair is eligible only when every other history field
# matches the SHA-256-verified private baseline and the independently
# recalculated target is valid. Any different key/value remains fail-closed.
AUTHORIZED_PERFORMANCE_REPAIRS = {
    ("newmont corporation", "NEM", 120.0, "2026-05-12"): 9.87,
    ("linde plc", "LIN", 478.2, "2026-08-18"): -0.46,
    ("gold future", "GC=F", 4516.0, "2026-08-20"): 4.47,
    ("l'air liquide s.a.", "AI.PA", 179.0, "2026-07-03"): -4.63,
    ("mtu aero engines", "MTX.DE", 361.8, "2026-08-04"): -1.11,
    ("taiwan semiconductor manufacturing company limited", "TSM", 414.0, "2026-08-06"): -1.54,
}

if len(opc.HISTORY_HEADERS) != 26 or PERFORMANCE_INDEX != 16:
    raise RuntimeError(
        "Sicherheitsabbruch: Historien-Spaltenvertrag hat sich geändert; "
        "Performance_Seit_Einstieg% muss weiterhin in Spalte Q liegen."
    )


@dataclass
class NormalizationPlan:
    rows: list[list]
    trusted_by_key: dict[tuple, dict]
    expected_by_key: dict[tuple, float]
    existing_by_key: dict[tuple, tuple[int, dict]]
    ratio_keys: list[tuple]
    repair_keys: list[tuple]
    canonical_keys: list[tuple]
    missing_keys: list[tuple]
    extra_keys: list[tuple]


def _number(value: Any, field: str) -> float:
    parsed = opc.parse_number(value)
    if parsed is None:
        raise RuntimeError(f"Sicherheitsabbruch: {field} fehlt oder ist keine gültige Zahl.")
    result = float(parsed)
    if not pd.notna(result) or result != result or abs(result) == float("inf"):
        raise RuntimeError(f"Sicherheitsabbruch: {field} ist nicht endlich.")
    return result


def expected_performance_points(row: dict) -> float:
    """Unabhängige Long-/Short-Neuberechnung in Prozentpunkten."""
    entry = _number(row.get("Einstieg"), "Einstieg")
    close = _number(row.get("Ausstiegskurs"), "Ausstiegskurs")
    if entry <= 0 or close <= 0:
        raise RuntimeError("Sicherheitsabbruch: Einstieg und Ausstiegskurs müssen positiv sein.")
    direction = str(row.get("Richtung", "")).strip().lower()
    if direction not in {"long", "short"}:
        raise RuntimeError("Sicherheitsabbruch: Richtung muss exakt Long oder Short sein.")
    value = (close - entry) / entry * 100.0
    if direction == "short":
        value = -value
    return round(value, 2)


def classify_performance_value(stored_value: Any, expected_points: float) -> str:
    """Erkennt Einheiten durch Sollwertvergleich, niemals durch Betragsschwelle."""
    stored = _number(stored_value, PERFORMANCE_COLUMN)
    ratio_matches = abs(round(stored * 100.0, 2) - expected_points) <= TOLERANCE
    points_matches = abs(round(stored, 2) - expected_points) <= TOLERANCE
    if ratio_matches == points_matches:
        reason = "mehrdeutig" if ratio_matches else "weder Verhältniswert noch Prozentpunktwert"
        raise RuntimeError(
            f"Sicherheitsabbruch: Performance-Wert ist {reason} "
            f"(gespeichert={stored!r}, Soll={expected_points:.2f})."
        )
    return "ratio" if ratio_matches else "points"


def _row_dict(raw: list, headers: list[str]) -> dict:
    if len(raw) > len(headers) and any(str(x).strip() for x in raw[len(headers):]):
        raise RuntimeError("Sicherheitsabbruch: Historienzeile enthält unerwartete Zusatzspalten.")
    return {column: (raw[i] if i < len(raw) else "") for i, column in enumerate(headers)}


def build_normalization_plan(existing_rows: list[list], trusted_history: pd.DataFrame) -> NormalizationPlan:
    headers = opc.HISTORY_HEADERS
    if len(existing_rows) < 2 or [str(x).strip() for x in existing_rows[1]] != headers:
        raise RuntimeError("Sicherheitsabbruch: Historien-Header stimmt nicht exakt.")
    if trusted_history is None or len(trusted_history) < opc.MINIMUM_PROTECTED_HISTORY_ROWS:
        raise RuntimeError("Sicherheitsabbruch: geschützte Baseline unterschreitet 51 Datensätze.")

    trusted_by_key: dict[tuple, dict] = {}
    expected_by_key: dict[tuple, float] = {}
    validation_errors: list[str] = []
    for _, series in trusted_history.iterrows():
        item = {column: series.get(column, "") for column in headers}
        key = _canonical_recovery_key(opc._history_key(item))
        if key in trusted_by_key:
            raise RuntimeError("Sicherheitsabbruch: private Baseline enthält doppelte Positionsschlüssel.")
        trusted_by_key[key] = item
        try:
            expected = expected_performance_points(item)
            expected_by_key[key] = expected
            # The hashed private source is also checked against the independent
            # calculation; it is not trusted merely because its hash is valid.
            classify_performance_value(item.get(PERFORMANCE_COLUMN, ""), expected)
        except RuntimeError as exc:
            validation_errors.append(f"Baseline {key!r}: {exc}")

    existing_by_key: dict[tuple, tuple[int, dict]] = {}
    for sheet_row_number, raw in enumerate(existing_rows[2:], start=3):
        if not any(str(value).strip() for value in raw):
            continue
        item = _row_dict(raw, headers)
        key = _canonical_recovery_key(opc._history_key(item))
        if key in existing_by_key:
            raise RuntimeError("Sicherheitsabbruch: vorhandene Historie enthält doppelte Positionsschlüssel.")
        existing_by_key[key] = (sheet_row_number, item)

    ratio_keys: list[tuple] = []
    repair_keys: list[tuple] = []
    canonical_keys: list[tuple] = []
    for key in trusted_by_key.keys() & existing_by_key.keys():
        trusted_item = trusted_by_key[key]
        existing_item = existing_by_key[key][1]
        differing = [
            column for column in headers
            if column != PERFORMANCE_COLUMN
            and _normalize_history_value(column, existing_item.get(column, ""))
            != _normalize_history_value(column, trusted_item.get(column, ""))
        ]
        if differing:
            validation_errors.append(
                "Baseline-Datensatz weicht außerhalb der Performance-Spalte ab "
                f"({key!r}: {', '.join(differing)})"
            )
            continue
        if key not in expected_by_key:
            continue
        stored_value = existing_item.get(PERFORMANCE_COLUMN, "")
        try:
            classification = classify_performance_value(
                stored_value, expected_by_key[key]
            )
        except RuntimeError as exc:
            # Repair is deliberately narrower than general normalization:
            # only the six exact key/value pairs independently evidenced by
            # the 2026-11-14 read-only preflight may be corrected. The trusted
            # source itself was validated above against the same independent
            # calculation; all non-performance fields were compared already.
            authorized_old_value = AUTHORIZED_PERFORMANCE_REPAIRS.get(key)
            try:
                observed_value = _number(stored_value, PERFORMANCE_COLUMN)
            except RuntimeError:
                observed_value = None
            if (
                authorized_old_value is not None
                and observed_value is not None
                and abs(observed_value - authorized_old_value) <= TOLERANCE
            ):
                repair_keys.append(key)
                continue
            validation_errors.append(f"Performance {key!r}: {exc}")
            continue
        (ratio_keys if classification == "ratio" else canonical_keys).append(key)

    missing_keys = [key for key in trusted_by_key if key not in existing_by_key]
    extra_keys = [key for key in existing_by_key if key not in trusted_by_key]

    # A row whose key was accidentally altered must not silently be treated as
    # an unrelated later trade and duplicated by an append. If all non-identity,
    # non-performance fields still match a protected baseline row, the conflict
    # is ambiguous and therefore fail-closed.
    identity_fields = {"Name", "Ticker", "Einstieg", "Einstiegsdatum", PERFORMANCE_COLUMN}
    comparison_fields = [column for column in headers if column not in identity_fields]
    for extra_key in extra_keys:
        extra_item = existing_by_key[extra_key][1]
        for baseline_key, baseline_item in trusted_by_key.items():
            if all(
                _normalize_history_value(column, extra_item.get(column, ""))
                == _normalize_history_value(column, baseline_item.get(column, ""))
                for column in comparison_fields
            ):
                validation_errors.append(
                    "Unbekannter Historien-Schlüssel ähnelt einer geschützten Baseline "
                    "außerhalb der Identitätsfelder; mögliche Schlüsselbeschädigung "
                    f"({extra_key!r} gegenüber {baseline_key!r})"
                )
                break

    if validation_errors:
        details = " | ".join(validation_errors)
        raise RuntimeError(
            "Sicherheitsabbruch: Performance-Preflight hat "
            f"{len(validation_errors)} Fehler festgestellt: {details}"
        )

    return NormalizationPlan(
        rows=existing_rows,
        trusted_by_key=trusted_by_key,
        expected_by_key=expected_by_key,
        existing_by_key=existing_by_key,
        ratio_keys=sorted(ratio_keys, key=repr),
        repair_keys=sorted(repair_keys, key=repr),
        canonical_keys=sorted(canonical_keys, key=repr),
        missing_keys=sorted(missing_keys, key=repr),
        extra_keys=sorted(extra_keys, key=repr),
    )


def _load_trusted_history():
    manifest = load_recovery_manifest()
    config = manifest["closed_positions"]
    secret = os.getenv(config["secret_env"], "").strip()
    if not secret:
        raise RuntimeError(f"Sicherheitsabbruch: Repository-Secret {config['secret_env']} fehlt.")
    trusted = opc._load_private_recovery_history(
        secret,
        expected_sha256=config["sha256"],
        minimum_rows=config["expected_count"],
    )
    if len(trusted) != config["expected_count"]:
        raise RuntimeError("Sicherheitsabbruch: private Baseline-Anzahl stimmt nicht mit dem Manifest überein.")
    return trusted


def _get_master(drive):
    query = (
        f"name='{opc.DRIVE_NAME}' and '{opc.FOLDER_ID}' in parents "
        f"and trashed=false and mimeType='{opc.GOOGLE_SHEETS_MIME_TYPE}'"
    )
    files = drive.files().list(q=query, fields="files(id,name,mimeType,parents)").execute().get("files", [])
    if len(files) != 1:
        raise RuntimeError(
            "Sicherheitsabbruch: es muss exakt ein bestehender Positions-Master vorhanden sein "
            f"(gefunden: {len(files)})."
        )
    return files[0]


def _read_history(sheets, spreadsheet_id: str) -> list[list]:
    return opc.read_existing_history(sheets, spreadsheet_id, allow_missing=False)


def _print_report(plan: NormalizationPlan) -> None:
    print("PERFORMANCE_NORMALIZATION_PREFLIGHT")
    print(f"Baseline-Datensätze: {len(plan.trusted_by_key)}")
    print(f"Vorhandene Verhältniswerte: {len(plan.ratio_keys)}")
    print(f"Explizit autorisierte Performance-Korrekturen: {len(plan.repair_keys)}")
    print(f"Bereits kanonische Prozentpunktwerte: {len(plan.canonical_keys)}")
    print(f"Fehlende Baseline-Datensätze (append-only): {len(plan.missing_keys)}")
    print(f"Zusätzliche spätere Trades (unverändert): {len(plan.extra_keys)}")
    print("Fehler: 0")
    for key in plan.ratio_keys:
        print(f"UMZUWANDELN_RATIO: {key!r} -> {plan.expected_by_key[key]:.2f}")
    for key in plan.repair_keys:
        print(
            f"KORRIGIEREN_Q: {key!r} | vorher="
            f"{AUTHORIZED_PERFORMANCE_REPAIRS[key]:.2f} | "
            f"nachher={plan.expected_by_key[key]:.2f}"
        )
    for key in plan.missing_keys:
        print(f"APPEND_FEHLT: {key!r} -> {plan.expected_by_key[key]:.2f}")
    print("Keine Live-Werte außerhalb der ausgelesenen Sheet-Zeilen werden unterstellt.")


def _backup_and_verify(drive, spreadsheet_id: str) -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{opc.DRIVE_NAME} - Performance-Normalisierung Backup {stamp}-{secrets.token_hex(3)}"
    copied = drive.files().copy(
        fileId=spreadsheet_id,
        body={"name": name, "parents": [opc.FOLDER_ID]},
    ).execute()
    backup_id = str(copied.get("id") or "").strip()
    if not backup_id or backup_id == spreadsheet_id:
        raise RuntimeError("Sicherheitsabbruch: Drive-Backup lieferte keine eindeutige Backup-ID.")
    verified = drive.files().get(
        fileId=backup_id, fields="id,name,mimeType,trashed,parents"
    ).execute()
    if (
        verified.get("id") != backup_id
        or verified.get("name") != name
        or verified.get("mimeType") != opc.GOOGLE_SHEETS_MIME_TYPE
        or verified.get("trashed") is True
        or opc.FOLDER_ID not in (verified.get("parents") or [])
    ):
        raise RuntimeError("Sicherheitsabbruch: Drive-Backup konnte nicht vollständig verifiziert werden.")
    print(f"BACKUP_VERIFIED | id={backup_id} | name={name}")
    return verified


def _sheet_id(sheets, spreadsheet_id: str) -> int:
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    matches = [
        item.get("properties", {})
        for item in meta.get("sheets", [])
        if str(item.get("properties", {}).get("title", "")).strip() == SHEET_TITLE
    ]
    if len(matches) != 1:
        raise RuntimeError("Sicherheitsabbruch: Historien-Tab fehlt oder ist mehrfach vorhanden.")
    return int(matches[0]["sheetId"])


def _append_missing(sheets, spreadsheet_id: str, plan: NormalizationPlan) -> int:
    if not plan.missing_keys:
        return 0
    values = []
    for key in plan.missing_keys:
        item = dict(plan.trusted_by_key[key])
        item[PERFORMANCE_COLUMN] = plan.expected_by_key[key]
        values.append([item.get(column, "") for column in opc.HISTORY_HEADERS])
    payload = opc._canonicalize_history_values(
        [[""] * len(opc.HISTORY_HEADERS), opc.HISTORY_HEADERS] + values
    )[2:]
    response = sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_TITLE}'!A:Z",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": payload},
    ).execute()
    updated = int(response.get("updates", {}).get("updatedRows", 0))
    if updated != len(payload):
        raise RuntimeError(
            f"Sicherheitsabbruch: Append unvollständig ({updated} von {len(payload)} Zeilen). Backup bleibt erhalten."
        )
    return updated


def _write_performance_cells(sheets, spreadsheet_id: str, plan: NormalizationPlan) -> int:
    keys_to_write = sorted(set(plan.ratio_keys) | set(plan.repair_keys), key=repr)
    if not keys_to_write:
        return 0
    data = []
    for key in keys_to_write:
        row_number = plan.existing_by_key[key][0]
        data.append({
            "range": f"'{SHEET_TITLE}'!Q{row_number}",
            "values": [[plan.expected_by_key[key]]],
        })
    response = sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    updated = int(response.get("totalUpdatedCells", 0))
    if updated != len(data):
        raise RuntimeError(
            f"Sicherheitsabbruch: Performance-Zellupdate unvollständig ({updated} von {len(data)}). Backup bleibt erhalten."
        )
    return updated


def _pad(row: list, width: int) -> list:
    return list(row[:width]) + [""] * max(0, width - len(row))


def verify_post_write(
    before_rows: list[list],
    after_rows: list[list],
    plan: NormalizationPlan,
) -> None:
    headers = opc.HISTORY_HEADERS
    if len(after_rows) < 2 or [str(x).strip() for x in after_rows[1]] != headers:
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: Historien-Header wurde verändert.")
    if len(after_rows) != len(before_rows) + len(plan.missing_keys):
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: unerwartete Änderung der Historienzeilenanzahl.")
    # Every pre-existing row must remain byte/value-identical outside Q.
    performance_row_numbers = {
        plan.existing_by_key[key][0]
        for key in set(plan.ratio_keys) | set(plan.repair_keys)
    }
    for index, before in enumerate(before_rows):
        after = after_rows[index]
        before_padded = _pad(before, len(headers))
        after_padded = _pad(after, len(headers))
        if index >= 2 and index + 1 in performance_row_numbers:
            for col in range(len(headers)):
                if col != PERFORMANCE_INDEX and before_padded[col] != after_padded[col]:
                    raise RuntimeError(
                        f"LIVE-VERIFIKATION FEHLER: bestehende Zelle außerhalb Q geändert "
                        f"(Sheet-Zeile {index + 1}, Spalte {col + 1})."
                    )
        elif before_padded != after_padded:
            raise RuntimeError(
                f"LIVE-VERIFIKATION FEHLER: bestehende Zeile {index + 1} wurde außerhalb des erlaubten Bereichs geändert."
            )

    after_plan = build_normalization_plan(after_rows, pd.DataFrame(
        [plan.trusted_by_key[key] for key in plan.trusted_by_key],
        columns=headers,
    ))
    if after_plan.ratio_keys or after_plan.repair_keys or after_plan.missing_keys:
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: nicht alle Baseline-Werte sind kanonisch vorhanden.")
    if len(after_plan.canonical_keys) != len(plan.trusted_by_key):
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: Baseline-Anzahl/Identität stimmt nicht.")
    # Appended baseline rows must match all 26 expected fields, with canonical Q.
    for key in plan.missing_keys:
        actual = after_plan.existing_by_key[key][1]
        expected = dict(plan.trusted_by_key[key])
        expected[PERFORMANCE_COLUMN] = plan.expected_by_key[key]
        differing = [
            col for col in headers
            if _normalize_history_value(col, actual.get(col, ""))
            != _normalize_history_value(col, expected.get(col, ""))
        ]
        if differing:
            raise RuntimeError(
                f"LIVE-VERIFIKATION FEHLER: appendete Baseline-Zeile weicht ab ({key!r}: {', '.join(differing)})."
            )


def run(*, migrate: bool) -> None:
    trusted = _load_trusted_history()
    creds = opc.google_credentials()
    scopes = set(getattr(creds, "scopes", None) or getattr(creds, "granted_scopes", None) or [])
    if "https://www.googleapis.com/auth/drive" not in scopes:
        raise RuntimeError("Sicherheitsabbruch: vollständiger Google-Drive-Scope fehlt.")
    drive = opc.build("drive", "v3", credentials=creds)
    sheets = opc.build("sheets", "v4", credentials=creds)
    master = _get_master(drive)
    spreadsheet_id = master["id"]
    _sheet_id(sheets, spreadsheet_id)
    before_rows = _read_history(sheets, spreadsheet_id)
    plan = build_normalization_plan(before_rows, trusted)
    _print_report(plan)
    if not migrate:
        print("PERFORMANCE_NORMALIZATION_PREFLIGHT=SUCCESS | read-only; keine Schreibzugriffe.")
        return
    if not plan.ratio_keys and not plan.repair_keys and not plan.missing_keys:
        print("PERFORMANCE_NORMALIZATION=ALREADY_CANONICAL | keine Schreibzugriffe erforderlich.")
        return
    _backup_and_verify(drive, spreadsheet_id)
    # Each operation is narrowly scoped: Q-cells for existing baseline rows,
    # then append-only A:Z rows for missing baseline records.
    updated_cells = _write_performance_cells(sheets, spreadsheet_id, plan)
    appended_rows = _append_missing(sheets, spreadsheet_id, plan)
    after_rows = _read_history(sheets, spreadsheet_id)
    verify_post_write(before_rows, after_rows, plan)
    print(
        "PERFORMANCE_NORMALIZATION=SUCCESS | "
        f"performance_cells_updated={updated_cells} | repaired_cells={len(plan.repair_keys)} | "
        f"appended_rows={appended_rows} | "
        "all baseline rows verified; backup retained."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Einmalige Performance-Einheiten-Normalisierung")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    run(migrate=args.migrate)


if __name__ == "__main__":
    main()
