"""Automatischer Optionsschein-Kursabruf.

Primärquelle: Börse Stuttgart / EUWAX Produktseite.
Der Abruf liefert bevorzugt den Geldkurs (Verkaufssicht). Wenn kein Geldkurs
vorliegt, wird der letzte Kurs verwendet. Sind beide nicht verfügbar, gilt der
Abruf als fehlgeschlagen. Es wird dann bewusst kein Ersatzkurs eingetragen.

Wichtig: Diese Datei enthält keinen Zustand. Ein fehlgeschlagener Abruf liefert
None und darf niemals einen alten Kurs als aktuellen Kurs weiterverwenden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Optional

import requests
from lxml import html as lxml_html


STUTTGART_URL = (
    "https://www.boerse-stuttgart.de/de-de/produkte/hebelprodukte/"
    "optionsscheine/stuttgart/{wkn}/"
)


@dataclass(frozen=True)
class OptionsscheinKurs:
    wkn: str
    geld: Optional[float] = None
    brief: Optional[float] = None
    letzter_kurs: Optional[float] = None
    aktueller_kurs: Optional[float] = None
    spread: Optional[float] = None
    kurszeit: str = ""
    quelle: str = ""
    url: str = ""


class OptionsscheinAbrufFehler(RuntimeError):
    pass


def parse_deutsche_zahl(text: str) -> Optional[float]:
    """Parst deutsche Kursangaben: 2,20 / 1.234,56 / 2.20 / 1234.56."""
    if text is None:
        return None
    value = unescape(str(text)).strip()
    if not value or value in {"-", "–", "—"}:
        return None
    value = value.replace("€", "").replace("EUR", "").strip()
    value = re.sub(r"\s+", "", value)
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        result = float(value)
    except ValueError:
        return None
    return result


def _clean_text(page: str) -> str:
    try:
        tree = lxml_html.fromstring(page)
        text = tree.text_content()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _find_first_number_after(text: str, label: str, max_chars: int = 80) -> Optional[float]:
    pattern = re.compile(
        re.escape(label) + r"\s*([0-9][0-9.,]*|[-–—])",
        re.IGNORECASE,
    )
    match = pattern.search(text[:])
    if not match:
        return None
    return parse_deutsche_zahl(match.group(1))


def _find_quotes(text: str) -> tuple[Optional[float], Optional[float]]:
    # Produktkopf: Geld <kurs> Brief <kurs>. Das ist die bevorzugte Taxe.
    pattern = re.compile(
        r"\bGeld\s+([0-9][0-9.,]*|[-–—])\s+Brief\s+([0-9][0-9.,]*|[-–—])",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        geld = parse_deutsche_zahl(match.group(1))
        brief = parse_deutsche_zahl(match.group(2))
        if geld is not None or brief is not None:
            return geld, brief
    return None, None


def _find_last_price(text: str) -> Optional[float]:
    patterns = [
        r"\bLetzter Preis\s+([0-9][0-9.,]*)\s*[A-Z]?",
        r"\bLetzter Kurs\s+([0-9][0-9.,]*)\s*[A-Z]?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = parse_deutsche_zahl(match.group(1))
            if value is not None:
                return value
    return None


def _find_course_time(text: str) -> str:
    patterns = [
        r"Kurszeit\s+([^|]{6,50}?)(?=\s+(?:Tagesvolumen|Tageshoch|Vortageskurs|52-Wochen))",
        r"Kurszeit\s+([0-9]{1,2}\.\d{1,2}\.\d{4}\s*/\s*[^|]{4,20})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def parse_stuttgart_html(page: str, wkn: str, url: str = "") -> OptionsscheinKurs:
    text = _clean_text(page)
    normalized_wkn = wkn.strip().upper()
    if normalized_wkn not in text.upper():
        raise OptionsscheinAbrufFehler(f"WKN {normalized_wkn} nicht auf der Börse-Stuttgart-Seite gefunden")

    geld, brief = _find_quotes(text)
    letzter = _find_last_price(text)
    kurs = geld if geld is not None else letzter

    if kurs is None:
        raise OptionsscheinAbrufFehler(f"Kein Geld-/Letzter-Kurs für {normalized_wkn} gefunden")

    spread = None
    if geld is not None and brief is not None:
        spread = round(brief - geld, 10)

    return OptionsscheinKurs(
        wkn=normalized_wkn,
        geld=geld,
        brief=brief,
        letzter_kurs=letzter,
        aktueller_kurs=kurs,
        spread=spread,
        kurszeit=_find_course_time(text),
        quelle="boerse_stuttgart_geld" if geld is not None else "boerse_stuttgart_letzter",
        url=url,
    )


def hole_optionsschein_kurs(wkn: str, timeout: int = 15, session: Optional[requests.Session] = None) -> OptionsscheinKurs:
    normalized_wkn = str(wkn or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", normalized_wkn):
        raise OptionsscheinAbrufFehler(f"Ungültige WKN: {wkn!r}")

    url = STUTTGART_URL.format(wkn=normalized_wkn.lower())
    client = session or requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Neuber-OptionsscheinTracker/1.0)",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    }
    response = client.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return parse_stuttgart_html(response.text, normalized_wkn, response.url)


def berechne_os_performance(
    os_einstieg: Optional[float],
    automatischer_kurs: Optional[float],
) -> Optional[tuple[float, str]]:
    """Gibt (Performance%, Quelle) nach der festgelegten Priorität zurück."""
    if os_einstieg is None or os_einstieg <= 0:
        return None
    if automatischer_kurs is not None:
        return round(((automatischer_kurs - os_einstieg) / os_einstieg) * 100, 2), "automatisch"
    # Kein echter Kurs: bewusst keine Schätzung und keine Performance.
    return None
