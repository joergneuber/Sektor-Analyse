#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kontrollierte, manifestgesteuerte Wiederherstellung offener und geschlossener Positionen.

Die aktuellen Quelldaten enthalten 16 offene und 51 geschlossene Positionen.
Diese Zahlen sind Daten des konkreten Recovery-Pakets, keine Programmkonstanten:
künftige Wiederherstellungen können andere Anzahlen verwenden, wenn Manifest,
Quelldatei und private Historie gemeinsam und nachvollziehbar aktualisiert werden.

Vorhandene Historienzeilen werden niemals ersetzt oder gelöscht. Eine partielle
Historie darf nur ergänzt werden, wenn jeder vorhandene Datensatz zum verifizierten
privaten Ausgangsbestand gehört. Ein fehlender Historien-Tab bleibt ein harter Abbruch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable

import pandas as pd

import offene_positionen_check as opc


RECOVERY_MANIFEST_PATH = Path("recovery/recovery_manifest.json")
# Kompatibilitätsalias für bestehende Tests/Importe. Laufzeitwerte kommen aus dem Manifest.
OPEN_SOURCE_PATH = Path("recovery/Offene_Positionen_Quelle_16.csv")
SOURCE_DRIVE_NAMES = ("Offene_Positionen", "Offene_Positionen.csv")


def _key_rows_from_sheet(
    rows: list[list],
    *,
    headers: list[str],
    key_fn: Callable[[dict], tuple],
) -> tuple[list[tuple], int]:
    """Liest Tab-2-Schlüssel streng; erwartet Metazeile, Header, danach Daten."""
    if len(rows) < 2:
        raise RuntimeError("Sicherheitsabbruch: Historien-Tab hat keine vollständige Kopfstruktur.")
    actual_header = [str(value).strip() for value in rows[1]]
    if actual_header != headers:
        raise RuntimeError("Sicherheitsabbruch: Historien-Header stimmt nicht exakt.")
    keys: list[tuple] = []
    for raw in rows[2:]:
        if not any(str(value).strip() for value in raw):
            continue
        item = {column: (raw[index] if index < len(raw) else "") for index, column in enumerate(headers)}
        keys.append(key_fn(item))
    if len(keys) != len(set(keys)):
        raise RuntimeError("Sicherheitsabbruch: vorhandener Tab 2 enthält doppelte Positionsschlüssel.")
    return keys, len(keys)


def _canonical_recovery_key(key: tuple) -> tuple:
    """Canonicalize the date component of the established four-part trade key."""
    values = list(key)
    if len(values) >= 4:
        normalized_date = _normalize_history_value("Einstiegsdatum", values[3])
        if normalized_date[0] == "date":
            values[3] = normalized_date[1]
    return tuple(values)


def _normalize_history_value(column: str, value) -> tuple:
    """Normalize Sheet number/date rendering while comparing every source field."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ("empty", "")
    if column in opc.HISTORY_NUMERIC_COLUMNS:
        number = opc.parse_number(text)
        if number is not None:
            return ("number", round(float(number), 10))
    if column in {"Einstiegsdatum", "Ausstiegsdatum"}:
        parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
        if not pd.isna(parsed):
            return ("date", parsed.date().isoformat())
    return ("text", text)


def plan_history_recovery(
    existing_rows: list[list],
    trusted_history: pd.DataFrame,
    *,
    headers: list[str],
    key_fn: Callable[[dict], tuple],
    minimum_count: int | None = None,
) -> tuple[set[tuple], list[dict]]:
    """Ermittelt ausschließlich anzuhängende Zeilen; bestehende Zeilen bleiben unangetastet.

    Die erwartete Zahl stammt aus der verifizierten privaten Historie bzw. dem
    Recovery-Manifest. Der projektweite Mindestschutz von 51 geschlossenen
    Datensätzen bleibt bestehen; ein konkreter Recovery-Bestand darf größer sein.
    """
    trusted_count = 0 if trusted_history is None else len(trusted_history)
    required_count = trusted_count if minimum_count is None else int(minimum_count)
    required_count = max(required_count, opc.MINIMUM_PROTECTED_HISTORY_ROWS)
    if trusted_history is None or trusted_count != required_count:
        raise RuntimeError(
            f"Sicherheitsabbruch: privater Initialbestand muss exakt {required_count} "
            f"Datensätze enthalten (gefunden: {trusted_count})."
        )
    trusted_keys: set[tuple] = set()
    trusted_rows: list[dict] = []
    for _, row in trusted_history.iterrows():
        item = {column: row.get(column, "") for column in headers}
        key = _canonical_recovery_key(key_fn(item))
        if key in trusted_keys:
            raise RuntimeError("Sicherheitsabbruch: privater Initialbestand enthält doppelte Positionsschlüssel.")
        trusted_keys.add(key)
        trusted_rows.append(item)
    if len(trusted_keys) != required_count:
        raise RuntimeError("Sicherheitsabbruch: Initialbestand ist nicht vollständig eindeutig.")

    existing_keys_list, _ = _key_rows_from_sheet(
        existing_rows, headers=headers, key_fn=key_fn
    )
    existing_keys = {_canonical_recovery_key(key) for key in existing_keys_list}
    if len(existing_keys) != len(existing_keys_list):
        raise RuntimeError(
            "Sicherheitsabbruch: vorhandene Historie enthält doppelte Positionsschlüssel "
            "nach Normalisierung der Datumsdarstellung."
        )
    if not existing_keys.issubset(trusted_keys) and not trusted_keys.issubset(existing_keys):
        raise RuntimeError(
            "Sicherheitsabbruch: vorhandene Historie ist weder eine Teilmenge des "
            "verifizierten Ausgangsbestands noch enthält sie dessen vollständige "
            "Schlüssel. Keine automatische Zusammenführung."
        )

    # Schlüsselgleichheit beweist nicht die inhaltliche Integrität eines Trades.
    # Jeder bereits vorhandene Initialdatensatz muss deshalb über ALLE
    # HISTORY_HEADERS-Felder mit dem gehashten privaten Ausgangsbestand
    # übereinstimmen. Spätere, zusätzliche Trades bleiben davon unberührt.
    trusted_by_key = {_canonical_recovery_key(key_fn(item)): item for item in trusted_rows}
    existing_by_key: dict[tuple, dict] = {}
    for raw in existing_rows[2:]:
        if not any(str(value).strip() for value in raw):
            continue
        item = {
            column: (raw[index] if index < len(raw) else "")
            for index, column in enumerate(headers)
        }
        key = _canonical_recovery_key(key_fn(item))
        existing_by_key[key] = item

    mismatches = []
    for key in existing_keys & trusted_keys:
        trusted_item = trusted_by_key[key]
        existing_item = existing_by_key[key]
        differing = [
            column for column in headers
            if _normalize_history_value(column, existing_item.get(column, ""))
            != _normalize_history_value(column, trusted_item.get(column, ""))
        ]
        if differing:
            mismatches.append((key, differing))
    if mismatches:
        detail = "; ".join(
            f"{key!r}: {', '.join(columns)}" for key, columns in mismatches[:5]
        )
        raise RuntimeError(
            "Sicherheitsabbruch: vorhandene historische Datensätze weichen trotz "
            f"identischer Positionsschlüssel vom privaten Ausgangsbestand ab: {detail}"
        )

    missing = [item for item in trusted_rows if _canonical_recovery_key(key_fn(item)) not in existing_keys]
    return existing_keys, missing


def load_recovery_manifest(path: Path = RECOVERY_MANIFEST_PATH) -> dict:
    """Lädt und validiert die explizite, versionierte Recovery-Datenbeschreibung."""
    if not path.is_file():
        raise RuntimeError(f"Recovery-Manifest fehlt: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Recovery-Manifest ist kein gültiges JSON.") from exc

    if manifest.get("schema_version") != 1:
        raise RuntimeError("Recovery-Manifest besitzt eine unbekannte schema_version.")
    opened = manifest.get("open_positions") or {}
    closed = manifest.get("closed_positions") or {}
    source_name = str(opened.get("source_file") or "").strip()
    source_path = Path(source_name)
    if not source_name or source_path.is_absolute() or ".." in source_path.parts:
        raise RuntimeError("Recovery-Manifest enthält einen ungültigen offenen Quellpfad.")
    if source_path.parent != Path("recovery"):
        raise RuntimeError("Offene Recovery-Quelle muss direkt im Verzeichnis recovery/ liegen.")
    if str(closed.get("secret_env") or "") != opc.RECOVERY_HISTORY_ENV:
        raise RuntimeError("Recovery-Manifest verweist auf ein nicht zugelassenes Secret.")
    for section, label in ((opened, "open_positions"), (closed, "closed_positions")):
        count = section.get("expected_count")
        digest = str(section.get("sha256") or "").lower()
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise RuntimeError(f"Recovery-Manifest: {label}.expected_count muss eine positive Ganzzahl sein.")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"Recovery-Manifest: {label}.sha256 ist ungültig.")
    if closed["expected_count"] < opc.MINIMUM_PROTECTED_HISTORY_ROWS:
        raise RuntimeError(
            "Recovery-Manifest unterschreitet den projektweiten Mindestschutz "
            f"von {opc.MINIMUM_PROTECTED_HISTORY_ROWS} geschlossenen Positionen."
        )
    safety = manifest.get("safety") or {}
    if safety.get("minimum_closed_positions") != opc.MINIMUM_PROTECTED_HISTORY_ROWS:
        raise RuntimeError("Recovery-Manifest verändert unzulässig den projektweiten Historien-Mindestschutz.")
    if safety.get("require_unique_position_keys") is not True:
        raise RuntimeError("Recovery-Manifest darf die Eindeutigkeitsprüfung nicht deaktivieren.")
    if safety.get("require_exact_source_hashes") is not True:
        raise RuntimeError("Recovery-Manifest darf die Hashprüfung nicht deaktivieren.")
    if safety.get("append_only_history") is not True:
        raise RuntimeError("Recovery-Manifest darf die append-only Historienregel nicht deaktivieren.")
    return manifest


def validate_open_recovery_source(
    path: Path | None = None,
    manifest: dict | None = None,
) -> pd.DataFrame:
    """Validiert die offene Quelle anhand des expliziten Recovery-Manifests."""
    manifest = manifest or load_recovery_manifest()
    open_config = manifest["open_positions"]
    path = path or Path(open_config["source_file"])
    if not path.is_file():
        raise RuntimeError(f"Wiederherstellungsquelle fehlt: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != open_config["sha256"]:
        raise RuntimeError(
            "Sicherheitsabbruch: SHA-256 der offenen Wiederherstellungsquelle stimmt nicht."
        )
    frame = opc.read_positions(str(path))
    expected_count = open_config["expected_count"]
    if len(frame) != expected_count:
        raise RuntimeError(
            f"Sicherheitsabbruch: erwartet {expected_count} offene Positionen, gefunden {len(frame)}."
        )
    statuses = frame["Status"].astype(str).str.strip().str.lower()
    if not statuses.eq("offen").all():
        raise RuntimeError("Sicherheitsabbruch: Wiederherstellungsquelle enthält nicht ausschließlich offene Positionen.")
    required = {"Ticker", "Name", "Einstieg", "Einstiegsdatum"}
    if not required.issubset(frame.columns):
        raise RuntimeError("Sicherheitsabbruch: offene Wiederherstellungsquelle besitzt nicht alle Schlüsselspalten.")
    keys = [opc._position_key(row) for _, row in frame.iterrows()]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Sicherheitsabbruch: offene Wiederherstellungsquelle enthält doppelte Positionsschlüssel.")
    if frame["Ticker"].astype(str).str.strip().eq("").any():
        raise RuntimeError("Sicherheitsabbruch: offene Wiederherstellungsquelle enthält leere Ticker.")
    return frame


def _drive_files(drive, query: str) -> list[dict]:
    return drive.files().list(
        q=query, fields="files(id,name,mimeType,parents)"
    ).execute().get("files", [])


def _preflight_source_target(drive) -> None:
    query = (
        "(name='Offene_Positionen' or name='Offene_Positionen.csv') "
        f"and '{opc.FOLDER_ID}' in parents and trashed=false"
    )
    files = _drive_files(drive, query)
    # Jede Mehrfachtreffer-Situation ist mehrdeutig: insbesondere darf eine
    # Google-Tabelle nicht stillschweigend gegenüber einer gleichnamigen CSV
    # bevorzugt werden. Der MIME-Typ löst die fachliche Quellenkollision nicht.
    if len(files) > 1:
        raise RuntimeError(
            "Sicherheitsabbruch: mehrere mögliche Offene_Positionen-Quelldateien gefunden."
        )


def _preflight_and_recover_existing_master(drive, sheets, trusted_history: pd.DataFrame, expected_history_count: int) -> str | None:
    """Sichert und ergänzt einen vorhandenen Master ausschließlich append-only."""
    query = (
        f"name='{opc.DRIVE_NAME}' and '{opc.FOLDER_ID}' in parents "
        f"and trashed=false and mimeType='{opc.GOOGLE_SHEETS_MIME_TYPE}'"
    )
    masters = [
        item for item in _drive_files(drive, query)
        if item.get("mimeType") == opc.GOOGLE_SHEETS_MIME_TYPE
    ]
    if len(masters) > 1:
        raise RuntimeError("Sicherheitsabbruch: mehrere gleichnamige Positions-Master gefunden.")
    if not masters:
        print(
            "PREFLIGHT: Kein bestehender Positions-Master; neuer Master nur mit "
            f"verifizierter Historie ({expected_history_count} Datensätze) zulässig."
        )
        return None

    spreadsheet_id = masters[0]["id"]
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    titles = [
        str(item.get("properties", {}).get("title", "")).strip()
        for item in meta.get("sheets", [])
    ]
    if titles.count("Geschlossene Positionen") != 1:
        raise RuntimeError(
            "Sicherheitsabbruch: bestehender Master besitzt nicht genau einen Tab "
            "'Geschlossene Positionen'. Er wird nicht automatisch neu angelegt."
        )

    existing_rows = opc.read_existing_history(sheets, spreadsheet_id, allow_missing=False)
    existing_keys, missing = plan_history_recovery(
        existing_rows,
        trusted_history,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=expected_history_count,
    )
    print(
        f"PREFLIGHT: vorhandene geschlossene Positionen={len(existing_keys)} | "
        f"fehlende Initialpositionen={len(missing)}"
    )

    if missing:
        backup_id = opc._snapshot_google_sheet(drive, spreadsheet_id)
        if not backup_id:
            raise RuntimeError("Sicherheitsabbruch: Drive-Backup des bestehenden Masters wurde nicht bestätigt.")
        print(f"SICHERUNG: bestehender Master vor Historienergänzung kopiert | Backup-ID={backup_id}")
        appended = opc._append_missing_history_rows(
            sheets,
            spreadsheet_id,
            trusted_history,
            allow_initialize=True,
        )
        if appended != len(missing):
            raise RuntimeError(
                f"Sicherheitsabbruch: erwartete {len(missing)} Append-Zeilen, "
                f"tatsächlich gemeldet {appended}."
            )

        verified_rows = opc.read_existing_history(sheets, spreadsheet_id, allow_missing=False)
        verified_keys_list, verified_count = _key_rows_from_sheet(
            verified_rows, headers=opc.HISTORY_HEADERS, key_fn=opc._history_key
        )
        verified_keys = {_canonical_recovery_key(key) for key in verified_keys_list}
        trusted_keys = {
            _canonical_recovery_key(opc._history_key({column: row.get(column, "") for column in opc.HISTORY_HEADERS}))
            for _, row in trusted_history.iterrows()
        }
        if not trusted_keys.issubset(verified_keys):
            raise RuntimeError("Sicherheitsabbruch: nach Append fehlen weiterhin historische Initialschlüssel.")
        if not existing_keys.issubset(verified_keys):
            raise RuntimeError("Sicherheitsabbruch: vorbestehende historische Schlüssel gingen verloren.")
        if verified_count < expected_history_count:
            raise RuntimeError(
                f"Sicherheitsabbruch: Historie bleibt unter {expected_history_count} Datensätzen."
            )
        print(
            f"HISTORIE: append-only Wiederherstellung verifiziert | "
            f"vorher={len(existing_keys)} | angehängt={appended} | nachher={verified_count}"
        )
    else:
        print(f"HISTORIE: alle {expected_history_count} Initialschlüssel bereits vorhanden; keine Historienänderung nötig.")
    return spreadsheet_id



def _preflight_existing_master_read_only(drive, sheets, trusted_history: pd.DataFrame) -> None:
    """Prüft Drive-/Sheets-Zugriff und Masterzustand ohne Schreiboperationen."""
    query = (
        f"name='{opc.DRIVE_NAME}' and '{opc.FOLDER_ID}' in parents "
        f"and trashed=false and mimeType='{opc.GOOGLE_SHEETS_MIME_TYPE}'"
    )
    masters = [
        item for item in _drive_files(drive, query)
        if item.get("mimeType") == opc.GOOGLE_SHEETS_MIME_TYPE
    ]
    if len(masters) > 1:
        raise RuntimeError("PREFLIGHT-Abbruch: mehrere gleichnamige Positions-Master gefunden.")
    if not masters:
        print("PREFLIGHT: kein bestehender Master; Wiederherstellung würde einen neuen Master anlegen.")
        return

    spreadsheet_id = masters[0]["id"]
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    titles = [
        str(item.get("properties", {}).get("title", "")).strip()
        for item in meta.get("sheets", [])
    ]
    if titles.count("Geschlossene Positionen") != 1:
        raise RuntimeError(
            "PREFLIGHT-Abbruch: bestehender Master besitzt nicht genau einen Tab "
            "'Geschlossene Positionen'. Es wird nichts automatisch angelegt."
        )
    existing_rows = opc.read_existing_history(sheets, spreadsheet_id, allow_missing=False)
    existing_keys, missing = plan_history_recovery(
        existing_rows,
        trusted_history,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=len(trusted_history),
    )
    print(
        "PREFLIGHT: Sheets-Lesezugriff erfolgreich | "
        f"Master={spreadsheet_id} | vorhandene Historie={len(existing_keys)} | "
        f"fehlende Initialzeilen={len(missing)} | keine Schreibzugriffe ausgeführt."
    )


def preflight_only() -> None:
    """Validiert Recovery-Daten und Google-Zugriff vollständig read-only."""
    manifest = load_recovery_manifest()
    open_frame = validate_open_recovery_source(manifest=manifest)
    closed_config = manifest["closed_positions"]
    secret = os.getenv(closed_config["secret_env"], "").strip()
    if not secret:
        raise RuntimeError(f"Sicherheitsabbruch: Repository-Secret {closed_config['secret_env']} fehlt.")
    open_tickers = {
        str(value).strip().upper()
        for value in open_frame["Ticker"].tolist()
    }
    if open_tickers & {str(value).upper() for value in opc.GOLD_SPOT_TICKER_ALIASES}:
        if not os.getenv("TWELVEDATA_API_KEY", "").strip():
            raise RuntimeError(
                "PREFLIGHT-Abbruch: TWELVEDATA_API_KEY fehlt; die offene Recovery-Quelle "
                "enthält XAU/USD und benötigt diesen Datenzugang."
            )
    trusted_history = opc._load_private_recovery_history(
        secret,
        expected_sha256=closed_config["sha256"],
        minimum_rows=closed_config["expected_count"],
    )
    if len(trusted_history) != closed_config["expected_count"]:
        raise RuntimeError(
            "Sicherheitsabbruch: private Historienanzahl stimmt nicht mit dem Recovery-Manifest überein."
        )

    creds = opc.google_credentials()
    granted_scopes = set(
        getattr(creds, "scopes", None)
        or getattr(creds, "granted_scopes", None)
        or []
    )
    required_drive_scope = "https://www.googleapis.com/auth/drive"
    insufficient_drive_scope = "https://www.googleapis.com/auth/drive.file"
    required_sheets_scope = "https://www.googleapis.com/auth/spreadsheets"
    if required_drive_scope not in granted_scopes:
        hint = (
            " (drive.file allein genügt nicht für die vollständige Ordnerinventarisierung)"
            if insufficient_drive_scope in granted_scopes else ""
        )
        raise RuntimeError(
            "PREFLIGHT-Abbruch: GDRIVE_TOKEN benötigt den vollständigen "
            f"Google-Drive-Scope {required_drive_scope}.{hint}"
        )
    if granted_scopes and required_sheets_scope not in granted_scopes:
        raise RuntimeError("PREFLIGHT-Abbruch: GDRIVE_TOKEN besitzt keinen bestätigten Sheets-Schreibscope.")
    if not granted_scopes:
        raise RuntimeError(
            "PREFLIGHT-Abbruch: OAuth-Scopes von GDRIVE_TOKEN sind nicht auslesbar; "
            "Schreibberechtigungen können nicht sicher bestätigt werden."
        )

    drive = opc.build("drive", "v3", credentials=creds)
    sheets = opc.build("sheets", "v4", credentials=creds)
    folder_files = _drive_files(
        drive, f"'{opc.FOLDER_ID}' in parents and trashed=false"
    )
    _preflight_source_target(drive)
    _preflight_existing_master_read_only(drive, sheets, trusted_history)
    # Wenn noch kein Master existiert, trotzdem Sheets-API-Zugriff testen,
    # sofern sich bereits irgendein Spreadsheet im Recovery-Ordner befindet.
    any_spreadsheet = next(
        (item for item in folder_files if item.get("mimeType") == opc.GOOGLE_SHEETS_MIME_TYPE),
        None,
    )
    if any_spreadsheet:
        sheets.spreadsheets().get(
            spreadsheetId=any_spreadsheet["id"], fields="spreadsheetId"
        ).execute()
    print(
        "POSITIONS_RECOVERY_PREFLIGHT=SUCCESS | "
        f"offen={len(open_frame)} | geschlossen={len(trusted_history)} | "
        "Quellen, Hashes, Secrets und lesender Google-Zugriff geprüft; keine Schreibzugriffe."
    )

def _verify_master_open_tab(sheets, spreadsheet_id: str, expected_open: pd.DataFrame) -> None:
    meta = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    matching = [
        item.get("properties", {})
        for item in meta.get("sheets", [])
        if str(item.get("properties", {}).get("title", "")).strip() == "Offene Positionen+Check"
    ]
    if len(matching) != 1:
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: Tab 'Offene Positionen+Check' fehlt oder ist mehrfach vorhanden.")
    title = matching[0]["title"]
    rows = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{title}'!A:ZZZ",
        valueRenderOption="FORMATTED_VALUE",
    ).execute().get("values", [])
    if len(rows) < 2 or [str(value).strip() for value in rows[1]] != opc.HEADERS:
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: Header des offenen Positions-Tabs stimmt nicht.")
    data_rows = [row for row in rows[2:] if any(str(value).strip() for value in row)]
    actual = [
        {column: (raw[index] if index < len(raw) else "") for index, column in enumerate(opc.HEADERS)}
        for raw in data_rows
    ]
    # Nur stabile Stammdaten vergleichen; Kurs- und Analysefelder sind Laufzeitergebnisse.
    stable_columns = (
        "Ticker", "Name", "Sektor", "Markt", "Waehrung", "Einstiegsdatum", "Einstieg",
        "Status", "OS_WKN", "OS_Einstiegskurs", "OS_Quelle", "OS_Kurszeit",
    )
    expected = []
    for _, source_row in expected_open.iterrows():
        expected_row = {column: source_row.get(column, "") for column in stable_columns}
        ticker = str(source_row.get("Ticker", "")).strip().upper()
        if ticker == "PPFD.SG":
            expected_row["Sektor"] = "Edelmetalle / Silber"
        elif ticker == "GC=F":
            expected_row["Sektor"] = "Edelmetalle / Gold (Future)"
        elif ticker in {"XAU/USD", "XAUUSD"}:
            expected_row["Sektor"] = "Edelmetalle / Gold (Spot)"
        expected.append(expected_row)
    actual_stable = [
        {column: row.get(column, "") for column in stable_columns}
        for row in actual
    ]
    if len(actual_stable) != len(expected):
        raise RuntimeError(
            "LIVE-VERIFIKATION FEHLER: Anzahl offener Positionen stimmt nicht "
            f"mit der geprüften Quelle überein ({len(actual_stable)} != {len(expected)})."
        )
    for index, (actual_row, expected_row) in enumerate(zip(actual_stable, expected), start=1):
        differing = [
            column for column in stable_columns
            if _normalize_history_value(column, actual_row.get(column, ""))
            != _normalize_history_value(column, expected_row.get(column, ""))
        ]
        if differing:
            raise RuntimeError(
                "LIVE-VERIFIKATION FEHLER: offene Positionsstammdaten weichen "
                f"in Datensatz {index} ab: {', '.join(differing)}."
            )
    if any(str(row.get("Status", "")).strip().lower() != "offen" for row in actual):
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: offener Positions-Tab enthält einen nicht offenen Status.")


def main() -> None:
    manifest = load_recovery_manifest()
    open_config = manifest["open_positions"]
    closed_config = manifest["closed_positions"]
    expected_open_count = open_config["expected_count"]
    expected_history_count = closed_config["expected_count"]

    open_frame = validate_open_recovery_source(manifest=manifest)
    secret = os.getenv(closed_config["secret_env"], "").strip()
    if not secret:
        raise RuntimeError(
            f"Sicherheitsabbruch: Repository-Secret {closed_config['secret_env']} fehlt."
        )
    trusted_history = opc._load_private_recovery_history(
        secret,
        expected_sha256=closed_config["sha256"],
        minimum_rows=expected_history_count,
    )
    if len(trusted_history) != expected_history_count:
        raise RuntimeError(
            "Sicherheitsabbruch: private Historienanzahl stimmt nicht mit dem Recovery-Manifest überein."
        )

    # Recovery-CSV wird nur als lokale Laufzeitquelle verwendet.
    source_path = Path(open_config["source_file"])
    shutil.copyfile(source_path, opc.INPUT_FILE)
    source_frame = opc.read_positions(opc.INPUT_FILE)
    if (
        len(source_frame) != expected_open_count
        or not source_frame["Status"].astype(str).str.strip().str.lower().eq("offen").all()
    ):
        raise RuntimeError("Sicherheitsabbruch: Laufzeitquelle weicht vom Recovery-Manifest ab.")

    # Lokale technische Check-Ausgabe: bis hierhin keinerlei Drive-Schreibzugriff.
    output, combined_history = opc.run_local(opc.INPUT_FILE, opc.OUTPUT_CSV)
    if len(output) != expected_open_count:
        raise RuntimeError(
            f"Sicherheitsabbruch: Check-Ausgabe enthält {len(output)} statt "
            f"{expected_open_count} offener Positionen."
        )
    if not output["Status"].astype(str).str.strip().str.lower().eq("offen").all():
        raise RuntimeError("Sicherheitsabbruch: Check-Ausgabe enthält nicht ausschließlich offene Positionen.")
    if len(combined_history) != expected_history_count:
        raise RuntimeError(
            f"Sicherheitsabbruch: Check-Ausgabe enthält {len(combined_history)} statt "
            f"{expected_history_count} historischer Positionen."
        )

    expected_open_tickers = open_frame["Ticker"].astype(str).str.strip().tolist()
    actual_open_tickers = output["Ticker"].astype(str).str.strip().tolist()
    if actual_open_tickers != expected_open_tickers:
        raise RuntimeError(
            "Sicherheitsabbruch: Check-Ausgabe weicht in Anzahl/Reihenfolge der "
            f"{expected_open_count} offenen Ticker ab."
        )

    trusted_keys = {
        _canonical_recovery_key(opc._history_key({column: row.get(column, "") for column in opc.HISTORY_HEADERS}))
        for _, row in trusted_history.iterrows()
    }
    combined_keys = {
        _canonical_recovery_key(opc._history_key({column: row.get(column, "") for column in opc.HISTORY_HEADERS}))
        for _, row in combined_history.iterrows()
    }
    if len(trusted_keys) != expected_history_count or combined_keys != trusted_keys:
        raise RuntimeError("Sicherheitsabbruch: zusammengeführte Check-Historie weicht vom privaten Ausgangsbestand ab.")

    creds = opc.google_credentials()
    drive = opc.build("drive", "v3", credentials=creds)
    sheets = opc.build("sheets", "v4", credentials=creds)
    _preflight_source_target(drive)
    master_id = _preflight_and_recover_existing_master(
        drive, sheets, trusted_history, expected_history_count
    )

    result_master_id = opc.upsert_google_sheet(output, combined_history, creds)
    if not result_master_id:
        raise RuntimeError("Sicherheitsabbruch: Google-Sheets-Master wurde nicht bestätigt.")
    if master_id and result_master_id != master_id:
        raise RuntimeError("Sicherheitsabbruch: Upsert hat unerwartet eine andere Master-ID geliefert.")

    opc._sync_cleaned_source_to_drive(str(source_path), creds)

    final_rows = opc.read_existing_history(sheets, result_master_id, allow_missing=False)
    final_keys, final_count = _key_rows_from_sheet(
        final_rows, headers=opc.HISTORY_HEADERS, key_fn=opc._history_key
    )
    final_keys = [_canonical_recovery_key(key) for key in final_keys]
    if not trusted_keys.issubset(set(final_keys)) or final_count < expected_history_count:
        raise RuntimeError("LIVE-VERIFIKATION FEHLER: verifizierter Initialbestand fehlt nach Drive-Update.")
    plan_history_recovery(
        final_rows,
        trusted_history,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=expected_history_count,
    )
    _verify_master_open_tab(sheets, result_master_id, open_frame)

    print(
        "POSITIONS_WIEDERHERSTELLUNG=SUCCESS | "
        f"offen={len(output)} | geschlossen_minimum={final_count} | "
        f"master_id={result_master_id}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manifestgesteuerte Positionswiederherstellung")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Nur Daten, Secrets und Google-Lesezugriff prüfen; keine Schreiboperationen ausführen.",
    )
    args = parser.parse_args()
    if args.preflight_only:
        preflight_only()
    else:
        main()
