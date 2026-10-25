"""Strict structural validation for the production Trading-Radar report."""
from __future__ import annotations

import re
import sys
import hashlib
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

_MISSING_VALUE_RE = re.compile(
    r"^(?:|[-–—]|n/?a|na|tbd|todo|xxx|placeholder|platzhalter|unbekannt|"
    r"nicht\s+(?:vorhanden|verfügbar|verifizierbar|bekannt)|fehlt|unklar)$",
    re.IGNORECASE,
)


def _label_value(text: str, labels: tuple[str, ...]) -> str | None:
    """Extract a labeled value without accepting an empty/placeholder value.

    Values may be inline (separated by | or ;) or on the following line.
    The extraction is intentionally conservative: a label without a value is
    not evidence that the field is present.
    """
    label = r"(?:" + "|".join(labels) + r")"
    match = re.search(
        rf"(?im)(?:^|[|;]\s*|\n)\s*{label}\s*[:=]\s*(?:([^|;\r\n]+)|(?:\r?\n\s*([^|;\r\n]+)))",
        text,
    )
    if not match:
        return None
    value = (match.group(1) or match.group(2) or "").strip().strip(". ")
    if _MISSING_VALUE_RE.fullmatch(value):
        return None
    return value


def _is_explicit_unavailable(text: str, labels: tuple[str, ...]) -> bool:
    """Allow an explicit unavailable state, but not a blank or vague mention."""
    label = r"(?:" + "|".join(labels) + r")"
    return bool(re.search(
        rf"(?i)(?:{label})\s*(?:[:=]\s*)?(?:nicht\s+(?:verfügbar|verifizierbar|vorhanden)|fehlt|unklar|FEHLT)\b",
        text,
    ))


def _valid_numeric_value(value: str | None) -> bool:
    """Accept a complete numeric field, optionally with a known unit/currency.

    Do not search for a number inside arbitrary prose (e.g. ``bald 100``).
    Decimal comma and common thousands separators are supported.
    """
    if value is None:
        return False
    cleaned = value.strip()
    if not cleaned or _MISSING_VALUE_RE.fullmatch(cleaned):
        return False

    # Explicitly supported wrappers; the numeric core must occupy the entire
    # remaining field. Units are deliberately limited to common price/ratio
    # labels rather than accepting arbitrary trailing prose.
    cleaned = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*(?:EUR|USD|GBP|€|\$|£|%|x|\b(?:Punkte|pts)\b)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    if not re.fullmatch(r"[-+]?(?:\d{1,3}(?:[ .’']\d{3})+|\d+)(?:[,.]\d+)?", cleaned):
        return False

    normalized = cleaned.replace(" ", "").replace("’", "").replace("'", "")
    # A dot/comma followed by exactly three digits is treated as a grouping
    # separator when another grouping separator exists; otherwise it is the
    # decimal separator. This handles common German and English report values.
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        number = float(normalized)
        return number == number and abs(number) != float("inf")
    except (TypeError, ValueError):
        return False


def _validate_m05_m10(text: str, errors: list[str]) -> None:
    """Check concrete M-05..M-10 report evidence; reject blank placeholders."""
    # M-05: the deterministic Tab-2 source block and row/count comparison are
    # checked in validate_report. Quantity and fees are not contract fields.

    # M-06: both values must be explicit and independently labeled. Either may
    # be a numeric value or an explicit unavailable state.
    benchmark = _section_body(text, 7, SECTION_NAMES[6])
    portfolio_labels = (r"Portfolio(?:-Gesamt)?performance", r"Gesamtperformance\s+Portfolio")
    average_labels = (r"(?:Ø|durchschnitt(?:liche|s)?|mittelwert)\s*Positionsperformance",
                      r"Positionsperformance(?:\s*\(?(?:Ø|Durchschnitt|Mittelwert)\)?\))?")
    portfolio_value = _label_value(benchmark, portfolio_labels)
    average_value = _label_value(benchmark, average_labels)
    portfolio_ok = _valid_numeric_value(portfolio_value) or _is_explicit_unavailable(benchmark, portfolio_labels)
    average_ok = _valid_numeric_value(average_value) or _is_explicit_unavailable(benchmark, average_labels)
    # Preserve compatibility with the report's established explicit prose form.
    portfolio_ok = portfolio_ok or _has_any(benchmark, (r"Portfolio-Gesamtperformance\s+(?:nicht verfügbar|nicht verifizierbar)",))
    average_ok = average_ok or _has_any(benchmark, (r"Positionsperformance\s+(?:nicht verfügbar|nicht verifizierbar)",))
    if not (portfolio_ok and average_ok):
        errors.append("M-06: Portfolio-Gesamtperformance und durchschnittliche Positionsperformance müssen jeweils einen konkreten Zahlenwert oder einen ausdrücklichen Nichtverfügbarkeitsstatus haben.")

    # M-07: identity/version/status values must be populated, not merely labels.
    thesis = _section_body(text, 2, SECTION_NAMES[1])
    thesis_id = _label_value(thesis, (r"Thesen-ID", r"thesis_id"))
    version = _label_value(thesis, (r"Version", r"version"))
    status = _label_value(thesis, (r"Status", r"status"))
    version_match = re.search(r"\d+", version or "")
    status_ok = bool(status and re.search(
        r"(?i)\b(?:NEU|BESTÄTIGT|ABGESCHWÄCHT|WARTET AUF EREIGNIS|INVALIDIERT|ERSETZT|FEHLT|UNKLAR|NICHT VERFÜGBAR)\b",
        status,
    ))
    no_thesis_update = _has_any(thesis, (
        r"keine neue oder aktualisierte these;?\s*thesenregister geprüft, keine änderung",
        r"keine neue oder aktualisierte these;?\s*thesenregister geprueft, keine aenderung",
    ))
    if not no_thesis_update and not (
        thesis_id and version_match and int(version_match.group(0)) >= 1 and status_ok
    ):
        errors.append("M-07: Thesen-Update benötigt Thesen-ID, positive Version und zulässigen Status oder den expliziten Nachweis, dass keine These aktualisiert wurde.")

    # M-08: every field needs a non-placeholder value. Explicit FEHLT/UNKLAR
    # remains visible as missing data, rather than being mistaken for a value.
    events = _section_body(text, 3, SECTION_NAMES[2])
    event_labels = (
        (r"Uhrzeit", r"Zeit"),
        (r"Zeitzone",),
        (r"Kennzahl", r"Indikator"),
        (r"Berichtszeitraum", r"Zeitraum"),
    )
    event_values = [_label_value(events, labels) for labels in event_labels]
    timezone_value = event_values[1]
    # Accept common named zones, UTC offsets, and IANA-style region names.
    timezone_ok = bool(timezone_value and re.search(
        r"(?i)(?:\b(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|UTC|CET|CEST|GMT)\b|"
        r"\b(?:UTC|GMT)?[+-]\d{1,2}(?::?\d{2})?\b|"
        r"\b[A-Za-z_]+/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?\b)", timezone_value))
    values_ok = all(event_values[i] for i in (0, 2, 3)) and timezone_ok
    # Observation may be on the same line or a subsequent line. Require a
    # non-placeholder value after the observation label.
    event_observation = any(_label_value(events, (label,)) for label in
                            (r"letzter Wert", r"Vorwert", r"Konsens", r"Istwert")) or _has_any(events, (
        r"(?:letzter Wert|Vorwert|Konsens|Istwert)\s+(?:nicht verfügbar|FEHLT|UNKLAR)",
    ))
    no_relevant_events = _has_any(events, (
        r"keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?Ereignisse",
        r"Ereignisradar\s*:\s*keine",
    ))
    if not no_relevant_events and not (values_ok and event_observation):
        errors.append("M-08: Ereignisdetails benötigen konkrete Uhrzeit, Zeitzone, Kennzahl, Zeitraum und einen ausgewiesenen Wert/Konsens oder expliziten Fehlstatus; alternativ muss ausdrücklich festgehalten sein, dass keine relevanten Ereignisse vorliegen.")

    # M-09: candidate labels need usable values. Numeric fields must contain
    # parseable numbers; explicit FEHLT is accepted only as a declared absence,
    # and does not silently become a numeric trade level.
    candidates = _section_body(text, 5, SECTION_NAMES[4])
    asset = _label_value(candidates, (r"Ticker", r"Asset"))
    trigger = _label_value(candidates, (r"Trigger", r"Bestätigung", r"Bestaetigung"))
    entry = _label_value(candidates, (r"Einstieg", r"Entry"))
    stop = _label_value(candidates, (r"Stop", r"Stop-Loss"))
    tp1 = _label_value(candidates, (r"TP1", r"Ziel\s*1"))
    tp2 = _label_value(candidates, (r"TP2", r"Ziel\s*2"))
    crv = _label_value(candidates, (r"CRV", r"Chance-Risiko"))
    counter = _label_value(candidates, (r"Gegenargument", r"Risiko"))
    rule_data = _label_value(candidates, (r"Regelstatus", r"Datenstatus"))
    candidate_required = (asset, trigger, entry, stop, tp1, tp2, crv, counter, rule_data)
    numeric_fields_ok = all(_valid_numeric_value(value) for value in (entry, stop, tp1, tp2, crv))
    no_actionable_candidates = _has_any(candidates, (
        r"keine\s+(?:belastbaren\s+)?(?:technischen\s+)?Kandidaten",
        r"keine\s+Kandidaten\s+(?:mit\s+)?(?:belastbarem|konkretem)\s+Setup",
    r"kein\s+belastbarer\s+technischer\s+Kandidat",
    ))
    allowed_candidate_status = (
        r"JETZT TECHNISCH PRÜFEN|AUF TRIGGER WARTEN|EREIGNIS ABWARTEN|"
        r"NICHT HANDELBAR|VERWERFEN\s*/\s*INVALIDIERT"
    )
    candidate_status_ok = bool(re.search(allowed_candidate_status, candidates, re.IGNORECASE))
    if not no_actionable_candidates and (not all(candidate_required) or not numeric_fields_ok or not candidate_status_ok):
        errors.append("M-09: Technische Kandidaten benötigen Asset/Ticker, Trigger, Einstieg, Stop, TP1/TP2, CRV, Gegenargument, Regel-/Datenstatus und einen zulässigen Kandidatenstatus; Zahlenfelder müssen numerisch sein. Alternativ muss ausdrücklich ausgewiesen sein, dass keine belastbaren Kandidaten vorliegen.")

    # M-10: a concrete OHLCV issue and its candidate impact must both be stated.
    appendix = _section_body(text, 8, SECTION_NAMES[7])
    warning = _has_any(appendix, (
        r"OHLCV.*(?:fehlt|fehlend|missing|luecke|lücke|nicht verfügbar|veraltet)",
        r"(?:fehlt|fehlend|missing|luecke|lücke|nicht verfügbar|veraltet).*OHLCV",
    ))
    impact = _has_any(appendix, (
        r"Kandidat(?:en)?(?:[^\r\n]{0,100})(?:eingeschränkt|nicht prüfbar|nicht valide|gesperrt)",
        r"(?:eingeschränkt|nicht prüfbar|nicht valide|gesperrt)(?:[^\r\n]{0,100})Kandidat(?:en)?",
    ))
    clean_data = _has_any(appendix, (
        r"OHLCV[^\r\n]{0,100}(?:vollständig|vollstaendig|keine\s+(?:Daten)?lücken|keine\s+fehlenden\s+Daten)",
        r"(?:vollständig|vollstaendig|keine\s+(?:Daten)?lücken|keine\s+fehlenden\s+Daten)[^\r\n]{0,100}OHLCV",
    )) and _has_any(appendix, (
        r"Kandidat(?:en)?[^\r\n]{0,100}(?:nicht eingeschränkt|keine Einschränkung|unverändert valide)",
        r"(?:nicht eingeschränkt|keine Einschränkung|unverändert valide)[^\r\n]{0,100}Kandidat(?:en)?",
    ))
    if not ((warning and impact) or clean_data):
        errors.append("M-10: OHLCV-Datenlücke/-veraltung und konkrete Auswirkung auf Kandidaten müssen ausgewiesen sein; bei lückenfreien Daten sind vollständige OHLCV-Abdeckung und fehlende Kandidateneinschränkung ausdrücklich zu nennen.")

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
        ticker_values = [
            match.group(1).strip()
            for line in body.splitlines()
            if (match := re.search(r"(?:^|\s)Ticker:\s*([^|\r\n]+)", line))
        ]
        rows = ticker_values
        if not count_match:
            errors.append("Tab-2-Quellen-/Exportzählung fehlt")
        elif int(count_match.group(1)) != len(rows):
            errors.append(f"Tab-2-Zählung widerspricht Positionszeilen: gemeldet={count_match.group(1)}, erkannt={len(rows)}")
        position_lines = [
            re.sub(r"\s+", " ", line.strip()).casefold()
            for line in body.splitlines()
            if re.search(r"(?:^|\s)Ticker:\s*\S+", line)
        ]
        if len(set(position_lines)) != len(position_lines):
            errors.append("Tab-2 enthält identische Positionszeilen; Quellen-/Exportidentität nicht eindeutig")
        if "TAB-2-QUELLE NICHT VERIFIZIERBAR" in body:
            errors.append("Tab-2-Quelle im Bericht als nicht verifizierbar markiert")
        digest_match = re.search(r"(?m)^TAB-2-EXPORT-SHA256=([0-9a-f]{64})\s*$", body)
        if not digest_match:
            errors.append("Tab-2-Export-Fingerabdruck fehlt oder ist ungueltig")
        else:
            export_lines = [line.rstrip() for line in body.strip().splitlines()
                            if not line.startswith("TAB-2-EXPORT-SHA256=")]
            actual_digest = hashlib.sha256("\n".join(export_lines).encode("utf-8")).hexdigest()
            if actual_digest != digest_match.group(1):
                errors.append("Tab-2-Export-Fingerabdruck stimmt nicht mit gespeicherten Zeilen ueberein")
        if count_match and int(count_match.group(1)) > 0 and not ticker_values:
            errors.append("Tab-2 meldet Positionen, aber keine konkreten Ticker-Datensätze")

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
