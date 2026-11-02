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
    """Append source-stamped live market data and a common-cutoff 1M/3M matrix."""
    import pandas as pd
    if fetcher is None:
        try:
            import yfinance as yf
        except ImportError:
            return report_text, ["Markt-Snapshot nicht erstellt: yfinance nicht installiert"]
        def fetcher(symbols):
            return yf.download(tickers=symbols, period="6mo", interval="1d",
                               auto_adjust=True, group_by="ticker", progress=False, threads=True)
    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
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
            values = values.copy()
            values.index = pd.to_datetime(values.index, utc=True, errors="coerce")
            values = values[~values.index.isna()].sort_index()
            return values[~values.index.duplicated(keep="last")]
        except (KeyError, TypeError, AttributeError):
            return None

    def latest(symbol):
        values = series_for(symbol)
        try:
            return float(values.iloc[-1]) if values is not None else None
        except (ValueError, TypeError, IndexError):
            return None

    def pct(symbol, periods):
        values = series_for(symbol)
        if values is None or len(values) <= periods:
            return None
        try:
            base = float(values.iloc[-1-periods])
            return (float(values.iloc[-1]) / base - 1) * 100 if base else None
        except (ValueError, TypeError, ZeroDivisionError):
            return None

    def fmt(value, suffix=""):
        return f"{value:+.2f}{suffix}" if value is not None else "NICHT VERFÜGBAR"

    now = datetime.now().astimezone().astimezone(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat()
    lines = ["", "**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**",
             f"- Datenstand UTC: {now}"]
    for symbol, label in (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"), ("^RUT", "Russell 2000")):
        close, r1, r3 = latest(symbol), pct(symbol, 21), pct(symbol, 63)
        lines.append(
            f"- Index {label} ({symbol}): letzter Schluss {close:.2f}; 1M {fmt(r1, '%')}; 3M {fmt(r3, '%')}."
            if close is not None else f"- Index {label} ({symbol}): NICHT VERFÜGBAR."
        )
    vix, dxy, tnx_raw, tlt = latest("^VIX"), latest("DX-Y.NYB"), latest("^TNX"), pct("TLT", 21)
    tnx = (tnx_raw / 10.0 if abs(tnx_raw) >= 10.0 else tnx_raw) if tnx_raw is not None else None
    tnx_series = series_for("^TNX")
    tnx_observation_date = (
        tnx_series.index[-1].isoformat() if tnx_series is not None and len(tnx_series)
        else "NICHT VERFÜGBAR"
    )
    lines.extend([
        f"- Volatilität VIX: {vix:.2f}" if vix is not None else "- Volatilität VIX: NICHT VERFÜGBAR.",
        f"- Dollar-Proxy DXY (DX-Y.NYB): {dxy:.2f}" if dxy is not None else "- Dollar-Proxy DXY: NICHT VERFÜGBAR.",
        (
            f"- US-10J-Rendite (^TNX): {tnx:.2f}% | Quelle: Yahoo Finance (^TNX); "
            f"Abrufzeit: {now}; Datenstichtag: {tnx_observation_date}; Einheit: %; "
            "Verzögerung: Tages-Schlusskurs; keine Intraday-Aktualität im gelieferten Snapshot."
        ) if tnx is not None else "- US-10J-Rendite: NICHT VERFÜGBAR.",
        f"- Anleihe-ETF TLT 1M: {fmt(tlt, '%')}.",
    ])

    # Keep the quantitative breadth observation inside the canonical Market-First
    # field as well as in the live-evidence appendix; the validator intentionally
    # does not borrow numbers from unrelated sections.
    sector_etfs = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    above = total = 0
    for symbol in sector_etfs:
        values = series_for(symbol)
        if values is not None and len(values) >= 50:
            total += 1
            above += int(float(values.iloc[-1]) > float(values.iloc[-50:].mean()))
    lines.append(f"- Marktbreite-Proxy: {above}/{total} Sektor-ETFs über ihrem 50-Tage-Durchschnitt."
                 if total else "- Marktbreite-Proxy: NICHT VERFÜGBAR.")
    if total:
        breadth_fact = f" Quantitative Marktbreite: {above}/{total} Sektor-ETFs über ihrem 50-Tage-Durchschnitt."
        report_text = re.sub(
            r"(?im)^(\s*(?:[-*•]\s*)?(?:\*\*)?Indizes/Marktbreite(?:\*\*)?\s*:\s*)([^\r\n]*)$",
            lambda m: m.group(1) + m.group(2).rstrip(". ") + ";" + breadth_fact,
            report_text,
            count=1,
        )
    lines.append("- Quelle: Yahoo Finance (Ticker in den Zeilen; Tages-Schlusskurse).")

    # M-07: when the narrative contains a different 10Y Treasury value, never
    # silently overwrite it. Emit a concrete open conflict with both values
    # and provenance limits; the report gate will block FINAL until reconciled.
    macro_observations = []
    for source_line in report_text.splitlines():
        if "Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose)" in source_line:
            continue
        match = re.search(
            r"(?i)(?:10Y|10J|10[- ]?year|10-jährige[rn]?)[^\r\n]{0,100}?(?:US[- ]?)?Treasury[^\r\n]{0,60}?(\d+(?:[,.]\d+)?)\s*%",
            source_line,
        )
        if match:
            try:
                macro_observations.append((float(match.group(1).replace(",", ".")), source_line.strip()))
            except ValueError:
                pass
    if tnx is not None:
        distinct_macro = sorted({round(value, 6) for value, _ in macro_observations})
        differing = [value for value in distinct_macro if abs(value - tnx) >= 0.001]
        if differing:
            macro_value = differing[0]
            lines.append(
                f"- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
                f"Makro-Wert: {macro_value:.4f}% (Quellzeile vorhanden; Quelle/Zeitstempel nicht eindeutig zuordenbar) | "
                f"Live-Wert: {tnx:.4f}% (Quelle: Yahoo Finance ^TNX; Abruf: {now}) | "
                "Abgleich: nicht aufgelöst; keine der Beobachtungen wird überschrieben."
            )

    groups = {
        "Indizes": [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^RUT", "Russell 2000")],
        "Regionen": [("SPY", "USA"), ("VGK", "Europa"), ("EEM", "Emerging Markets"), ("EWJ", "Japan")],
        "Sektoren": [(x, x) for x in sector_etfs],
        "Anlageklassen": [("GLD", "Gold"), ("SLV", "Silber"), ("USO", "Öl"),
                          ("TLT", "US-Anleihen"), ("UUP", "US-Dollar"), ("BTC-USD", "Bitcoin")],
    }
    all_series = {sym: series_for(sym) for rows in groups.values() for sym, _ in rows}
    # Volatility and dollar observations are part of the same regime snapshot,
    # so they participate in (and must match) the common observation cutoff.
    all_series["^VIX"] = series_for("^VIX")
    all_series["DX-Y.NYB"] = series_for("DX-Y.NYB")
    all_series = {sym: ser for sym, ser in all_series.items() if ser is not None and not ser.empty}
    common_cutoff = min((ser.index.max() for ser in all_series.values()), default=None)
    if common_cutoff is not None:
        def close_at_common_cutoff(symbol):
            series = all_series.get(symbol)
            if series is None:
                return None
            eligible = series.loc[series.index <= common_cutoff]
            if eligible.empty or eligible.index[-1] != common_cutoff:
                return None
            try:
                return float(eligible.iloc[-1])
            except (TypeError, ValueError):
                return None
        vix = close_at_common_cutoff("^VIX")
        dxy = close_at_common_cutoff("DX-Y.NYB")
        for index, line in enumerate(lines):
            if line.startswith("- Volatilität VIX:"):
                lines[index] = f"- Volatilität VIX: {vix:.2f}" if vix is not None else "- Volatilität VIX: NICHT VERFÜGBAR."
            elif line.startswith("- Dollar-Proxy DXY"):
                lines[index] = f"- Dollar-Proxy DXY (DX-Y.NYB): {dxy:.2f}" if dxy is not None else "- Dollar-Proxy DXY: NICHT VERFÜGBAR."
    # M-02 consumes the same real market-data cutoff as the relative-strength
    # matrix. Retrieval time is not substituted for an observation date.
    if common_cutoff is not None:
        cutoff_date = common_cutoff.date().isoformat()
        report_text = re.sub(
            r"(?im)^\s*Marktregime-Datenstichtag\s*:[^\r\n]*\n?", "", report_text
        )
        regime_heading = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?Marktregime(?:\*\*)?\s*:",
            report_text,
        )
        if regime_heading:
            line_end = report_text.find("\n", regime_heading.start())
            if line_end < 0:
                line_end = len(report_text)
            report_text = (
                report_text[:line_end] + "\nMarktregime-Datenstichtag: " + cutoff_date
                + report_text[line_end:]
            )
    # The report gate deliberately validates the canonical risk field, not a
    # detached appendix. Copy only the measured VIX/DXY values into that field.
    if vix is not None or dxy is not None:
        def enrich_risk_line(match):
            original = match.group(0)
            suffix = []
            if vix is not None:
                suffix.append(f"VIX: {vix:.2f}")
            else:
                suffix.append("VIX: NICHT VERFÜGBAR")
            if dxy is not None:
                suffix.append(f"DXY: {dxy:.2f}")
            else:
                suffix.append("DXY: NICHT VERFÜGBAR")
            # Keep the original M2/liquidity narrative while replacing only
            # the broken entity-resolver placeholder for VIX.
            original = re.sub(r"(?i)Name nicht aufgelöst\s*\(VIX\)", "VIX", original)
            original = re.sub(r"(?i)Name nicht aufgeloest\s*\(VIX\)", "VIX", original)
            return original.rstrip(". ") + "; " + "; ".join(suffix)
        report_text = re.sub(
            r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?Dollar/Liquidität/Volatilität(?:\*\*)?\s*:[^\r\n]*$",
            enrich_risk_line, report_text, count=1,
        )
    matrix_results = {}
    matrix_lines = ["Relative-Stärke-Matrix (adjusted close; 1M=21 Handelstage, 3M=63 Handelstage):"]
    matrix_lines.append(f"Gemeinsamer Datenstichtag: {common_cutoff.isoformat()}" if common_cutoff is not None
                        else "Gemeinsamer Datenstichtag: NICHT VERFÜGBAR")
    for group, rows in groups.items():
        row_text = []
        for sym, label in rows:
            ser = all_series.get(sym)
            eligible = ser.loc[ser.index <= common_cutoff] if ser is not None and common_cutoff is not None else None
            # A common cutoff is only comparable when every included series has
            # an observation exactly at that cutoff. Do not silently substitute
            # an older last close for a missing market session.
            if (eligible is None or eligible.empty or common_cutoff is None
                    or eligible.index[-1] != common_cutoff or len(eligible) <= 63):
                row_text.append(
                    f"{label} ({sym}) 1M NICHT VERFÜGBAR (kein Kurs exakt am gemeinsamen Stichtag); "
                    "3M NICHT VERFÜGBAR (kein Kurs exakt am gemeinsamen Stichtag)"
                )
                continue
            last = float(eligible.iloc[-1])
            base1, base3 = float(eligible.iloc[-22]), float(eligible.iloc[-64])
            if last <= 0 or base1 <= 0 or base3 <= 0:
                row_text.append(f"{label} ({sym}) 1M NICHT VERFÜGBAR (Datenlücke); 3M NICHT VERFÜGBAR (Datenlücke)")
                continue
            r1, r3 = (last/base1-1)*100, (last/base3-1)*100
            matrix_results[sym] = (r1, r3)
            row_text.append(f"{label} ({sym}) 1M {r1:+.2f}%; 3M {r3:+.2f}%")
        matrix_lines.append(f"{group}: " + " | ".join(row_text))
    if matrix_results:
        ranked = sorted(matrix_results.items(), key=lambda item: item[1][1])
        matrix_lines.extend([
            "Gewinner: " + ", ".join(f"{sym} ({v[1]:+.2f}% 3M)" for sym, v in ranked[-3:]),
            "Verlierer: " + ", ".join(f"{sym} ({v[1]:+.2f}% 3M)" for sym, v in ranked[:3]),
            f"Divergenz: {max(v[1] for v in matrix_results.values())-min(v[1] for v in matrix_results.values()):.2f} Prozentpunkte (3M-Spannweite)",
        ])
    else:
        matrix_lines.extend([
            "Gewinner: NICHT VERFÜGBAR (Datenlücke)",
            "Verlierer: NICHT VERFÜGBAR (Datenlücke)",
            "Divergenz: NICHT VERFÜGBAR (Datenlücke)",
        ])
    block = "\n".join(lines) + "\n"
    matrix_block = "\n".join(matrix_lines) + "\n"

    # Idempotent: remove prior live block and prior matrix before insertion.
    report_text = re.sub(
        r"(?ms)\n\*\*Live-Marktbelege \(Yahoo Finance; Schlusskurse, keine Prognose\):\*\*"
        r".*?(?=\n[ \t]*(?:#{1,6}[ \t]*)?2[ \t]*[.·][ \t]*Thesen-Update\b[^\r\n]*$)",
        "", report_text,
    )
    report_text = re.sub(
        r"(?ms)\nRelative-Stärke-Matrix \(adjusted close;.*?(?=\n[ \t]*(?:#{1,6}[ \t]*)?5[ \t]*[.·][ \t]*Technische Kandidaten\b)",
        "", report_text,
    )
    section_two_heading = r"^[ \t]*(?:#{1,6}[ \t]*)?2[ \t]*[.·][ \t]*Thesen-Update\b[^\r\n]*$"
    insertion = re.search(section_two_heading, report_text, re.I | re.M)
    if not insertion:
        return report_text, ["Markt-Snapshot vorhanden, aber Abschnitt 2 für Einfügung nicht auffindbar"]
    report_text = report_text[:insertion.start()] + block + "\n" + report_text[insertion.start():]
    section4 = re.search(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?4[ \t]*[.·][ \t]*Assetklassenvergleich[^\r\n]*$", report_text)
    section5 = re.search(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?5[ \t]*[.·][ \t]*Technische Kandidaten[^\r\n]*$", report_text)
    if section4 and section5 and section4.start() < section5.start():
        report_text = report_text[:section5.start()] + "\n" + matrix_block + "\n" + report_text[section5.start():]
    return report_text, []



def _verify_fomc_official_source(event_date: date, *, requester=None) -> tuple[bool, str]:
    """Verify the FOMC date/time against a live, official Federal Reserve page.

    Try the month view first and the same official page without its view query
    second. A failed first URL must not prevent verification when the canonical
    page is reachable. No stale/local calendar is used as proof of live
    verification. All errors are retained in the returned diagnostic.
    """
    month_slug = f"{event_date:%Y}-{event_date.strftime('%B').lower()}"
    urls = [
        f"https://www.federalreserve.gov/newsevents/{month_slug}.htm?view=calendar",
        f"https://www.federalreserve.gov/newsevents/{month_slug}.htm",
    ]
    if requester is None:
        import requests
        requester = requests.get

    errors = []
    responses = []
    for url in urls:
        try:
            response = requester(
                url, timeout=12,
                headers={"User-Agent": "TradingRadar/1.1 (official-event-verification)"},
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            final_url = str(getattr(response, "url", url) or url)
            if status_code != 200:
                errors.append(f"{url} -> HTTP {status_code}")
                continue
            if not re.match(r"^https://www\.federalreserve\.gov/newsevents/", final_url, re.I):
                errors.append(f"{url} -> unzulässige Weiterleitungsdomain: {final_url}")
                continue
            html = str(getattr(response, "text", "") or "")
            if not html.strip():
                errors.append(f"{url} -> HTTP 200, aber leerer Antworttext")
                continue
            responses.append((url, final_url, html))
        except Exception as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")

    def plain_text(fragment: str) -> str:
        fragment = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", " ", fragment)
        fragment = re.sub(r"(?is)<[^>]+>", " ", fragment)
        return re.sub(r"\s+", " ", fragment).casefold()

    month = event_date.strftime("%B").casefold()
    day = str(event_date.day)
    year = str(event_date.year)
    # The Fed's month page often represents the release date as a bare day
    # number and the meeting range as "Two-day meeting, October 27 - 28".
    # Validate the year/month from the official page title/URL and the target
    # day from that bounded FOMC Meetings category, rather than requiring the
    # year to be repeated in every child element.
    full_date_pattern = re.compile(
        rf"\b{re.escape(month)}\s+(?:\d{{1,2}}\s*[-–]\s*)?{re.escape(day)}"
        rf"(?:\s*[-–]\s*\d{{1,2}})?[,]?\s+{re.escape(year)}\b"
    )
    range_pattern = re.compile(
        rf"\b{re.escape(month)}\s+\d{{1,2}}\s*[-–]\s*{re.escape(day)}\b"
    )
    bare_day_pattern = re.compile(rf"(?<!\d){re.escape(day)}(?!\d)")

    for requested_url, final_url, html in responses:
        blocks = re.findall(r"(?is)<tr\b[^>]*>.*?</tr\s*>", html)
        if not blocks:
            blocks = re.findall(r"(?is)<li\b[^>]*>.*?</li\s*>", html)
        matching_blocks = []
        for block in blocks:
            block_text = plain_text(block)
            if (
                (full_date_pattern.search(block_text) or range_pattern.search(block_text))
                and re.search(r"\bfomc\b", block_text)
                and re.search(r"\b2\s*:\s*00\s*p\.?m\.?\b", block_text)
            ):
                matching_blocks.append(block_text)

        page_text = plain_text(html)
        if not matching_blocks:
            group_match = re.search(
                r"\bfomc meetings\b(.*?)(?=\bbeige book\b|\bstatistical releases\b|\bspeeches\b|\btestimony\b|\bother\b)",
                page_text,
            )
            if group_match:
                group_text = re.sub(r"\s+", " ", group_match.group(1)).strip()
                # Confirm page context identifies the correct month and year.
                page_context_ok = (
                    month in page_text and year in page_text
                    and re.search(rf"\b{re.escape(year)}\b", page_text)
                )
                target_day_ok = bool(
                    full_date_pattern.search(group_text)
                    or range_pattern.search(group_text)
                )
                if (
                    page_context_ok and target_day_ok
                    and re.search(r"\bfomc\b", group_text)
                    and re.search(r"\b2\s*:\s*00\s*p\.?m\.?\b", group_text)
                ):
                    matching_blocks.append(group_text)

        if matching_blocks:
            press_verified = any(
                re.search(r"\b2\s*:\s*30\s*p\.?m\.?\b", block)
                for block in matching_blocks
            )
            detail = (
                f"Datum, FOMC und 14:00 Uhr im abgegrenzten offiziellen Kalenderbereich bestätigt "
                f"({final_url}; Abruf: {requested_url})"
            )
            if press_verified:
                detail += "; PRESSEKONFERENZ_VERIFIZIERT"
            else:
                detail += "; Pressekonferenzzeit im Eintrag nicht separat bestätigt"
            if errors:
                detail += "; vorherige Abrufversuche fehlgeschlagen: " + " | ".join(errors)
            return True, detail

        errors.append(f"{requested_url} -> HTTP 200, aber kein passender FOMC-Eintrag für {event_date.isoformat()}")

    diagnostic = " | ".join(errors) if errors else "keine offizielle Kalenderantwort erhalten"
    return False, f"Fed-Kalenderprüfung fehlgeschlagen; {diagnostic}"


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
    # Enrich only an undated FOMC mention. If the report already contains an
    # explicit event date, preserve that record and its verification state:
    # enrichment must not silently replace a dated, unverified event with the
    # next calendar entry and thereby erase the original unknowns.
    fomc_has_explicit_date = bool(
        re.search(r"(?is)\bFOMC\b.{0,1200}?^\s*(?:[-*•]\s*)?(?:Datum|Date)\s*[:=]\s*\d{4}-\d{2}-\d{2}", body, re.MULTILINE)
    )
    if not fomc_has_explicit_date:
        body = _enrich_fomc_event(body, report_text)
    # A record starts at a bullet or numbered line that is not a field label.
    lines = body.splitlines()
    # Markdown labels (including "- **Quelle:**") are continuation fields,
    # not new event records. Strip formatting before classifying each line.
    field_labels = (
        "Ereignis-Uhrzeit|Ereignis Uhrzeit|Datum|Date|Uhrzeit|Zeit|Zeitzone|"
        "Kennzahl|Indikator|Berichtszeitraum|Zeitraum|Vorwert|Letzter Wert|"
        "Konsens|Istwert|Quelle|Source|Betroffene These/Assetklasse/konkrete offene Positionen|"
        "Szenarien(?:\\s+(?:unter/entsprechend/über|unter|entsprechend|über|ueber)\\s+Konsens)?|"
        "Reaktionskanäle|Bestätigung/Widerlegung|Bestaetigung/Widerlegung|"
        "Was die These bestätigen/widerlegen würde|Quellenprüfung|Quellenpruefung"
    )
    field = re.compile(rf"(?i)^\s*(?:{field_labels})\s*[:=]")
    def is_field_line(line: str) -> bool:
        normalized = re.sub(r"^\s*[-*•]\s*", "", line).strip()
        normalized = normalized.replace("**", "").replace("__", "")
        return bool(field.match(normalized))
    starts = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.+)$", line)
        is_event_header = bool(m and re.match(r"(?i)^Ereignis[- ]Uhrzeit\s*[:=]", m.group(1).strip()))
        if m and (is_event_header or not is_field_line(line)):
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
        if not date_m:
            header_date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", plain_rec.splitlines()[0] if plain_rec.splitlines() else "")
            if header_date:
                date_m = header_date
        source_m = re.search(
            r"(?im)(?:^|[|;])\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=]\s*([^\r\n|;]+)",
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
        source_value = source_m.group(1).strip() if source_m else ""
        if not source_value and len(records) == 1:
            # In a single-event section, tolerate a source line retained in
            # the section body even if malformed markdown confused record slicing.
            fallback_source = re.search(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=]\s*([^\r\n|;]+)",
                re.sub(r"\*\*([^*]+)\*\*", r"\1", body),
            )
            source_value = fallback_source.group(1).strip() if fallback_source else ""
        source_is_placeholder = bool(re.fullmatch(
            r"(?i)(?:NICHT VERFÜGBAR(?:\s*\([^)]*\))?|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR)",
            source_value,
        ))
        source_has_evidence = bool(re.search(
            r"(?i)(?:https?://|forexfactory|\bfred\b|federal reserve|official|"
            r"\bbls\b|bureau of labor|\bbea\b|\becb\b|\beia\b|"
            r"\bism\b|\badp\b|\bcme\b|\bsec\b|treasury|"
            r"opec|world bank|imf|yahoo finance|trading economics)",
            source_value,
        ))
        source_present = bool(source_value and not source_is_placeholder and source_has_evidence)
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



def validate_ohlcv_audit(audit: Mapping[str, Any], *, explicit_no_candidates: bool = False) -> list[str]:
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
        has_affected_rows = (
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
        # A complete candidate mapping with an explicitly empty affected list is
        # a valid assessment: the named candidates were checked and none overlaps
        # current missing/stale/recent-invalid OHLCV symbols. Universe-wide gaps
        # remain disclosed; they must not block unrelated, fully mapped candidates.
        checked = audit.get("candidate_tickers_checked")
        unmapped = audit.get("unmapped_candidate_tickers")
        has_complete_zero_impact_assessment = (
            audit.get("candidate_mapping_complete") is True
            and isinstance(checked, (list, tuple, set))
            and any(isinstance(item, str) and item.strip() for item in checked)
            and isinstance(unmapped, (list, tuple, set))
            and not any(isinstance(item, str) and item.strip() for item in unmapped)
            and isinstance(affected_candidates, (list, tuple, set))
            and len(affected_candidates) == 0
        )
        has_candidate_assessment = has_affected_rows or has_complete_zero_impact_assessment
        if not has_candidate_assessment and not explicit_no_candidates:
            if covered < universe:
                findings.append(
                    f"OHLCV-Symbolabdeckung nicht vollständig: {covered}/{universe} Symbole abgedeckt"
                )
            else:
                # Symbol coverage and candle quality are separate dimensions:
                # all symbols may exist while stale/invalid/gapped history still
                # requires disclosure. Never call 862/862 "not fully covered".
                findings.append(
                    f"OHLCV-Datenqualitätsstatus '{status}' trotz Symbolabdeckung "
                    f"{covered}/{universe}; Datenlücken/Veraltung/ungültige Kerzen separat prüfen"
                )
            findings.append(
                "OHLCV-Kandidatenfolgen fehlen: affected_candidates ist leer/ungültig "
                "und es liegt kein vollständiger Kandidaten-Mapping-Nachweis vor"
            )
    # Kandidatenbezogene Auswirkungen werden zusätzlich einzeln markiert; eine
    # globale Datenlücke darf nicht dazu führen, dass betroffene Kandidaten
    # stillschweigend als handelbar erscheinen.
    return findings


def report_explicitly_declares_no_technical_candidates(report_text: str) -> bool:
    """Detect a plain-language zero-candidate declaration without guessing from an empty parse."""
    if not isinstance(report_text, str):
        return False
    section = re.search(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*\r?\n"
        r"(.*?)(?=^[ \t]*(?:#{1,6}\s*)?6(?:\s*[.·](?!\d)|\s+)|\Z)", report_text
    )
    if not section:
        return False
    body = section.group(1)
    explicit = re.search(
        r"(?i)keine belastbaren technischen kandidaten|keine technischen kandidaten|"
        r"keine kandidaten|kein kandidat|keine handelsfreigabe", body
    )
    structured_candidate = re.search(
        r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?(?:Asset/Ticker|Ticker|Asset)\s*[:=]", body
    )
    return bool(explicit and not structured_candidate)


def report_has_explicit_empty_candidate_groups(report_text: str) -> bool:
    """Return True only when all four required candidate groups explicitly declare zero records.

    This is deliberately stricter than a plain-language no-candidate sentence:
    the report must contain section 5, all four required groups exactly once,
    each group must explicitly be empty, and no candidate record may appear.
    """
    if not isinstance(report_text, str):
        return False
    section_match = re.search(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*\r?\n"
        r"(.*?)(?=^[ \t]*(?:#{1,6}\s*)?6(?:\s*[.·](?!\d)|\s+)|\Z)",
        report_text,
    )
    if not section_match:
        return False
    body = section_match.group(1)
    required_groups = (
        "JETZT TECHNISCH PRÜFEN",
        "AUF TRIGGER WARTEN",
        "EREIGNIS ABWARTEN",
        "NICHT HANDELBAR",
    )
    group_pattern = re.compile(
        r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:[-*•][ \t]*)?(?:\*\*)?("
        + "|".join(re.escape(group) for group in sorted(required_groups, key=len, reverse=True))
        + r")(?:\*\*)?[ \t]*[:：][ \t]*([^\r\n]*)$"
    )
    matches = list(group_pattern.finditer(body))
    if len(matches) != len(required_groups):
        return False
    by_group = {}
    for match in matches:
        group = re.sub(r"\s+", " ", match.group(1)).strip().upper()
        by_group.setdefault(group, []).append(match)
    if set(by_group) != set(required_groups) or any(len(v) != 1 for v in by_group.values()):
        return False
    if re.search(
        r"(?im)^[ \t]*(?:[-*•][ \t]*)?(?:\*\*)?(?:Asset/Ticker|Ticker|Asset)(?:\*\*)?[ \t]*[:=]",
        body,
    ):
        return False
    empty_markers = re.compile(
        r"(?i)^(?:keine kandidaten(?: in dieser gruppe)?|0 kandidaten|explizit leer|leer)[ \t.!—-]*$"
    )
    for match in matches:
        group_body_start = match.end()
        later = [m.start() for m in matches if m.start() > match.start()]
        group_body_end = min(later) if later else len(body)
        group_content = body[group_body_start:group_body_end].strip()
        # The inline heading marker is the preferred canonical form. For a
        # multiline section, accept a single explicit empty marker and nothing else.
        if not empty_markers.fullmatch(match.group(2).strip()):
            if not empty_markers.fullmatch(group_content):
                return False
    return True
