"""Deterministic market-data and event normalization helpers for Trading Radar.

These helpers do not infer financial facts. They normalize source rows into one
canonical record and aggregate explicit source statuses without conflating them.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from datetime import datetime, date
from pathlib import Path
import json
from typing import Any
import re

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




def append_live_market_snapshot(report_text: str, *, fetcher=None) -> tuple[str, list[str]]:
    """Add a compact, source-stamped market snapshot using live Yahoo Finance data.

    The snapshot is evidence, not a forecast. Missing instruments are reported as
    unavailable; no values are inferred. An injectable fetcher keeps tests offline.
    """
    import pandas as pd
    if fetcher is None:
        try:
            import yfinance as yf
        except ImportError:
            return report_text, ["Markt-Snapshot nicht erstellt: yfinance nicht installiert"]
        def fetcher(symbols):
            return yf.download(
                tickers=symbols, period="4mo", interval="1d", auto_adjust=True,
                group_by="ticker", progress=False, threads=True,
            )
    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    ]
    try:
        data = fetcher(symbols)
    except Exception as exc:
        return report_text, [f"Markt-Snapshot fehlgeschlagen: {type(exc).__name__}: {exc}"]
    if data is None or getattr(data, "empty", True):
        return report_text, ["Markt-Snapshot fehlgeschlagen: keine Kursdaten geliefert"]

    def series_for(symbol):
        try:
            if isinstance(data.columns, pd.MultiIndex):
                # yfinance group_by='ticker' generally yields (ticker, field).
                if (symbol, "Close") in data.columns:
                    values = data[(symbol, "Close")].dropna()
                elif ("Close", symbol) in data.columns:
                    values = data[("Close", symbol)].dropna()
                else:
                    return None
            else:
                values = data["Close"].dropna() if "Close" in data else None
            if values is None or len(values) < 2:
                return None
            return values
        except (KeyError, TypeError, AttributeError):
            return None

    def pct(symbol, periods):
        values = series_for(symbol)
        if values is None or len(values) <= periods:
            return None
        try:
            return (float(values.iloc[-1]) / float(values.iloc[-1-periods]) - 1) * 100
        except (ValueError, TypeError, ZeroDivisionError):
            return None

    def latest(symbol):
        values = series_for(symbol)
        if values is None:
            return None
        try:
            return float(values.iloc[-1])
        except (ValueError, TypeError):
            return None

    def fmt(value, suffix=""):
        return f"{value:+.2f}{suffix}" if value is not None else "NICHT VERFÜGBAR"

    lines = [
        "",
        "**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**",
        f"- Datenstand UTC: {datetime.now().astimezone().astimezone(__import__('datetime').timezone.utc).replace(microsecond=0).isoformat()}",
    ]
    labels = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq Composite", "^RUT": "Russell 2000"}
    for symbol, label in labels.items():
        close = latest(symbol)
        r20, r60 = pct(symbol, 20), pct(symbol, 60)
        lines.append(
            f"- Index {label} ({symbol}): letzter Schluss {close:.2f}; 1M {fmt(r20, '%')}; 3M {fmt(r60, '%')}."
            if close is not None else
            f"- Index {label} ({symbol}): NICHT VERFÜGBAR."
        )
    vix, dxy, tnx_raw, tlt = latest("^VIX"), latest("DX-Y.NYB"), latest("^TNX"), pct("TLT", 20)
    # Yahoo ^TNX quotes the 10-year yield in tenths of a percentage point.
    tnx = (tnx_raw / 10.0) if tnx_raw is not None else None
    lines.append(f"- Volatilität VIX: {vix:.2f}" if vix is not None else "- Volatilität VIX: NICHT VERFÜGBAR.")
    lines.append(f"- Dollar-Proxy DXY (DX-Y.NYB): {dxy:.2f}" if dxy is not None else "- Dollar-Proxy DXY: NICHT VERFÜGBAR.")
    lines.append(f"- US-10J-Rendite (^TNX): {tnx:.2f}%" if tnx is not None else "- US-10J-Rendite: NICHT VERFÜGBAR.")
    lines.append(f"- Anleihe-ETF TLT 1M: {fmt(tlt, '%')}.")
    sector_etfs = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    above = total = 0
    for symbol in sector_etfs:
        values = series_for(symbol)
        if values is None or len(values) < 50:
            continue
        try:
            total += 1
            above += int(float(values.iloc[-1]) > float(values.iloc[-50:].mean()))
        except (ValueError, TypeError):
            total -= 1
    lines.append(
        f"- Marktbreite-Proxy: {above}/{total} Sektor-ETFs über ihrem 50-Tage-Durchschnitt."
        if total else "- Marktbreite-Proxy: NICHT VERFÜGBAR."
    )
    lines.append("- Quelle: Yahoo Finance (Ticker in den Zeilen; Tages-Schlusskurse).")
    block = "\n".join(lines) + "\n"
    # Avoid duplicating the live snapshot if the report is reprocessed.
    # Accept decorated Markdown section headings such as
    # "### 2. Thesen-Update (Register)" while still requiring a standalone
    # heading line. Use the same boundary for cleanup and insertion so a
    # repeated run cannot leave a duplicate snapshot behind.
    section_two_heading = (
        r"^[ \t]*(?:#{1,6}[ \t]*)?2[ \t]*[.·][ \t]*"
        r"Thesen-Update\b[^\r\n]*$"
    )
    report_text = re.sub(
        r"(?ms)\n\*\*Live-Marktbelege \(Yahoo Finance; Schlusskurse, keine Prognose\):\*\*"
        r".*?(?=\n[ \t]*(?:#{1,6}[ \t]*)?2[ \t]*[.·][ \t]*Thesen-Update\b[^\r\n]*$)",
        "", report_text,
    )
    insertion = re.search(section_two_heading, report_text, re.I | re.M)
    if not insertion:
        return report_text, ["Markt-Snapshot vorhanden, aber Abschnitt 2 für Einfügung nicht auffindbar"]
    return report_text[:insertion.start()] + block + "\n" + report_text[insertion.start():], []


def _verify_fomc_official_source(event_date: date, *, requester=None) -> tuple[bool, str]:
    """Verify date, FOMC label and 14:00 in the same official calendar event block.

    A page-wide match is deliberately insufficient: unrelated text elsewhere on
    the monthly calendar must not validate the selected event. The press
    conference time is reported as verified only when it appears in that block.
    """
    month_url = f"https://www.federalreserve.gov/newsevents/{event_date:%Y-%B}.htm?view=calendar".replace(
        event_date.strftime("%B"), event_date.strftime("%B").lower()
    )
    try:
        if requester is None:
            import requests
            requester = requests.get
        response = requester(month_url, timeout=12, headers={"User-Agent": "TradingRadar/1.0 (event-source-verification)"})
        status_code = int(getattr(response, "status_code", 0))
        final_url = str(getattr(response, "url", month_url) or month_url)
        html = str(getattr(response, "text", "") or "")
        if status_code != 200:
            return False, f"Fed-Kalender HTTP {status_code}"
        if not re.match(r"^https://www\.federalreserve\.gov/newsevents/", final_url, re.I):
            return False, "Weiterleitung auf nicht zugelassene Quelldomain"

        # Prefer semantic event rows/items. If no such blocks are present, fail
        # closed rather than combining unrelated page-wide text.
        blocks = re.findall(r"(?is)<tr\b[^>]*>.*?</tr\s*>", html)
        if not blocks:
            blocks = re.findall(r"(?is)<li\b[^>]*>.*?</li\s*>", html)

        def plain_text(fragment: str) -> str:
            fragment = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", " ", fragment)
            fragment = re.sub(r"(?is)<[^>]+>", " ", fragment)
            return re.sub(r"\s+", " ", fragment).casefold()

        month = event_date.strftime("%B").casefold()
        day = str(event_date.day)
        year = str(event_date.year)
        date_pattern = re.compile(
            rf"\b{re.escape(month)}\s+(?:\d{{1,2}}\s*[-–]\s*)?{re.escape(day)}"
            rf"(?:\s*[-–]\s*\d{{1,2}})?[,]?\s+{year}\b"
        )
        matching_blocks = []
        for block in blocks:
            text = plain_text(block)
            if (
                date_pattern.search(text)
                and re.search(r"\bfomc\b", text)
                and re.search(r"\b2\s*:\s*00\s*p\.?\s*m\.?\b", text)
            ):
                matching_blocks.append(text)
        if not matching_blocks:
            return False, "Kein einzelner offizieller Kalender-Eintrag enthält gleichzeitig Datum, FOMC und 14:00 Uhr"
        press_verified = any(
            re.search(r"\b2\s*:\s*30\s*p\.?\s*m\.?\b", block)
            for block in matching_blocks
        )
        detail = f"Datum, FOMC und 14:00 Uhr im selben offiziellen Kalender-Eintrag bestätigt ({final_url})"
        if press_verified:
            detail += "; PRESSEKONFERENZ_VERIFIZIERT"
        else:
            detail += "; Pressekonferenzzeit im Eintrag nicht separat bestätigt"
        return True, detail
    except Exception as exc:
        return False, f"Fed-Kalenderprüfung fehlgeschlagen: {type(exc).__name__}: {exc}"


def _enrich_fomc_event(
    body: str,
    report_text: str,
    calendar_path: str | Path = "fomc_termine.json",
    *,
    source_requester=None,
) -> str:
    """Enrich FOMC record, marking it verified only after a live official-source check."""
    if not re.search(r"(?i)\bFOMC\b", body):
        return body
    try:
        calendar_file = Path(calendar_path)
        if not calendar_file.is_absolute():
            calendar_file = Path(__file__).resolve().parent / calendar_file
        payload = json.loads(calendar_file.read_text(encoding="utf-8-sig"))
        dates = sorted(date.fromisoformat(str(value)) for value in payload.get("termine", []))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return body + "\nQuellenprüfung: NICHT VERIFIZIERT (Projektkalender fehlt oder ist ungültig)\n"
    cutoff_match = re.search(
        r"(?i)(?:Datenstand|Cutoff)[^\n]*?(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?)",
        report_text,
    )
    try:
        cutoff_value = cutoff_match.group(1) if cutoff_match else ""
        if "T" in cutoff_value:
            from datetime import time as datetime_time, timezone
            from zoneinfo import ZoneInfo
            cutoff_dt = datetime.fromisoformat(cutoff_value.replace("Z", "+00:00"))
            if cutoff_dt.tzinfo is None:
                cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
            cutoff_local = cutoff_dt.astimezone(ZoneInfo("America/New_York"))
            cutoff = cutoff_local.date()
            # The 14:00 ET statement is no longer upcoming once the report
            # cutoff reaches or passes the scheduled release time.
            if cutoff_local.time().replace(tzinfo=None) >= datetime_time(14, 0):
                cutoff = date.fromordinal(cutoff.toordinal() + 1)
        else:
            cutoff = date.fromisoformat(cutoff_value) if cutoff_value else date.today()
    except (ValueError, OverflowError):
        cutoff = date.today()
    event_date = next((value for value in dates if value >= cutoff), None)
    if event_date is None:
        return body + "\nQuellenprüfung: NICHT VERIFIZIERT (kein passender zukünftiger Termin im Projektkalender)\n"
    verified, verification_detail = _verify_fomc_official_source(event_date, requester=source_requester)
    month_url = f"https://www.federalreserve.gov/newsevents/{event_date:%Y-%B}.htm?view=calendar".replace(
        event_date.strftime("%B"), event_date.strftime("%B").lower()
    )
    clean = []
    for line in body.splitlines():
        if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Ereignis-Uhrzeit|Uhrzeit|Zeit|Zeitzone|Datum|Quelle|Kennzahl|Quellenprüfung)\s*[:=]", re.sub(r"\*\*", "", line)):
            continue
        if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Ereignis(?:\s*\d*)?)\s*[:=]", re.sub(r"\*\*", "", line)):
            continue
        clean.append(line)
    values = "\n".join(clean).strip()
    fields = [
        f"Datum: {event_date.isoformat()}",
        (("Uhrzeit: 14:00 (FOMC-Erklärung); Pressekonferenz: 14:30 (im selben Eintrag bestätigt)"
          if "PRESSEKONFERENZ_VERIFIZIERT" in verification_detail
          else "Uhrzeit: 14:00 (FOMC-Erklärung); Pressekonferenz: NICHT SEPARAT VERIFIZIERT")
         if verified else "Uhrzeit: NICHT VERIFIZIERT (offizieller Kalenderabruf fehlgeschlagen)"),
        "Zeitzone: America/New_York (EDT/EST gemäß Ereignisdatum)",
        "Kennzahl: FOMC-Zinsentscheid / Zielkorridor Fed Funds Rate",
        f"Quelle: Federal Reserve, offizieller Kalender ({month_url})",
        f"Quellenprüfung: {'LIVE_VERIFIZIERT' if verified else 'NICHT VERIFIZIERT'} — {verification_detail}",
    ]
    return "- Ereignis: FOMC-Zinsentscheid\n" + "\n".join(fields) + "\n" + values + "\n"



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
    no_events = bool(re.search(
        r"(?im)^\s*[-*•]?\s*keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse[^\r\n]*$",
        body,
    ))
    if no_events:
        # A no-events statement must not be followed by orphaned event-field
        # placeholders; they otherwise get misread as a malformed event record.
        nonempty = [line.strip() for line in body.splitlines() if line.strip()]
        allowed = re.compile(
            r"(?i)^(?:[-*•]\s*)?(?:Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|"
            r"Berichtszeitraum|Zeitraum|Vorwert|Letzter Wert|Konsens|Istwert|Quelle)\s*[:=]\s*"
            r"(?:NICHT VERFÜGBAR(?:\s*\([^)]*\))?|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR).*$"
        )
        if all(re.search(
            r"(?i)^[-*•]?\s*keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse",
            line,
        ) or allowed.fullmatch(line) or line in {"---"} for line in nonempty):
            clean_body = "\n\n* Keine relevanten Ereignisse im geprüften Zeitraum.\n\n"
            return report_text[:start] + clean_body + report_text[end:], []
    if re.fullmatch(r"(?is)\s*(?:keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse[^\n]*\s*)?", body):
        return report_text, []
    body = _enrich_fomc_event(body, report_text)
    # A record starts at a bullet or numbered line that is not a field label.
    lines = body.splitlines()
    # Markdown labels (including "- **Quelle:**") are continuation fields,
    # not new event records. Strip formatting before classifying each line.
    field_labels = (
        "Ereignis-Uhrzeit|Ereignis Uhrzeit|Datum|Date|Uhrzeit|Zeit|Zeitzone|"
        "Kennzahl|Indikator|Berichtszeitraum|Zeitraum|Vorwert|Letzter Wert|"
        "Konsens|Istwert|Quelle|Source|Betroffene These/Assetklasse/konkrete offene Positionen|"
        "Szenarien|Reaktionskanäle|Bestätigung/Widerlegung|Bestaetigung/Widerlegung|Quellenprüfung|Quellenpruefung"
    )
    field = re.compile(rf"(?i)^\s*(?:{field_labels})\s*[:=]")
    def is_field_line(line: str) -> bool:
        normalized = re.sub(r"^\s*[-*•]\s*", "", line).strip()
        normalized = normalized.replace("**", "").replace("__", "")
        return bool(field.match(normalized))
    starts = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.+)$", line)
        if m and not is_field_line(line):
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
        # Strip markdown emphasis before extracting labels. Both **Quelle:** and
        # **Quelle**: are common generator outputs and mean the same field.
        plain_rec = re.sub(r"\*\*([^*]+)\*\*", r"\1", rec)
        date_m = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:Datum|Date)\s*[:=]\s*([^\r\n|;]+)",
            plain_rec,
        )
        source_m = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=]\s*([^\r\n|;]+)",
            plain_rec,
        )
        key = (name, (date_m.group(1).strip().casefold() if date_m else ""), (source_m.group(1).strip().casefold() if source_m else ""))
        if key in seen:
            continue
        seen.add(key)
        # Remove only duplicate unavailable placeholders. Never splice from the
        # first placeholder to the last: valid values/scenarios between them are
        # meaningful event evidence and must survive normalization.
        rec_lines = rec.splitlines()
        for label in ("Uhrzeit", "Zeitzone", "Kennzahl", "Berichtszeitraum", "Zeitraum", "Letzter Wert", "Konsens"):
            label_pattern = re.compile(
                rf"(?i)^\s*(?:[-*•]\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[:=]\s*(.*)$"
            )
            indexed = [(i, label_pattern.match(line)) for i, line in enumerate(rec_lines)]
            indexed = [(i, m) for i, m in indexed if m]
            if not indexed:
                continue
            placeholder_indices = []
            valid_indices = []
            for i, match in indexed:
                value = match.group(1).strip().strip("* ")
                if re.fullmatch(
                    r"(?i)(?:NICHT VERFÜGBAR(?:\s*\([^)]*\))?|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR)",
                    value,
                ):
                    placeholder_indices.append(i)
                else:
                    valid_indices.append(i)
            remove = set(placeholder_indices if valid_indices else placeholder_indices[1:])
            if remove:
                rec_lines = [line for i, line in enumerate(rec_lines) if i not in remove]
        rec = "\n".join(rec_lines)
        source_present = bool(source_m and source_m.group(1).strip())
        if not source_present and len(records) == 1:
            # In a single-event section, tolerate a source line retained in
            # the section body even if malformed markdown confused record slicing.
            source_present = bool(re.search(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=]\s*\S",
                re.sub(r"\*\*([^*]+)\*\*", r"\1", body),
            ))
        if not source_present:
            findings.append("Ereignis ohne verifizierte Quelle")
        if not date_m or not date_m.group(1).strip():
            findings.append("Ereignis ohne Datum")
        if re.search(r"(?i)\bFOMC\b", plain_rec) and not re.search(
            r"(?im)^\s*(?:[-*•]\s*)?Quellenprüfung\s*[:=]\s*LIVE_VERIFIZIERT\b",
            plain_rec,
        ):
            findings.append("FOMC-Ereignis nicht live gegen den offiziellen Fed-Kalender verifiziert")
        unique.append(rec)
    rebuilt = "\n".join(intro + ([""] if intro and unique else []) + [r for r in unique])
    if not unique and body.strip():
        findings.append("Ereignisradar konnte nicht sicher normalisiert werden")
        return report_text, findings
    return report_text[:start] + "\n" + rebuilt.rstrip() + "\n\n" + report_text[end:], sorted(set(findings))



def downgrade_ohlcv_affected_candidates(report_text: str, audit: Mapping[str, Any]) -> str:
    """Mark only affected section-5 candidates non-tradable; keep unrelated report usable."""
    affected = {
        str(t).strip().upper()
        for key in ("missing_tickers", "stale_tickers", "recent_invalid_ohlcv_tickers")
        for t in (audit.get(key) or [])
    }
    if not affected:
        return report_text
    headings = list(re.finditer(r"(?im)^\s*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten\s*$", report_text))
    if len(headings) != 1:
        return report_text
    start = headings[0].end()
    next_heading = re.search(r"(?im)^\s*(?:#{1,6}\s*)?6\s*[.·]\s*Portfolio\s*&\s*Risiko\s*$", report_text[start:])
    if not next_heading:
        return report_text
    end = start + next_heading.start()
    section = report_text[start:end]
    # Candidate blocks begin at a bullet containing Asset/Ticker; field lines remain in the block.
    starts = list(re.finditer(r"(?im)^\s*[-*•]\s+.*Asset/Ticker\s*[:：]", section))
    if not starts:
        starts = list(re.finditer(r"(?im)^\s*[-*•]\s+.*(?:Asset/Ticker|Ticker|Asset)\s*[:：=]", section))
    if not starts:
        return report_text
    chunks = []
    cursor = 0
    for i, match in enumerate(starts):
        chunks.append(section[cursor:match.start()])
        block_end = starts[i+1].start() if i+1 < len(starts) else len(section)
        block = section[match.start():block_end]
        ticker_values = set(re.findall(r"\(([A-Z0-9.^=!-]+(?:\.[A-Z0-9]+)?)\)", block, re.I))
        ticker_values.update(re.findall(r"(?i)(?:Ticker|Asset/Ticker)\s*[:：=]\s*([A-Z0-9.^=!-]+)", block))
        matched_affected = sorted(t.upper() for t in ticker_values if t.upper() in affected)
        plain_block = re.sub(r"\*\*([^*]+):\*\*", r"\1:", block)
        existing_nontradable = re.search(
            r"(?i)(?:Regel[-/ ]*Datenstatus|Regelstatus|Datenstatus)\s*[:：=]\s*NICHT HANDELBAR",
            plain_block,
        )
        if matched_affected and not existing_nontradable:
            status_pattern = re.compile(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Regel[-/ ]*Datenstatus|Regelstatus|Datenstatus)\s*[:：=].*$"
            )
            note = "OHLCV für " + ", ".join(matched_affected) + " nicht aktuell/vollständig; Setup bis zur Datenprüfung nicht handelbar"
            if status_pattern.search(plain_block):
                lines = block.splitlines()
                for line_index, line in enumerate(lines):
                    plain_line = re.sub(r"\*\*([^*]+):\*\*", r"\1:", line)
                    if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Regel[-/ ]*Datenstatus|Regelstatus|Datenstatus)\s*[:：=]", plain_line):
                        prefix = re.split(r"[:：=]", plain_line, maxsplit=1)[0].strip().lstrip("-*• ").strip()
                        lines[line_index] = "  - " + prefix + ": NICHT HANDELBAR"
                        break
                block = "\n".join(lines)
            else:
                block = block.rstrip() + "\n  - Regel-/Datenstatus: NICHT HANDELBAR\n"
            if "OHLCV-Datenhinweis:" not in block:
                block = block.rstrip() + "\n  - OHLCV-Datenhinweis: " + note + "\n"
        chunks.append(block)
        cursor = block_end
    chunks.append(section[cursor:])
    new_section = "".join(chunks)
    return report_text[:start] + new_section + report_text[end:]



def validate_ohlcv_audit(audit: Mapping[str, Any]) -> list[str]:
    """Validate audit integrity; universe-wide gaps are disclosed, not an automatic report veto.

    Candidate-specific consequences belong to the candidate's own tradeability status.
    """
    findings = []
    universe, covered = audit.get("universe_count"), audit.get("covered_count")
    if not isinstance(universe, int) or isinstance(universe, bool) or not isinstance(covered, int) or isinstance(covered, bool):
        return ["OHLCV-Prüfung ohne gültige Universums-/Abdeckungszahlen"]
    if universe < 0 or covered < 0 or covered > universe:
        return ["Widersprüchliche OHLCV-Abdeckungszahlen"]
    status = str(audit.get("status", "unavailable")).casefold()
    if status == "unavailable":
        return [str(audit.get("error") or "OHLCV-Datenbank oder Audit nicht verfügbar")]
    if status == "complete" and covered != universe:
        findings.append("OHLCV-Status complete widerspricht den aktuellen Abdeckungszahlen")
        findings.append(f"OHLCV-Abdeckung nicht vollständig: {covered}/{universe} Symbole abgedeckt")
        return findings
    if covered < universe or status in {"gaps", "partial", "incomplete"}:
        # Universe-Lücken dürfen nur dann rein dokumentarisch bleiben, wenn
        # das Audit eine tatsächlich nichtleere, verwertbare Kandidatenbewertung
        # mitliefert. Ein vorhandener Schlüssel mit []/None/falschem Typ ist
        # kein Nachweis, dass Kandidatenfolgen geprüft wurden.
        affected_candidates = audit.get("affected_candidates")
        has_candidate_assessment = (
            isinstance(affected_candidates, (list, tuple, set))
            and any(
                (isinstance(item, str) and item.strip())
                or (
                    isinstance(item, Mapping)
                    and any(
                        isinstance(item.get(key), str) and item.get(key).strip()
                        for key in ("ticker", "symbol", "asset", "candidate")
                    )
                )
                for item in affected_candidates
            )
        )
        if not has_candidate_assessment:
            findings.append(f"OHLCV-Abdeckung nicht vollständig: {covered}/{universe} Symbole abgedeckt")
            findings.append("OHLCV-Kandidatenfolgen fehlen: affected_candidates ist leer, ungültig oder enthält keine verwertbare Kandidatenbewertung")
    # Kandidatenbezogene Auswirkungen werden zusätzlich einzeln markiert; eine
    # globale Datenlücke darf nicht dazu führen, dass betroffene Kandidaten
    # stillschweigend als handelbar erscheinen.
    return findings
