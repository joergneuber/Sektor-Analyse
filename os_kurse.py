#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Produktive Optionsschein-Kursversorgung ueber die Boerse Stuttgart.

Die Funktionen dieses Moduls werden direkt vom Positions-Tracker verwendet.
Es gibt bewusst keine manuelle Kursquelle und keine Performance-Schaetzung ueber
Hebel oder Basiswertbewegung.

Produktives OS-Schema:
    OS_WKN
    OS_Einstiegskurs
    OS_Aktueller_Kurs
    OS_Performance%
    OS_Quelle
    OS_Kurszeit

Regel:
- Echter Kurs vorhanden -> OS_Aktueller_Kurs + OS_Quelle='Boerse Stuttgart'.
- Wenn die Quelle eine Kurszeit liefert, wird genau diese als OS_Kurszeit uebernommen.
- Kein Kurs -> aktueller Kurs, Performance und Kurszeit werden geleert;
  OS_Quelle='nicht_verfuegbar'.
- Kein alter/staler Kurs wird wiederverwendet.
- Performance wird ausschliesslich aus OS_Einstiegskurs und echtem OS-Aktueller_Kurs berechnet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

STUTTGART_URL = (
    "https://www.boerse-stuttgart.de/de-de/produkte/hebelprodukte/"
    "optionsscheine/stuttgart/{wkn}/"
)
REQUEST_TIMEOUT = 20
QUELLE = "Börse Stuttgart"
NICHT_VERFUEGBAR = "nicht_verfügbar"

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
    kurszeit: Optional[str]
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


def _find_kurszeit(text: str) -> Optional[str]:
    """Liest nur eine vom Quelltext gelieferte Kurszeit.

    Der lokale Abrufzeitpunkt wird absichtlich NICHT als Kurszeit verwendet.
    Falls die Stuttgart-Seite keine Kurszeit im gelieferten HTML enthaelt,
    bleibt OS_Kurszeit leer.
    """
    # Ausschliesslich das explizite Stuttgart-Feld "Kurszeit" verwenden.
    # Andere Datums-/Zeitangaben im HTML (z. B. "Stand" oder ISO-Zeitstempel)
    # sind keine nachgewiesene Kurszeit und werden daher bewusst ignoriert.
    pattern = (
        r"(?:Kurszeit|Kurs\s*zeit)\s*[:|]?\s*"
        r"(\d{1,2}\.\d{1,2}\.\d{4})\s*/?\s*"
        r"(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:Uhr)?"
    )
    match = re.search(pattern, text, re.I)
    if match:
        return f"{match.group(1).strip()} / {match.group(2).strip()}"
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
                f"Kein realer Kurs für {wkn} gefunden (weder Geldkurs noch letzter Preis)."
            )
        kurs = letzter
    return OptionsscheinKurs(
        wkn=wkn,
        aktueller_kurs=kurs,
        kurszeit=_find_kurszeit(text),
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
        raise OptionsscheinAbrufFehler(f"HTTP-Abruf für {wkn} fehlgeschlagen: {exc}") from exc
    return parse_stuttgart_html(response.text, wkn)


def berechne_os_performance(einstieg: object, aktuell: object) -> Optional[float]:
    try:
        if einstieg is None or aktuell is None or pd.isna(einstieg) or pd.isna(aktuell):
            return None
        entry = float(str(einstieg).replace(",", "."))
        current = float(str(aktuell).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    return round(((current - entry) / entry) * 100.0, 2)


def aktualisiere_optionsscheine(df: pd.DataFrame) -> pd.DataFrame:
    """Aktualisiert echte OS-Kurse für offene Optionsschein-Positionen."""
    required = [
        "Produkt_Typ", "Status", "OS_WKN", "OS_Einstiegskurs",
        "OS_Aktueller_Kurs", "OS_Performance%", "OS_Quelle", "OS_Kurszeit",
    ]
    for column in required:
        if column not in df.columns:
            df[column] = ""
    # Diese Felder werden im Lauf bewusst von Zahlen auf leere Werte und
    # zurueck wechseln koennen. Als object vermeiden wir pandas-Dtype-Warnungen
    # und halten leere Zellen im CSV/Google-Sheet wirklich leer.
    for column in ("OS_Aktueller_Kurs", "OS_Performance%", "OS_Quelle", "OS_Kurszeit"):
        df[column] = df[column].astype(object)

    for idx, row in df.iterrows():
        if str(row.get("Status", "")).strip().lower() != "offen":
            continue
        if str(row.get("Produkt_Typ", "")).strip().lower() != "optionsschein":
            continue

        # Alte/stale Automatikwerte vor jedem Abruf konsequent entfernen.
        df.at[idx, "OS_Aktueller_Kurs"] = ""
        df.at[idx, "OS_Performance%"] = ""
        df.at[idx, "OS_Kurszeit"] = ""
        df.at[idx, "OS_Quelle"] = NICHT_VERFUEGBAR

        wkn = str(row.get("OS_WKN", "")).strip().upper()
        ticker = str(row.get("Ticker", "")).strip()
        if not wkn or wkn.lower() == "nan":
            print(f"DEBUG: {ticker} -> Optionsschein ohne OS_WKN; kein Kurs verfügbar.")
            continue

        df.at[idx, "OS_WKN"] = wkn
        try:
            quote = hole_optionsschein_kurs(wkn)
        except OptionsscheinAbrufFehler as exc:
            print(f"DEBUG: {ticker} / {wkn} -> kein Stuttgart-Kurs: {exc}")
            continue
        except Exception as exc:
            print(f"DEBUG: {ticker} / {wkn} -> unerwarteter OS-Fehler: {type(exc).__name__}: {exc}")
            continue

        df.at[idx, "OS_Aktueller_Kurs"] = round(float(quote.aktueller_kurs), 6)
        df.at[idx, "OS_Quelle"] = QUELLE
        if quote.kurszeit:
            df.at[idx, "OS_Kurszeit"] = quote.kurszeit

        performance = berechne_os_performance(row.get("OS_Einstiegskurs"), quote.aktueller_kurs)
        if performance is not None:
            df.at[idx, "OS_Performance%"] = performance

        print(
            f"DEBUG: {ticker} / {wkn} -> OS-Kurs {quote.aktueller_kurs} | "
            f"Performance {performance if performance is not None else '-'}% | "
            f"Quelle {QUELLE} | Kurszeit {quote.kurszeit or '-'}"
        )

    return df
