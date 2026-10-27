"""Deterministic market-data and event normalization helpers for Trading Radar.

These helpers do not infer financial facts. They normalize source rows into one
canonical record and aggregate explicit source statuses without conflating them.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

MISSING_TOKENS = {"", "-", "—", "n/a", "na", "nicht verfügbar", "nicht verfuegbar",
                  "unbekannt", "fehlt", "unklar", "none", "null"}
OHLCV_STATUSES = {"VALID", "MISSING", "STALE", "INVALID"}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).strip().split())
    return None if value.casefold() in MISSING_TOKENS else value


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one event. Missing fields are represented once as null/status."""
    out = {
        "event_id": _clean(event.get("event_id") or event.get("id")),
        "name": _clean(event.get("name") or event.get("event") or event.get("kennzahl")),
        "date": _clean(event.get("date") or event.get("datum")),
        "time": _clean(event.get("time") or event.get("uhrzeit")),
        "timezone": _clean(event.get("timezone") or event.get("zeitzone")),
        "period": _clean(event.get("period") or event.get("berichtszeitraum")),
        "previous": _clean(event.get("previous") or event.get("last_value") or event.get("letzter_wert")),
        "consensus": _clean(event.get("consensus") or event.get("konsens")),
        "actual": _clean(event.get("actual") or event.get("istwert")),
        "source": _clean(event.get("source") or event.get("quelle")),
    }
    missing = [key for key in ("name", "date", "source") if out[key] is None]
    out["status"] = "INCOMPLETE" if missing else "READY"
    out["missing_required"] = missing
    out["time_verified"] = bool(out["time"] and out["timezone"])
    return out


def deduplicate_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate events by stable ID, else by normalized name/date/source."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in events:
        item = normalize_event(raw)
        key = (item["event_id"] or "", item["name"] or "", item["date"] or "", item["source"] or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def classify_ohlcv_row(row: Mapping[str, Any], *, cutoff: datetime) -> str:
    """Classify a symbol row as VALID/MISSING/STALE/INVALID using explicit inputs."""
    values = [row.get(k) for k in ("open", "high", "low", "close", "volume", "timestamp")]
    if any(v is None or (isinstance(v, str) and v.strip().casefold() in MISSING_TOKENS) for v in values):
        return "MISSING"
    try:
        numbers = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return "INVALID"
    if stamp.tzinfo is None:
        return "INVALID"
    op, hi, lo, close, vol = numbers
    if min(op, hi, lo, close) <= 0 or vol < 0 or hi < max(op, lo, close) or lo > min(op, hi, close):
        return "INVALID"
    if stamp > cutoff:
        return "INVALID"  # future-dated data is invalid for this cutoff
    if (cutoff - stamp).total_seconds() > 7 * 24 * 3600:
        return "STALE"
    return "VALID"


def summarize_ohlcv_status(statuses: Iterable[str]) -> dict[str, int]:
    """Count each explicit OHLCV status exactly once; unknown values fail closed."""
    counts = {status: 0 for status in sorted(OHLCV_STATUSES)}
    for status in statuses:
        if status not in OHLCV_STATUSES:
            raise ValueError(f"Unbekannter OHLCV-Status: {status!r}")
        counts[status] += 1
    counts["TOTAL"] = sum(counts.values())
    return counts



def normalize_event_section(report_text: str) -> tuple[str, list[str]]:
    """Canonicalize event records in report section 3 and remove duplicates.

    This is intentionally conservative: it does not invent values or infer dates.
    If the section is not parseable, it is left intact and a finding is returned.
    """
    import re
    headings = list(re.finditer(r"(?im)^\s*(?:#{1,6}\s*)?3\s*[.·]\s*Ereignisradar\s*$", report_text))
    if len(headings) != 1:
        return report_text, [f"Ereignisradar-Abschnitt nicht eindeutig auffindbar (Anzahl={len(headings)})"]
    start = headings[0].end()
    next_heading = re.search(r"(?im)^\s*(?:#{1,6}\s*)?4\s*[.·]\s*Assetklassenvergleich\s*$", report_text[start:])
    if not next_heading:
        return report_text, ["Ereignisradar-Ende nicht eindeutig auffindbar"]
    end = start + next_heading.start()
    body = report_text[start:end]
    if re.fullmatch(r"(?is)\s*(?:keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse[^\n]*\s*)?", body):
        return report_text, []
    # A record starts at a bullet or numbered line that is not a field label.
    lines = body.splitlines()
    field = re.compile(r"(?i)^\s*(?:[-•]\s*)?(?:Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|Berichtszeitraum|Zeitraum|Vorwert|Konsens|Istwert|Quelle)\s*[:=]")
    starts = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.+)$", line)
        if m and not field.match(line):
            starts.append(i)
    if not starts:
        # Single non-empty paragraph is treated as one event only if explicitly named.
        if not re.search(r"(?im)^\s*(?:Ereignis|Termin|Event)\s*[:=]", body):
            return report_text, ["Ereignisradar enthält keine eindeutig abgrenzbaren Ereignisdatensätze"]
        starts = [i for i, line in enumerate(lines) if re.match(r"(?i)^\s*(?:Ereignis|Termin|Event)\s*[:=]", line)]
    records = []
    if starts:
        if starts[0] > 0 and any(x.strip() for x in lines[:starts[0]]):
            intro = lines[:starts[0]]
        else:
            intro = []
        for n, idx in enumerate(starts):
            stop = starts[n+1] if n+1 < len(starts) else len(lines)
            records.append(lines[idx:stop])
    else:
        intro, records = [], []
    seen = set(); unique = []; findings = []
    for rec_lines in records:
        rec = "\n".join(rec_lines).strip()
        name_m = re.search(r"(?im)^\s*(?:[-*•]\s*)?(?:Ereignis|Termin|Event|Kennzahl|Indikator)?\s*[:=]?\s*(.+?)\s*$", rec_lines[0])
        name = (name_m.group(1) if name_m else rec_lines[0]).strip().casefold()
        date_m = re.search(r"(?im)^\s*(?:Datum|Date)\s*[:=]\s*([^\r\n|;]+)", rec)
        source_m = re.search(r"(?im)^\s*(?:Quelle|Source)\s*[:=]\s*([^\r\n|;]+)", rec)
        key = (name, (date_m.group(1).strip().casefold() if date_m else ""), (source_m.group(1).strip().casefold() if source_m else ""))
        if key in seen:
            continue
        seen.add(key)
        # Collapse repeated unavailable placeholders per field to one canonical value.
        for label in ("Uhrzeit", "Zeitzone", "Kennzahl", "Berichtszeitraum"):
            pattern = re.compile(rf"(?im)^\s*(?:[-•]\s*)?{label}\s*[:=]\s*(?:NICHT VERFÜGBAR[^\r\n]*|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR)\s*$")
            hits = list(pattern.finditer(rec))
            if len(hits) > 1:
                first = hits[0].start(); last = hits[-1].end()
                rec = rec[:first] + f"{label}: NICHT VERFÜGBAR (nicht verifiziert)" + rec[last:]
                # Remove remaining duplicate placeholder lines after first replacement.
                rec_lines2 = rec.splitlines(); seen_label = False; kept = []
                for line in rec_lines2:
                    if re.match(rf"(?i)^\s*{label}\s*[:=]", line):
                        if seen_label: continue
                        seen_label = True
                    kept.append(line)
                rec = "\n".join(kept)
        if not re.search(r"(?im)^\s*(?:Quelle|Source)\s*[:=]\s*\S", rec):
            findings.append("Ereignis ohne verifizierte Quelle")
        if not re.search(r"(?im)^\s*(?:Datum|Date)\s*[:=]\s*\S", rec):
            findings.append("Ereignis ohne Datum")
        unique.append(rec)
    rebuilt = "\n".join(intro + ([""] if intro and unique else []) + [r for r in unique])
    if not unique and body.strip():
        findings.append("Ereignisradar konnte nicht sicher normalisiert werden")
        return report_text, findings
    return report_text[:start] + "\n" + rebuilt.rstrip() + "\n\n" + report_text[end:], sorted(set(findings))


def validate_ohlcv_audit(audit: Mapping[str, Any]) -> list[str]:
    """Fail-closed validation of structured OHLCV audit values before FINAL."""
    findings = []
    universe, covered = audit.get("universe_count"), audit.get("covered_count")
    if not isinstance(universe, int) or isinstance(universe, bool) or not isinstance(covered, int) or isinstance(covered, bool):
        return ["OHLCV-Prüfung ohne gültige Universums-/Abdeckungszahlen"]
    if universe < 0 or covered < 0 or covered > universe:
        findings.append("Widersprüchliche OHLCV-Abdeckungszahlen")
    status = str(audit.get("status", "unavailable")).casefold()
    missing = list(audit.get("missing_tickers") or [])
    stale = list(audit.get("stale_tickers") or [])
    invalid = list(audit.get("invalid_ohlcv_tickers") or [])
    if status != "complete" or covered != universe or missing or stale or invalid:
        findings.append("OHLCV-Abdeckung nicht vollständig oder enthält Datenlücken")
    if status == "complete" and (covered != universe or missing or stale or invalid):
        findings.append("OHLCV-Status complete widerspricht den Detaildaten")
    return sorted(set(findings))
