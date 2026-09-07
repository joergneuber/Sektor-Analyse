#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optionsschein-Kursaktualisierung für Offene_Positionen.xlsx

Ablauf:
    Offene_Positionen.xlsx
        -> WKN aus OS_WKN lesen
        -> Kurs bei Börse Stuttgart abrufen
        -> OS_Aktueller_Kurs aktualisieren
        -> OS_Performance% berechnen
        -> OS_Quelle und OS_Zeitstempel schreiben
        -> dieselbe XLSX-Datei atomar ersetzen

Aktives OS-Schema (bewusst nur diese sechs Spalten):
    OS_Einstiegskurs
    OS_Aktueller_Kurs
    OS_Performance%
    OS_Quelle
    OS_Zeitstempel
    OS_WKN

Wichtig:
- Die XLSX-Datei bleibt XLSX; es gibt keinen CSV-Zwischenschritt.
- Kurs- und Performancewerte werden als echte Excel-Zahlen gespeichert.
- OS_WKN, OS_Quelle und OS_Zeitstempel werden als Text behandelt.
- Die bestehende Excel-Darstellung der sechs OS-Spalten wird nicht durch
  allgemeine Tabellenformatierung ersetzt; für die drei Zahlenfelder werden
  die bereits im Bestand verwendeten Formate gezielt beibehalten/gesetzt.
- Bei fehlendem Kurs wird kein Ersatzkurs geschätzt. Der aktuelle Kurs und
  die Performance bleiben leer; OS_Quelle wird auf "nicht_verfügbar" gesetzt.
- Der Zeitstempel ist der lokale Zeitpunkt des Abrufs durch dieses Programm,
  nicht der historische Kurszeitpunkt der Börse Stuttgart.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from openpyxl import load_workbook


DEFAULT_XLSX = Path(__file__).resolve().parent / "Offene_Positionen.xlsx"
STUTTGART_URL = (
    "https://www.boerse-stuttgart.de/de-de/produkte/hebelprodukte/"
    "optionsscheine/stuttgart/{wkn}/"
)
REQUEST_TIMEOUT = 20
QUELLE = "Börse Stuttgart"
NICHT_VERFUEGBAR = "nicht_verfügbar"
SHEET_NAME = "Offene_Positionen"

OS_SPALTEN = [
    "OS_Einstiegskurs",
    "OS_Aktueller_Kurs",
    "OS_Performance%",
    "OS_Quelle",
    "OS_Zeitstempel",
    "OS_WKN",
]

OS_NUMMER_FORMAT = '0.00'
OS_PERFORMANCE_FORMAT = '0.00" %"'

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class OptionsscheinAbrufFehler(RuntimeError):
    """Fehler beim Abruf oder Parsen eines Optionsscheins."""


@dataclass(frozen=True)
class OptionsscheinKurs:
    wkn: str
    aktueller_kurs: float
    quelle: str = QUELLE
    url: str = ""


def _normalisiere_text(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _zahl_de(value: object) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace("\xa0", " ").replace(" ", "")
    s = re.sub(r"[^0-9,.+\-]", "", s)
    if not s:
        return None

    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        parts = s.split(".")
        s = "".join(parts[:-1]) + "." + parts[-1]

    try:
        number = float(s)
    except ValueError:
        return None
    return number if number >= 0 else None


def _find_exact_wkn(text: str, wkn: str) -> bool:
    return bool(re.search(rf"\bWKN\s*[:|]?\s*{re.escape(wkn)}\b", text, re.I))


def _find_geldkurs(text: str) -> Optional[float]:
    # Auf der Stuttgart-Seite steht der relevante Geldkurs im Kopfbereich
    # unmittelbar vor "Brief". Dadurch werden Zahlen anderer Kennzahlen
    # nicht versehentlich als Geldkurs interpretiert.
    patterns = [
        r"\bGeld\s+([0-9]+(?:[.,][0-9]+)?)\s+Brief\b",
        r"\bGeld\s*[:|]?\s*([0-9]+(?:[.,][0-9]+)?)\s+Brief\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = _zahl_de(match.group(1))
            if value is not None:
                return value
    return None


def _find_last_price(text: str) -> Optional[float]:
    # Der Abschnitt "Daten & Zahlen / Kursdaten" ist der eindeutige
    # Optionsschein-Kursbereich. "G" nach dem Preis wird bewusst erlaubt.
    patterns = [
        r"\bLetzter\s+Preis\s+([0-9]+(?:[.,][0-9]+)?)\s*(?:G|B)?\b",
        r"\bLetzter\s+Kurs\s+([0-9]+(?:[.,][0-9]+)?)\s*(?:G|B)?\b",
        r"\bLast\s+Price\s+([0-9]+(?:[.,][0-9]+)?)\s*(?:G|B)?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = _zahl_de(match.group(1))
            if value is not None:
                return value
    return None


def parse_stuttgart_html(html: str, wkn: str) -> OptionsscheinKurs:
    text = _normalisiere_text(html)
    if not _find_exact_wkn(text, wkn):
        raise OptionsscheinAbrufFehler(
            f"WKN {wkn} nicht als Instrument-WKN auf der Stuttgart-Seite gefunden."
        )

    geld = _find_geldkurs(text)
    if geld is not None:
        kurs = geld
    else:
        letzter = _find_last_price(text)
        if letzter is None:
            raise OptionsscheinAbrufFehler(
                f"Kein realer Kurs für {wkn} gefunden "
                f"(weder Geldkurs noch letzter Preis)."
            )
        kurs = letzter

    return OptionsscheinKurs(
        wkn=wkn,
        aktueller_kurs=kurs,
        quelle=QUELLE,
        url=STUTTGART_URL.format(wkn=wkn),
    )


def hole_optionsschein_kurs(wkn: str) -> OptionsscheinKurs:
    wkn = str(wkn).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", wkn):
        raise OptionsscheinAbrufFehler(
            f"Ungültige WKN {wkn!r}: exakt sechs alphanumerische Zeichen erwartet."
        )

    url = STUTTGART_URL.format(wkn=wkn)
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise OptionsscheinAbrufFehler(
            f"HTTP-Abruf für {wkn} fehlgeschlagen: {exc}"
        ) from exc

    return parse_stuttgart_html(response.text, wkn)


def _header_map(ws) -> dict[str, int]:
    result: dict[str, int] = {}
    for cell in ws[1]:
        if cell.value is not None:
            result[str(cell.value).strip()] = cell.column
    return result


def _require_schema(ws) -> dict[str, int]:
    columns = _header_map(ws)
    missing = [name for name in OS_SPALTEN + ["Produkt_Typ"] if name not in columns]
    if missing:
        raise ValueError("Fehlende Spalten: " + ", ".join(missing))
    return columns


def _cell_number(value: object) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _zahl_de(value)


def berechne_os_performance(einstieg: object, aktuell: object) -> Optional[float]:
    entry = _cell_number(einstieg)
    current = _cell_number(aktuell)
    if entry is None or current is None or entry <= 0:
        return None
    return ((current - entry) / entry) * 100.0


def _leere_automatische_felder(ws, row: int, col: dict[str, int]) -> None:
    for name in ("OS_Aktueller_Kurs", "OS_Performance%", "OS_Quelle", "OS_Zeitstempel"):
        ws.cell(row=row, column=col[name]).value = None


def _setze_zahlenformat(ws, row: int, col: dict[str, int]) -> None:
    ws.cell(row=row, column=col["OS_Einstiegskurs"]).number_format = OS_NUMMER_FORMAT
    ws.cell(row=row, column=col["OS_Aktueller_Kurs"]).number_format = OS_NUMMER_FORMAT
    ws.cell(row=row, column=col["OS_Performance%"]).number_format = OS_PERFORMANCE_FORMAT
    ws.cell(row=row, column=col["OS_WKN"]).number_format = "@"


def verarbeite_datei(xlsx_datei: Path) -> int:
    xlsx_datei = Path(xlsx_datei).resolve()
    if not xlsx_datei.exists():
        raise FileNotFoundError(f"Datei nicht gefunden: {xlsx_datei}")
    if xlsx_datei.suffix.lower() != ".xlsx":
        raise ValueError("Es wird ausdrücklich eine .xlsx-Datei erwartet.")

    wb = load_workbook(xlsx_datei, data_only=False)
    if SHEET_NAME in wb.sheetnames:
        ws = wb[SHEET_NAME]
    elif len(wb.worksheets) == 1:
        ws = wb.worksheets[0]
    else:
        raise ValueError(
            f"Arbeitsblatt {SHEET_NAME!r} nicht gefunden; mehrere Blätter vorhanden."
        )

    col = _require_schema(ws)
    os_anzahl = 0

    for row in range(2, ws.max_row + 1):
        produkt_typ = str(ws.cell(row=row, column=col["Produkt_Typ"]).value or "").strip().lower()
        if produkt_typ != "optionsschein":
            continue

        os_anzahl += 1
        _leere_automatische_felder(ws, row, col)
        _setze_zahlenformat(ws, row, col)

        wkn_cell = ws.cell(row=row, column=col["OS_WKN"])
        wkn = str(wkn_cell.value or "").strip().upper()
        if not wkn or wkn.lower() == "none":
            continue
        wkn_cell.value = wkn
        wkn_cell.number_format = "@"

        try:
            quote = hole_optionsschein_kurs(wkn)
            kurs_cell = ws.cell(row=row, column=col["OS_Aktueller_Kurs"])
            kurs_cell.value = float(quote.aktueller_kurs)
            kurs_cell.number_format = OS_NUMMER_FORMAT

            performance = berechne_os_performance(
                ws.cell(row=row, column=col["OS_Einstiegskurs"]).value,
                quote.aktueller_kurs,
            )
            if performance is not None:
                perf_cell = ws.cell(row=row, column=col["OS_Performance%"])
                perf_cell.value = float(performance)
                perf_cell.number_format = OS_PERFORMANCE_FORMAT

            ws.cell(row=row, column=col["OS_Quelle"]).value = quote.quelle
            ws.cell(row=row, column=col["OS_Zeitstempel"]).value = datetime.now().astimezone().isoformat(timespec="seconds")
            ws.cell(row=row, column=col["OS_Quelle"]).number_format = "@"
            ws.cell(row=row, column=col["OS_Zeitstempel"]).number_format = "@"

            print(
                f"[OK] Zeile {row} / {wkn}: "
                f"Kurs={quote.aktueller_kurs:.6g}, "
                f"Performance={performance:.4f}%" if performance is not None
                else f"[OK] Zeile {row} / {wkn}: Kurs={quote.aktueller_kurs:.6g}, Performance=-"
            )

        except OptionsscheinAbrufFehler as exc:
            ws.cell(row=row, column=col["OS_Quelle"]).value = NICHT_VERFUEGBAR
            ws.cell(row=row, column=col["OS_Zeitstempel"]).value = datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"[KEIN KURS] Zeile {row} / {wkn}: {exc}")

        except Exception as exc:
            ws.cell(row=row, column=col["OS_Quelle"]).value = NICHT_VERFUEGBAR
            ws.cell(row=row, column=col["OS_Zeitstempel"]).value = datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"[FEHLER] Zeile {row} / {wkn}: {type(exc).__name__}: {exc}")

    temp = xlsx_datei.with_name(xlsx_datei.name + ".tmp.xlsx")
    wb.save(temp)
    wb.close()
    temp.replace(xlsx_datei)
    return os_anzahl


def main() -> int:
    xlsx_datei = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_XLSX
    print(f"Datei: {xlsx_datei}")
    try:
        count = verarbeite_datei(xlsx_datei)
    except Exception as exc:
        print(f"[ABBRUCH] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Optionsschein-Zeilen verarbeitet: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
