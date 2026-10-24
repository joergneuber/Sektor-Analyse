"""Strict structural validation for the production Trading-Radar report."""
from __future__ import annotations

import re
import sys
from pathlib import Path

SECTION_NAMES = (
    "Trading-Radar",
    "Thesen-Update",
    "Ereignisradar",
    "Assetklassenvergleich",
    "Technische Kandidaten",
    "Portfolio & Risiko",
    "MSCI-Vergleich",
    "Daten-/Prüfanhang",
)

# Accept the legacy report's numbered dot and the specification's middle dot,
# but only as a numbered heading line (not an incidental body-text mention).
HEADER_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?P<number>[1-8])\s*[.·]\s*(?P<title>[^\r\n]+?)\s*$"
)



def _section_body(text: str, number: int, title: str) -> str:
    """Return the body of one canonical numbered section, bounded by the next heading."""
    headings = list(HEADER_RE.finditer(text))
    start = next((m for m in headings if int(m.group("number")) == number
                  and m.group("title").strip().casefold() == title.casefold()), None)
    if start is None:
        return ""
    end = next((m.start() for m in headings if m.start() > start.start()
                and int(m.group("number")) > number), len(text))
    return text[start.end():end]

def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns)

def _validate_m05_m10(text: str, errors: list[str]) -> None:
    """Check concrete M-05..M-10 report evidence; never infer missing facts as present."""
    # M-05: source-derived closed-position rows/count are checked above. A count is
    # not a license to require quantity/fees, which are outside this contract.

    # M-06: distinguish portfolio total performance from average position performance.
    portfolio = _section_body(text, 7, SECTION_NAMES[6])
    portfolio_total = _has_any(portfolio, (
        r"Portfolio(?:-Gesamt)?performance", r"Gesamtperformance.*Portfolio",
        r"Portfolio.*Performance.*(?:nicht verfügbar|nicht verifizierbar|fehl\w*|%|\d)"))
    position_average = _has_any(portfolio, (
        r"(?:Ø|durchschnitt(?:liche|s)?|mittelwert).*Positionsperformance",
        r"Positionsperformance.*(?:Ø|durchschnitt|mittelwert|%|nicht verfügbar|nicht verifizierbar)"))
    if not (portfolio_total and position_average):
        errors.append("M-06: Portfolio-Gesamtperformance und durchschnittliche Positionsperformance müssen getrennt ausgewiesen sein (oder jeweils ausdrücklich als nicht verfügbar).")

    # M-07: thesis update must preserve identity/version/status rather than prose only.
    thesis = _section_body(text, 2, SECTION_NAMES[1])
    thesis_identity = _has_any(thesis, (r"Thesen-ID\s*:", r"thesis_id\s*[:=]"))
    thesis_version = _has_any(thesis, (r"Version\s*:", r"version\s*[:=]"))
    thesis_status = _has_any(thesis, (r"Status\s*:", r"status\s*[:=]"))
    if not (thesis_identity and thesis_version and thesis_status):
        errors.append("M-07: Thesen-Update benötigt These-ID, Version und Status; Änderungen müssen nachvollziehbar sein.")

    # M-08: events need time zone, metric, reporting period and values when available.
    events = _section_body(text, 3, SECTION_NAMES[2])
    event_fields = (
        _has_any(events, (r"Uhrzeit\s*:", r"Zeit\s*:")),
        _has_any(events, (r"Zeitzone\s*:", r"\b(?:ET|UTC|CET|CEST|GMT)\b")),
        _has_any(events, (r"Kennzahl\s*:", r"Indikator\s*:")),
        _has_any(events, (r"Berichtszeitraum\s*:", r"Zeitraum\s*:")),
        _has_any(events, (r"(?:letzter Wert|Vorwert|Konsens|Istwert)\s*:", r"(?:letzter Wert|Vorwert|Konsens|Istwert)\s*(?:=|\b)")),
    )
    if not all(event_fields):
        errors.append("M-08: Ereignisdetails unvollständig; Uhrzeit, Zeitzone, Kennzahl, Zeitraum und verfügbare Werte/Konsens ausweisen.")

    # M-09: actionable candidate data, without accepting a generic mention as a setup.
    candidates = _section_body(text, 5, SECTION_NAMES[4])
    candidate_fields = (
        _has_any(candidates, (r"Ticker\s*:", r"Asset\s*:")),
        _has_any(candidates, (r"Trigger\s*:", r"Bestaetigung\s*:", r"Bestätigung\s*:")),
        _has_any(candidates, (r"Einstieg\s*:", r"Entry\s*:")),
        _has_any(candidates, (r"Stop\s*:", r"Stop-Loss\s*:")),
        _has_any(candidates, (r"TP1\s*:", r"Ziel 1\s*:")),
        _has_any(candidates, (r"TP2\s*:", r"Ziel 2\s*:")),
        _has_any(candidates, (r"CRV\s*:", r"Chance-Risiko")),
        _has_any(candidates, (r"Gegenargument\s*:", r"Risiko\s*:")),
        _has_any(candidates, (r"Regelstatus\s*:", r"Datenstatus\s*:")),
    )
    if not all(candidate_fields):
        errors.append("M-09: Technische Kandidaten benötigen Asset/Ticker, Trigger, Einstieg, Stop, TP1/TP2, CRV, Gegenargument und Regel-/Datenstatus.")

    # M-10: if OHLCV is missing/stale, explicitly describe its effect on candidates.
    appendix = _section_body(text, 8, SECTION_NAMES[7])
    warning = _has_any(appendix, (r"OHLCV.*(?:fehlt|fehlend|missing|luecke|lücke|nicht verfügbar|veraltet)",
                                  r"(?:fehlt|fehlend|missing|luecke|lücke|nicht verfügbar|veraltet).*OHLCV"))
    impact = _has_any(appendix, (r"Kandidat(?:en)?(?:.*)(?:eingeschränkt|nicht prüfbar|nicht valide|gesperrt)",
                                 r"(?:eingeschränkt|nicht prüfbar|nicht valide|gesperrt).*Kandidat(?:en)?"))
    if not (warning and impact):
        errors.append("M-10: OHLCV-Datenlücken/-veraltung und konkrete Auswirkung auf technische Kandidaten müssen im Prüfanhang stehen.")

def validate_report(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["Radar-Bericht ist leer"]

    matches = list(HEADER_RE.finditer(text))
    expected = list(SECTION_NAMES)
    found = [(int(m.group("number")), m.group("title").strip(), m.start(), m.end()) for m in matches]

    for number, title in enumerate(expected, start=1):
        matching = [item for item in found if item[0] == number and item[1].casefold() == title.casefold()]
        if len(matching) != 1:
            errors.append(f"Pflichtabschnitt {number} ({title}): erwartet genau einmal, gefunden {len(matching)}")

    valid = [item for item in found if item[1].casefold() in {x.casefold() for x in expected}]
    if len(valid) == 8 and [item[0] for item in valid] != list(range(1, 9)):
        errors.append("Trading-Radar-Abschnitte stehen nicht in Soll-Reihenfolge")

    # Check content only for correctly identified section headings. The body
    # ends at the next numbered expected heading; empty/placeholder bodies fail.
    for number, title, start, end in found:
        if number < 1 or number > 8 or title.casefold() != expected[number - 1].casefold():
            continue
        later = [item[2] for item in found if item[2] > start and item[0] in range(number + 1, 9)]
        body_end = min(later) if later else len(text)
        body = text[end:body_end].strip()
        body = re.sub(r"(?im)^TRADING_RADAR_FACHLICH_FINAL=.*$", "", body).strip()
        meaningful = re.sub(r"[\W_]", "", body, flags=re.UNICODE)
        if len(meaningful) < 20:
            errors.append(f"Pflichtabschnitt {number} ({title}): Inhalt fehlt/zu kurz")


    # Fachlich unvollständige Berichte dürfen den regulären Publikations-Gate
    # nicht passieren. NICHT_FINAL bleibt ein Fehler-/Teilbericht.
    status = re.findall(r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text)
    if len(status) != 1:
        errors.append("Fachstatusmarker fehlt oder ist nicht eindeutig")
    elif status[0] != "FINAL":
        errors.append("Fachstatus NICHT_FINAL: reguläre Veröffentlichung gesperrt")

    # Tab 2 muss im Bericht als deterministischer Quellenblock vorkommen und
    # die ausgewiesene Zeilenzahl muss mit den exportierten Datensätzen stimmen.
    closed = re.search(r"(?ms)^10\.5 Geschlossene Positionen[^\r\n]*\r?\n(.*?)(?=^\s*TRADING_RADAR_FACHLICH_FINAL=|\Z)", text)
    if not closed:
        errors.append("Geschlossene-Positionen-Abschnitt 10.5 fehlt")
    else:
        body = closed.group(1)
        count_match = re.search(r"(?m)^TAB-2-ANZAHL=(\d+)\s*$", body)
        rows = [line for line in body.splitlines() if re.search(r"(?m)(?:^|\s)Ticker:\s*\S+", line)]
        if not count_match:
            errors.append("Tab-2-Quellen-/Exportzählung fehlt")
        elif int(count_match.group(1)) != len(rows):
            errors.append(f"Tab-2-Zählung widerspricht Positionszeilen: gemeldet={count_match.group(1)}, erkannt={len(rows)}")
        if "TAB-2-QUELLE NICHT VERIFIZIERBAR" in body:
            errors.append("Tab-2-Quelle im Bericht als nicht verifizierbar markiert")

    if r"\n" in text:
        errors.append("Bericht enthält literal \\n statt physischer Zeilenumbrüche")

    _validate_m05_m10(text, errors)
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python trading_radar_report_gate.py REPORT.txt", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"REPORT_CHECK=FAIL | Bericht nicht lesbar: {exc}")
        return 1
    errors = validate_report(text)
    if errors:
        for error in errors:
            print(f"REPORT_CHECK=FAIL | {error}")
        return 1
    print("REPORT_CHECK=PASS | acht Pflichtabschnitte eindeutig, geordnet und mit Inhalt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
