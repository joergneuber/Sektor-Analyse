"""Equal-weighted arithmetic mean of one-unit trade returns in percent.

Each unique trade contributes exactly one share/unit:
- closed trades: entry to exit price
- open trades: entry to current price
- long and short directions are handled explicitly
- costs are zero
- currencies are deliberately ignored because each trade return is calculated
  as a percentage in its own quote currency; individual trade percentages are averaged equally.

This is a trade-return basket metric, not a capital-weighted portfolio return.
"""
from __future__ import annotations

import csv
import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Iterable


CLOSED = {"verkauft", "gestoppt", "geschlossen", "closed", "sold", "stopped"}
OPEN = {"offen", "open", "aktiv", "active"}
LONG = {"long", "buy", "kauf", "long-position"}
SHORT = {"short", "sell", "verkauf", "leer", "short-position"}
# EUNL.DE is the externally selected MSCI World comparison instrument in this
# Trading-Radar project, not a system-generated trade. It must never dilute
# the equal-weighted system-trade result. Other rows can be explicitly marked
# as system trades via Portfolio_Rolle/Trade_Rolle if they share this ticker.
EXTERNAL_BENCHMARK_TICKERS = {"EUNL.DE"}
BENCHMARK_ROLE_VALUES = {"benchmark", "benchmarkposition", "benchmark_position", "vergleichsindex", "vergleichsposition"}


def _num(value):
    raw = str(value or "").strip().replace("%", "")
    if not raw:
        return None
    raw = raw.replace(" ", "").replace("’", "").replace("'", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        number = float(raw)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _date_key(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _read_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        first = next((line for line in sample.splitlines() if line.strip()), "")
        delimiter = max((";", ",", "\t", "|"), key=lambda d: first.count(d))
        return list(csv.DictReader(handle, delimiter=delimiter))


def calculate_portfolio_performance(source_paths: Iterable[str | Path]) -> dict:
    """Average individual percentage returns for all unique open and closed one-unit trades."""
    chosen: dict[tuple[str, ...], dict] = {}
    source_names: list[str] = []
    source_row_counts: dict[str, int] = {}
    issues: list[str] = []
    duplicate_conflicts: list[str] = []
    benchmark_excluded: set[str] = set()
    benchmark_excluded_ids: set[tuple[str, ...]] = set()
    rows_seen = 0

    for raw_path in source_paths:
        path = Path(raw_path)
        if not path.is_file():
            issues.append(f"Quelle fehlt: {path.name}")
            continue
        if path.suffix.lower() not in {".csv", ".tsv"}:
            continue
        try:
            rows = _read_rows(path)
        except (OSError, csv.Error, UnicodeError) as exc:
            issues.append(f"{path.name}: Lesefehler {type(exc).__name__}")
            continue
        source_names.append(path.name)
        source_row_counts[path.name] = 0
        seen_in_source: set[tuple[str, ...]] = set()
        for row_no, row in enumerate(rows, start=2):
            if not any(str(v or "").strip() for v in row.values()):
                continue
            rows_seen += 1
            source_row_counts[path.name] += 1
            norm = {str(k or "").strip().casefold(): v for k, v in row.items()}

            def get(*names):
                for name in names:
                    value = norm.get(name.casefold())
                    if value is not None and str(value).strip():
                        return str(value).strip()
                return ""

            ticker = get("Ticker", "Symbol").upper()
            role_raw = get("Portfolio_Rolle", "Portfolio Rolle", "Trade_Rolle", "Trade Rolle", "Positionstyp", "Rolle").casefold().strip()
            benchmark_flag = get("Ist_Benchmark", "Benchmarkposition", "Is_Benchmark").casefold().strip()
            explicitly_benchmark = (role_raw in BENCHMARK_ROLE_VALUES or benchmark_flag in {"1", "true", "ja", "yes", "y"})
            date_raw = get("Einstiegsdatum", "Kaufdatum", "Entry Date")
            entry_date = _date_key(date_raw)
            entry = _num(get("Einstieg", "Einstiegskurs", "Kaufkurs", "Entry"))
            status_raw = get("Status").casefold()
            direction_raw = get("Richtung", "Side").casefold()
            trade_id = get("Trade_ID", "Trade ID", "TradeID", "Position_ID", "Position ID").strip()
            if explicitly_benchmark or ticker in EXTERNAL_BENCHMARK_TICKERS:
                benchmark_excluded.add(ticker or "[ohne Ticker]")
                benchmark_identity = trade_id.casefold() if trade_id else (
                    f"{entry:.12g}" if entry is not None else ""
                ) + "|" + direction_raw
                benchmark_excluded_ids.add((ticker, entry_date or "", benchmark_identity))
                continue

            # Prefer a stable trade identifier. If the source has none, use
            # ticker + entry date + entry price + direction. This prevents two
            # separate same-ticker/same-day entries at different prices from
            # being collapsed into one. Exact same-price collisions without an
            # ID remain ambiguous and fail closed.
            identity_tail = trade_id.casefold() if trade_id else (
                f"{entry:.12g}" if entry is not None else ""
            ) + "|" + direction_raw
            key = (ticker, entry_date or "", identity_tail)
            if key in seen_in_source:
                duplicate_conflicts.append(
                    f"{ticker}/{entry_date}: mehrfacher Eintrag derselben Trade-Identität innerhalb "
                    f"der Quelle {path.name}; ohne eindeutige Trade-ID nicht deduplizierbar"
                )
                continue
            if ticker and entry_date:
                seen_in_source.add(key)

            missing = []
            if not ticker: missing.append("Ticker")
            if not entry_date: missing.append("gültiges Einstiegsdatum")
            product_type = get("Produkt_Typ", "Produkt Typ", "Instrumententyp", "Product Type").casefold()
            is_option = any(token in product_type for token in ("optionsschein", "option", "warrant", "hebelprodukt"))
            if is_option:
                option_entry = _num(get("OS_Einstiegskurs", "OS Einstiegskurs", "Optionsschein_Einstieg"))
                if option_entry is None or option_entry <= 0:
                    missing.append("gültiger Optionsschein-Einstiegskurs")
            elif entry is None or entry <= 0:
                missing.append("gültiger Einstiegskurs")
            if status_raw not in CLOSED | OPEN: missing.append("gültiger Status")
            if direction_raw not in LONG | SHORT: missing.append("eindeutige Richtung")
            # A lower-priority export may contain a stale/incomplete duplicate
            # of a row already supplied with better fields by the preferred
            # source (e.g. raw open positions vs. the enriched Check export).
            if missing and ticker and entry_date and key in chosen and chosen[key]["source"] != path.name:
                continue
            if missing:
                issues.append(f"{path.name}:{row_no} {ticker or '[ohne Ticker]'}: fehlend/ungültig: {', '.join(missing)}")
                continue

            is_closed = status_raw in CLOSED
            product_type = get("Produkt_Typ", "Produkt Typ", "Instrumententyp", "Product Type").casefold()
            is_option = any(token in product_type for token in ("optionsschein", "option", "warrant", "hebelprodukt"))
            # For an option/warrant, the underlying share price is not the
            # traded instrument's return. Use the option's own percentage
            # return, which is already expressed per one unit of that trade.
            # If that quote is absent, fail closed instead of substituting the
            # underlying's performance.
            option_entry = _num(get("OS_Einstiegskurs", "OS Einstiegskurs", "Optionsschein_Einstieg")) if is_option else None
            mark = _num(get("OS_Ausstiegskurs", "OS_Ausstieg", "OS_Manueller_Kurs", "OS_Aktueller_Kurs")) if is_option else _num(
                get("Ausstiegskurs", "Verkaufskurs", "Exit") if is_closed
                else get("Aktueller_Kurs", "Aktueller Kurs", "Kurs aktuell", "Last", "Current Price")
            )
            # Der Optionsschein ist das gehandelte Instrument. Wenn eine
            # explizite OS-Performance fehlt, wird sie aus OS-Einstieg und
            # OS-Ausstiegs-/Marktkurs berechnet. Underlying-Kurse sind tabu.
            option_return = None
            if is_option and option_entry and option_entry > 0 and mark and mark > 0:
                option_return = (mark - option_entry) / option_entry * 100.0
            if is_option and option_return is None:
                issues.append(f"{path.name}:{row_no} {ticker}: Optionsschein-Einstiegs- oder Optionsschein-Ausstiegskurs fehlt/ungültig; Underlying-Rendite wird nicht ersatzweise verwendet")
                continue
            if not is_option and (mark is None or mark <= 0) and key in chosen and chosen[key]["source"] != path.name:
                # A lower-priority duplicate export may omit the current mark;
                # retain the already validated enriched record.
                continue
            if not is_option and (mark is None or mark <= 0):
                issues.append(f"{path.name}:{row_no} {ticker}: {'Ausstiegskurs' if is_closed else 'aktueller Kurs'} fehlt/ungültig")
                continue

            sign = -1.0 if direction_raw in SHORT else 1.0
            return_pct = option_return if is_option else sign * (mark - entry) / entry * 100.0
            # If the source also stores a performance percentage, reconcile it
            # against the independently reproducible price-derived return.
            # Never silently choose whichever number looks more favorable.
            stored_return_raw = get("OS_Performance%", "OS Performance%", "Optionsschein_Performance%") if is_option else get(
                "Performance_Seit_Einstieg%", "Performance Seit Einstieg%", "Performance%", "Rendite%"
            )
            stored_return = _num(stored_return_raw) if stored_return_raw else None
            if stored_return is not None and not math.isclose(
                stored_return, return_pct, rel_tol=0.0, abs_tol=0.15
            ):
                issues.append(
                    f"{path.name}:{row_no} {ticker}: gespeicherte Einzelrendite {stored_return:+.2f}% "
                    f"widerspricht der aus Einstieg und {'Optionsschein-' if is_option else ''}Kurs "
                    f"reproduzierten Rendite {return_pct:+.2f}% (Toleranz 0,15 Prozentpunkte)."
                )
            record = {
                "ticker": ticker, "entry_date": entry_date,
                "trade_identity_hash": hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16],
                "entry": (option_entry if is_option else entry),
                "identity_entry": entry, "mark": mark, "status": "CLOSED" if is_closed else "OPEN",
                "direction": "SHORT" if sign < 0 else "LONG", "sign": sign,
                "pnl_one_unit": (sign * (mark - option_entry) if is_option and mark is not None else (None if is_option else sign * (mark - entry))),
                "return_pct": return_pct,
                "unit": 1, "source": path.name, "row": row_no,
            }
            previous = chosen.get(key)
            if previous is None:
                chosen[key] = record
                continue

            if (not math.isclose(previous.get("identity_entry", previous["entry"]), entry, rel_tol=1e-9, abs_tol=1e-9)
                    or previous["direction"] != record["direction"]):
                duplicate_conflicts.append(
                    f"{ticker}/{entry_date}: widersprüchlicher Einstieg oder Richtung "
                    f"({previous['source']}:{previous['row']} vs {path.name}:{row_no})"
                )
                continue

            if previous["status"] != record["status"]:
                # A verified closed row supersedes an older open snapshot.
                if record["status"] == "CLOSED":
                    chosen[key] = record
                continue
            if not math.isclose(previous["mark"], mark, rel_tol=1e-9, abs_tol=1e-9):
                duplicate_conflicts.append(
                    f"{ticker}/{entry_date}: widersprüchliche Kursstände in mehreren Quellen "
                    f"({previous['source']}:{previous['row']} vs {path.name}:{row_no})"
                )

    records = list(chosen.values())
    closed = [r for r in records if r["status"] == "CLOSED"]
    opened = [r for r in records if r["status"] == "OPEN"]
    if not records:
        issues.append("Keine gültigen offenen/geschlossenen Trades gefunden.")
    if duplicate_conflicts:
        issues.extend(duplicate_conflicts)

    complete = bool(records) and not issues and not duplicate_conflicts
    portfolio_pct = (
        sum(r["return_pct"] for r in records) / len(records)
        if complete and records else None
    )
    # Auch der offene Positionsdurchschnitt ist nur bei vollständiger
    # Abdeckung aller offenen Datensätze ausweisbar. Keine Teilmengenmittel.
    average_open_pct = (
        sum(r["return_pct"] for r in opened) / len(opened)
        if complete and opened else None
    )
    return {
        "status": "AVAILABLE" if complete else "NOT_AVAILABLE",
        "portfolio_total_pct": portfolio_pct,
        "average_trade_return_pct": portfolio_pct,
        "average_position_pct": average_open_pct,
        "trade_count": len(records), "closed_count": len(closed), "open_count": len(opened),
        "rows_seen": rows_seen, "source_row_counts": source_row_counts,
        "unit_per_trade": 1, "costs": 0,
        "aggregation": "arithmetic_mean_of_individual_trade_return_percentages",
        "average_position_scope": "currently_open_trades_only",
        "currency_conversion": False, "currency_independent": True,
        "benchmark_excluded_tickers": sorted(benchmark_excluded),
        "benchmark_excluded_count": len(benchmark_excluded_ids),
        "sources": sorted(set(source_names)),
        "duplicate_conflicts": duplicate_conflicts, "issues": issues, "records": records,
    }


def format_performance_block(result: dict) -> str:
    def fmt(value):
        return f"{value:+.2f}".replace(".", ",") + " %"
    portfolio = fmt(result["portfolio_total_pct"]) if result["portfolio_total_pct"] is not None else "NICHT VERFÜGBAR"
    average = fmt(result["average_position_pct"]) if result["average_position_pct"] is not None else "NICHT VERFÜGBAR"
    detail_payload = ""
    detail_digest = "NICHT VERFÜGBAR"
    if result["status"] == "AVAILABLE":
        ordered = sorted(
            result["records"],
            key=lambda item: (item["ticker"], item["entry_date"], item["status"],
                              item["direction"], item["entry"], item["mark"]),
        )
        detail_payload = ";".join(
            "|".join((
                str(item["ticker"]), str(item["entry_date"]), str(item["trade_identity_hash"]),
                str(item["status"]), str(item["direction"]), f"{float(item['entry']):.10f}",
                f"{float(item['mark']):.10f}", f"{float(item['return_pct']):.10f}",
            ))
            for item in ordered
        )
        detail_digest = hashlib.sha256(detail_payload.encode("utf-8")).hexdigest()
    lines = [
        "Portfolio-Performance (Python-Berechnung):",
        f"Portfolio-Gesamtperformance: {portfolio}",
        f"Ø Positionsperformance: {average}",
        f"Performance-Datenbasis: {result['trade_count']} eindeutige Trades; abgeschlossen={result['closed_count']}; offen={result['open_count']}",
        "Berechnungsmethode: gleichgewichteten arithmetischen Mittelwert der individuellen prozentualen Renditen aller eindeutigen offenen und abgeschlossenen System-Trades; jeder Trade zählt genau einmal; genau 1 Stück je Trade; abgeschlossene Trades zum Ausstiegskurs; offene Trades zum aktuellen Kurs; Kosten=0; keine Kapitalgewichtung und keine Währungsumrechnung.",
        "Benchmarkpositionen ausgeschlossen: " + (", ".join(result.get("benchmark_excluded_tickers", [])) if result.get("benchmark_excluded_tickers") else "KEINE") + f" (Anzahl={result.get('benchmark_excluded_count', 0)}); externe Vergleichspositionen zählen nicht als System-Trades.",
        "Formel je Trade: Long=(Kurs-Einstieg)/Einstieg*100; Short=(Einstieg-Kurs)/Einstieg*100. Portfolio-Gesamtperformance=Summe der Einzelrenditen geteilt durch Anzahl eindeutiger Trades; Stückzahl 1; Kosten 0.",
        "Positionsdurchschnitt: arithmetisches Mittel ausschließlich der aktuell offenen Einzelrenditen; kein Ersatz für die Portfolio-Gesamtperformance.",
        "Performance-Einzelrenditen: " + (detail_payload or "NICHT VERFÜGBAR"),
        "Performance-Einzelrenditen-SHA256: " + detail_digest,
        "Datenquellen: " + (", ".join(result["sources"]) if result["sources"] else "NICHT VERFÜGBAR"),
    ]
    if result["issues"]:
        lines.append("Performance-Datenlücken: " + " | ".join(result["issues"]))
    return "\n".join(lines)
