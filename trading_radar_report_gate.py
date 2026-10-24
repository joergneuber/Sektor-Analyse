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
