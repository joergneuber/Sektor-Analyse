"""Strict structural validation for the production Trading-Radar report."""
from __future__ import annotations

import re
import sys
import hashlib
import math
import datetime
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

def _normalize_tab2_entry_date(value: str) -> str | None:
    """Return an ISO date only for dates accepted by the Tab-2 data contract."""
    raw = str(value or "").strip()
    if not raw or _MISSING_VALUE_RE.fullmatch(raw):
        return None
    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(raw, date_format).date().isoformat()
        except ValueError:
            continue
    return None


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
        rf"(?im)(?:^|[|;]\s*|\n)\s*(?:[-•]\s+|\d+[.)]\s+)?"
        rf"(?:\*\*)?{label}(?:\*\*)?\s*[:=](?:\*\*)?\s*"
        rf"(?:([^|;\r\n]+)|(?:\r?\n\s*([^|;\r\n]+)))",
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
        rf"(?i)(?:\*\*)?(?:{label})(?:\*\*)?\s*(?:[:=](?:\*\*)?\s*)?"
        rf"(?:nicht\s+(?:verfügbar|verifizierbar|vorhanden)|fehlt|unklar)\b",
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

    # M-06: each field is evaluated independently. A placeholder cannot be
    # rescued by an unrelated unavailable-status mention elsewhere in the section.
    benchmark = _section_body(text, 7, SECTION_NAMES[6])
    portfolio_labels = (r"Portfolio(?:-Gesamt)?performance", r"Gesamtperformance\s+Portfolio")
    average_labels = (r"(?:Ø|durchschnitt(?:liche|s)?|mittelwert)\s*Positionsperformance",
                      r"Positionsperformance(?:\s*\(?(?:Ø|Durchschnitt|Mittelwert)\)?)?")

    def performance_field_ok(labels):
        label = r"(?:" + "|".join(labels) + r")"
        matches = list(re.finditer(
            rf"(?im)(?:^|[|;]\s*|\n)\s*{label}\s*[:=]\s*([^|;\r\n]+)",
            benchmark,
        ))
        if matches:
            for match in matches:
                value = match.group(1).strip().strip(". ")
                if _valid_numeric_value(value):
                    continue
                if re.fullmatch(
                    r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?",
                    value,
                ):
                    continue
                return False
            return True
        # Legacy prose form remains accepted only if it is an explicit status.
        return _is_explicit_unavailable(benchmark, labels) or _has_any(
            benchmark,
            (r"Portfolio-Gesamtperformance\s+(?:nicht verfügbar|nicht verifizierbar)",
             r"Positionsperformance\s+(?:nicht verfügbar|nicht verifizierbar)"),
        )

    portfolio_ok = performance_field_ok(portfolio_labels)
    average_ok = performance_field_ok(average_labels)
    if not (portfolio_ok and average_ok):
        errors.append("M-06: Portfolio-Gesamtperformance und durchschnittliche Positionsperformance müssen jeweils einen konkreten Zahlenwert oder einen ausdrücklichen Nichtverfügbarkeitsstatus haben.")

    # M-07: identity/version/status values must be populated, not merely labels.
    thesis = _section_body(text, 2, SECTION_NAMES[1])
    thesis_id = _label_value(thesis, (r"Thesen-ID", r"thesis_id"))
    version = _label_value(thesis, (r"Version", r"version"))
    status = _label_value(thesis, (r"Status", r"status"))
    version_match = re.search(r"\d+", version or "")
    status_ok = bool(status and re.search(
        r"(?i)\b(?:NEU|AKTUALISIERT|BESTÄTIGT|ABGESCHWÄCHT|WARTET AUF EREIGNIS|INVALIDIERT|ERSETZT|FEHLT|UNKLAR|NICHT VERFÜGBAR)\b",
        status,
    ))
    no_thesis_update = _has_any(thesis, (
        r"keine\s+neue\s+oder\s+aktualisierte\s+these\s*[;,.]?\s*"
        r"thesenregister\s+geprüft,?\s*keine\s+änderung",
        r"keine\s+neue\s+oder\s+aktualisierte\s+these\s*[;,.]?\s*"
        r"thesenregister\s+geprueft,?\s*keine\s+aenderung",
        r"(?:keine\s+neuen?\s+thesen|keine\s+thesenaktualisierung|"
        r"keine\s+aktualisierung\s+der\s+thesen).{0,120}"
        r"(?:thesenregister.{0,60}(?:geprüft|geprueft|unverändert|unveraendert|keine\s+änderung|keine\s+aenderung)|"
        r"keine\s+änderung(?:en)?\s+am\s+thesenregister)",
        r"thesenregister.{0,80}(?:unverändert|unveraendert|keine\s+änderung|keine\s+aenderung)",
    ))
    if not no_thesis_update and not (
        thesis_id and version_match and int(version_match.group(0)) >= 1 and status_ok
    ):
        errors.append("M-07: Thesen-Update benötigt Thesen-ID, positive Version und zulässigen Status oder den expliziten Nachweis, dass keine These aktualisiert wurde.")

    # M-08: validate each event record independently. A complete event must
    # not mask a second event that lacks its own time/zone/metric/period/value.
    events = _section_body(text, 3, SECTION_NAMES[2])
    event_labels = (
        (r"Uhrzeit", r"Zeit"),
        (r"Zeitzone",),
        (r"Kennzahl", r"Indikator"),
        (r"Berichtszeitraum", r"Zeitraum"),
    )
    observation_labels = (r"letzter Wert", r"Vorwert", r"Konsens", r"Istwert")
    no_relevant_events = bool(re.fullmatch(
        r"(?is)\s*(?:[-*•]\s*)?(?:Ereignisradar\s*:\s*)?"
        r"keine\s+(?:(?:relevanten|anstehenden)\s+)?ereignisse"
        r"(?:\s+im\s+[^.!?\r\n]+)?[.!]?\s*",
        events,
    ))

    def event_records(section_body):
        """Split bullet/numbered event entries; field-only bullets continue a record."""
        records = []
        current = []
        field_only = re.compile(
            r"(?i)^\s*(?:[-*•]\s+|\d+[.)]\s+)?(?:\*\*)?"
            r"(?:Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|Berichtszeitraum|Zeitraum|"
            r"letzter Wert|Vorwert|Konsens|Istwert)(?:\*\*)?\s*[:=](?:\*\*)?"
        )
        bullet = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.*)$")
        named_event = re.compile(r"(?i)^\s*(?:Ereignis|Termin|Event)\s*[:=]")
        for line in section_body.splitlines():
            match = bullet.match(line)
            starts_record = (
                (bool(match) and not field_only.match(line))
                or (bool(named_event.match(line)) and not field_only.match(line))
            )
            if starts_record and current:
                records.append("\n".join(current).strip())
                current = []
            current.append(line)
        if current:
            records.append("\n".join(current).strip())
        records = [record for record in records if record]
        return records or ([section_body.strip()] if section_body.strip() else [])

    def field_ok(record, labels, timezone=False):
        label = r"(?:" + "|".join(labels) + r")"
        matches = list(re.finditer(
            rf"(?im)(?:^|[|;]\s*|\n)\s*(?:[-•]\s+|\d+[.)]\s+)?"
            rf"(?:\*\*)?{label}(?:\*\*)?\s*[:=](?:\*\*)?\s*([^|;\r\n]+)",
            record,
        ))
        if not matches:
            return _is_explicit_unavailable(record, labels)
        for match in matches:
            value = match.group(1).strip().strip(". ")
            if re.fullmatch(
                r"(?i)(?:NICHT\s+VERFÜGBAR|nicht\s+verifizierbar|nicht\s+vorhanden|fehlt|unklar)(?:\s*\([^)]*\))?",
                value,
            ):
                continue
            if _MISSING_VALUE_RE.fullmatch(value):
                return False
            if timezone and not re.search(
                r"(?i)(?:\b(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|UTC|CET|CEST|GMT)\b|"
                r"\b(?:UTC|GMT)?[+-]\d{1,2}(?::?\d{2})?\b|"
                r"\b[A-Za-z_]+/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?\b)", value
            ):
                return False
        return True

    if not no_relevant_events:
        records = event_records(events)
        for event_index, record in enumerate(records, start=1):
            values_ok = all(
                field_ok(record, event_labels[i], timezone=(i == 1))
                for i in range(4)
            )
            observation_ok = any(
                _label_value(record, (label,)) is not None
                or _is_explicit_unavailable(record, (label,))
                for label in observation_labels
            )
            if not values_ok or not observation_ok:
                errors.append(
                    f"M-08: Ereignisdatensatz {event_index} benötigt eigene Uhrzeit, "
                    "Zeitzone, Kennzahl, Zeitraum und einen ausgewiesenen Wert/Konsens "
                    "oder expliziten Fehlstatus."
                )
    # M-09: isolate candidate records. A complete candidate must never supply
    # fields for another candidate via section-wide/global label searches.
    candidates = _section_body(text, 5, SECTION_NAMES[4])

    def split_candidate_records(section_body: str) -> list[str]:
        records: list[str] = []
        current: list[str] = []
        bullet = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.*)$")
        field_start = re.compile(
            r"(?i)^\s*(?:[-*•]\s+|\d+[.)]\s+)?(?:\*\*)?"
            r"(?:Trigger|Bestätigung|Bestaetigung|Einstieg|Entry|Stop|Stop-Loss|"
            r"TP1|Ziel\s*1|TP2|Ziel\s*2|CRV|Chance-Risiko|Gegenargument|Risiko|"
            r"Regelstatus|Datenstatus|Richtung|Side)(?:\*\*)?\s*[:=]"
        )
        for line in section_body.splitlines():
            match = bullet.match(line)
            starts = bool(match) and not field_start.match(line)
            # A line naming a new ticker/asset starts a new candidate even when
            # it isn't a markdown bullet. Field continuation lines remain local.
            if re.match(r"(?i)^\s*(?:Ticker|Asset)\s*[:=]", line) and current:
                starts = True
            if starts and current:
                records.append("\n".join(current).strip())
                current = []
            current.append(match.group(1) if starts and match else line)
        if current:
            records.append("\n".join(current).strip())
        # The report's established one-candidate inline format is a single
        # record. Empty records are ignored, but an explicit no-candidate
        # declaration is handled separately below.
        return [record for record in records if record.strip()]

    def parse_number(value: str | None) -> float | None:
        if value is None or not _valid_numeric_value(value):
            return None
        cleaned = re.sub(r"^(?:EUR|USD|GBP|€|\$|£)\s*", "", value.strip(), flags=re.I)
        cleaned = re.sub(r"\s*(?:EUR|USD|GBP|€|\$|£|%|x|\b(?:Punkte|pts)\b)$", "", cleaned, flags=re.I).strip()
        cleaned = cleaned.replace(" ", "").replace("’", "").replace("'", "")
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".") if cleaned.rfind(",") > cleaned.rfind(".") else cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            number = float(cleaned)
            return number if math.isfinite(number) else None
        except (ValueError, NameError):
            return None

    no_actionable_candidates = _has_any(candidates, (
        r"keine\s+(?:belastbaren\s+)?(?:technischen\s+)?Kandidaten",
        r"keine\s+Kandidaten\s+(?:mit\s+)?(?:belastbarem|konkretem)\s+Setup",
        r"kein\s+belastbarer\s+technischer\s+Kandidat",
    ))
    allowed_candidate_status = re.compile(
        r"JETZT TECHNISCH PRÜFEN|AUF TRIGGER WARTEN|EREIGNIS ABWARTEN|"
        r"NICHT HANDELBAR|VERWERFEN\s*/\s*INVALIDIERT",
        re.IGNORECASE,
    )
    if not no_actionable_candidates:
        records = split_candidate_records(candidates)
        if not records:
            errors.append("M-09: Keine separat prüfbaren Kandidatendatensätze gefunden.")
        for candidate_index, record in enumerate(records, start=1):
            def field(labels: tuple[str, ...]) -> str | None:
                return _label_value(record, labels)

            asset = field((r"Ticker", r"Asset"))
            trigger = field((r"Trigger", r"Bestätigung", r"Bestaetigung"))
            entry_text = field((r"Einstieg", r"Entry"))
            stop_text = field((r"Stop", r"Stop-Loss"))
            tp1_text = field((r"TP1", r"Ziel\s*1"))
            tp2_text = field((r"TP2", r"Ziel\s*2"))
            crv_text = field((r"CRV", r"Chance-Risiko"))
            counter = field((r"Gegenargument", r"Risiko"))
            rule_data = field((r"Regelstatus", r"Datenstatus"))
            status_ok = bool(allowed_candidate_status.search(record))
            entry_num, stop_num = parse_number(entry_text), parse_number(stop_text)
            tp1_num, crv_num = parse_number(tp1_text), parse_number(crv_text)
            required = (asset, trigger, entry_text, stop_text, tp1_text, tp2_text,
                        crv_text, counter, rule_data)
            numeric_ok = all(parse_number(value) is not None for value in
                             (entry_text, stop_text, tp1_text, tp2_text, crv_text))
            if not all(required) or not numeric_ok or not status_ok:
                errors.append(
                    f"M-09: Kandidat {candidate_index} benötigt eigene Asset-/Trigger-/Einstiegs-/Stop-/TP-/CRV-/"
                    "Gegenargument-/Regelstatusfelder und einen zulässigen Status; Zahlen müssen numerisch sein."
                )
                continue

            direction_text = field((r"Richtung", r"Side", r"Positionstyp")) or ""
            direction = direction_text.strip().casefold()
            is_short = direction in {"short", "verkauf", "sell"}
            is_long = direction in {"long", "kauf", "buy"}
            # Explicit direction enables deterministic stop-side validation.
            # If omitted, infer only from the common directional relationship;
            # equal entry/stop is always invalid.
            invalid_stop = stop_num == entry_num
            if is_long and stop_num >= entry_num:
                invalid_stop = True
            elif is_short and stop_num <= entry_num:
                invalid_stop = True
            elif not is_long and not is_short:
                if stop_num > entry_num and tp1_num > entry_num:
                    invalid_stop = True
                elif stop_num < entry_num and tp1_num < entry_num:
                    invalid_stop = True
            if invalid_stop:
                errors.append(
                    f"M-09: Kandidat {candidate_index} ({asset}) hat eine unplausible Stop-Lage "
                    f"(Einstieg={entry_num:g}, Stop={stop_num:g}, Richtung={direction_text or 'nicht angegeben'})."
                )
                continue

            if is_short:
                risk = stop_num - entry_num
                reward = entry_num - tp1_num
            else:
                risk = entry_num - stop_num
                reward = tp1_num - entry_num
            if risk <= 0:
                errors.append(f"M-09: Kandidat {candidate_index} ({asset}) hat kein positives Risiko zwischen Einstieg und Stop.")
                continue
            computed_crv = reward / risk
            if abs(computed_crv - crv_num) > max(0.35, abs(computed_crv) * 0.20):
                errors.append(
                    f"M-09: Kandidat {candidate_index} ({asset}) weist CRV={crv_num:g} aus, "
                    f"aus Einstieg/Stop/TP1 ergibt sich jedoch {computed_crv:.2f}."
                )
            if crv_num < 1 and re.search(r"JETZT TECHNISCH PRÜFEN", record, re.IGNORECASE):
                errors.append(f"M-09: Kandidat {candidate_index} ({asset}) hat CRV<1 und darf nicht als JETZT TECHNISCH PRÜFEN gelten.")

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
        # The report's established wording states that no technical OHLCV gaps
        # restrict the sector analysis. When paired with explicit complete
        # OHLCV coverage above, this is equivalent evidence of no candidate
        # restriction and must not be rejected as a false M-10 failure.
        r"keine\s+technischen\s+Datenlücken[^\r\n]{0,100}(?:Sektor-Analyse|Sektoranalyse)[^\r\n]{0,60}einschränken",
        r"keine\s+technischen\s+Datenluecken[^\r\n]{0,100}(?:Sektor-Analyse|Sektoranalyse)[^\r\n]{0,60}einschraenken",
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
    # Bound the block at chapter 11, not only at the final status marker.
    # Otherwise method/data-appendix lines are accidentally included in the
    # export digest and a correct SHA-256 appears invalid.
    closed_pattern = re.compile(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?10\.5[.]?\s+Geschlossene Positionen[^\r\n]*\r?\n(.*?)"
        r"(?=^[ \t]*(?:#{1,6}\s*)?11(?:\s*[.·](?!\d)|\s+)[^\r\n]*$"
        r"|^[ \t]*TRADING_RADAR_FACHLICH_FINAL=|\Z)"
    )
    closed_sections = list(closed_pattern.finditer(text))
    if len(closed_sections) > 1:
        errors.append("Mehrere Abschnitte 10.5 Geschlossene Positionen gefunden")
    canonical_sections = [
        match for match in closed_sections
        if re.search(r"(?m)^TAB-2-ANZAHL=\d+\s*$", match.group(1))
        and re.search(r"(?m)^TAB-2-QUELLE=[^\r\n]+\s*$", match.group(1))
    ]
    if len(canonical_sections) == 1:
        closed = canonical_sections[0]
    elif not closed_sections:
        closed = None
        errors.append("Geschlossene-Positionen-Abschnitt 10.5 fehlt")
    elif not canonical_sections:
        closed = closed_sections[0]
    else:
        closed = None
        errors.append("Mehrere kanonische Tab-2-Exportblöcke gefunden")
    if closed is not None:
        body = closed.group(1)
        count_match = re.search(r"(?m)^TAB-2-ANZAHL=(\d+)\s*$", body)
        source_match = re.search(r"(?m)^TAB-2-QUELLE=([^\r\n]+)\s*$", body)
        if not source_match or not source_match.group(1).strip():
            errors.append("Tab-2-Quellenmarker fehlt oder ist leer")
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
            line for line in body.splitlines()
            if re.search(r"(?:^|\s)Ticker:\s*\S+", line)
        ]
        # Jede Position muss ueber den vereinbarten Schluessel Ticker +
        # Einstiegsdatum identifizierbar sein. Der komplette Zeilentext ist
        # kein geeigneter Schluessel, da Quellzeilennummern unterschiedlich
        # sein koennen und weitere Faktenfelder sich wiederholen duerfen.
        position_keys = []
        invalid_identity_rows = []
        for line in position_lines:
            ticker_match = re.search(r"(?:^|\s)Ticker:\s*([^|\r\n]+)", line)
            entry_match = re.search(r"(?:^|\s)Einstiegsdatum:\s*([^|\r\n]+)", line)
            source_row_match = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
            ticker = ticker_match.group(1).strip().casefold() if ticker_match else ""
            entry_date = entry_match.group(1).strip() if entry_match else ""
            source_row = source_row_match.group(1) if source_row_match else "?"
            normalized_date = _normalize_tab2_entry_date(entry_date)
            if not ticker or normalized_date is None:
                invalid_identity_rows.append(source_row)
            else:
                position_keys.append((ticker, normalized_date))
        if invalid_identity_rows:
            errors.append(
                "Tab-2-Positionsidentitaet fehlt: Ticker+Einstiegsdatum "
                "nicht verifizierbar bei Quellzeilen " + ", ".join(invalid_identity_rows)
            )
        duplicate_keys = sorted(
            f"{ticker}|{entry_date}"
            for ticker, entry_date in set(position_keys)
            if position_keys.count((ticker, entry_date)) > 1
        )
        if duplicate_keys:
            errors.append(
                "Tab-2-Positionsschluessel Ticker+Einstiegsdatum doppelt: "
                + ", ".join(duplicate_keys)
            )
        source_rows = []
        for line in position_lines:
            source_match_row = re.search(r"(?:^|\s)Quellzeile:\s*(\d+)\b", line)
            if source_match_row:
                source_rows.append(int(source_match_row.group(1)))
        if position_lines and len(source_rows) != len(position_lines):
            errors.append("Tab-2-Quellzeilennummer fehlt bei mindestens einem Positionsdatensatz")
        if len(set(source_rows)) != len(source_rows):
            errors.append("Tab-2-Quellzeilennummer ist doppelt; Quellen-/Exportidentität nicht eindeutig")
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
