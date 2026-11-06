#!/usr/bin/env python3
"""Read-only release-DB diagnosis for Paket 2 (GitHub Actions runner)."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import historical_ohlcv

ROOT = Path.cwd()
SOURCE_DB = ROOT / os.environ.get("NMM_HISTORICAL_OHLCV_DB", "historical_ohlcv.sqlite")
AUDIT_DB = ROOT / "historical_ohlcv_diagnostic_copy.sqlite"
REPORT = ROOT / "historical_ohlcv_release_diagnostic.json"
GAP_DETAIL_JSON = ROOT / "OHLCV_Gap_Ursachen.json"
GAP_DETAIL_CSV = ROOT / "OHLCV_Gap_Detail.csv"
GAP_DETAIL_MD = ROOT / "OHLCV_Gap_Report.md"
INVALID_DETAIL_CSV = ROOT / "OHLCV_Invalid_Detail.csv"
INVALID_SUMMARY_JSON = ROOT / "OHLCV_Invalid_Zusammenfassung.json"
MISSING_INSTRUMENTS_CSV = ROOT / "OHLCV_Missing_Instruments.csv"
QUALITY_REPORT_MD = ROOT / "OHLCV_Quality_Report.md"
GAP_LOOKBACK_DAYS = int(os.environ.get("NMM_HISTORICAL_OHLCV_GAP_LOOKBACK_DAYS", "365"))
YAHOO_PROBE_BATCH_SIZE = max(1, int(os.environ.get("NMM_HISTORICAL_OHLCV_YAHOO_PROBE_BATCH_SIZE", "20")))
SOURCE_FILE = ROOT / "analyse.py"
DYNAMIC_UNIVERSE = ROOT / os.environ.get(
    "NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE", "historical_ohlcv_universe.json"
)


def sha256_file(path: Path, chunk_size: int = 8 *  1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path) -> dict:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=180) as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        schema = {
            table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for table in tables
        }
        required = {
            "instruments": {"ticker", "row_count"},
            "bars": {"ticker", "date", "open", "high", "low", "close", "volume"},
        }
        missing = {
            table: sorted(columns - set(schema.get(table, [])))
            for table, columns in required.items()
            if columns - set(schema.get(table, []))
        }
        result = {
            "integrity_check": integrity,
            "integrity_ok": integrity == ["ok"],
            "tables": tables,
            "schema": schema,
            "missing_required_columns": missing,
        }
        if not missing:
            result["instrument_count"] = conn.execute(
                "SELECT COUNT(*) FROM instruments"
            ).fetchone()[0]
            result["bar_count"] = conn.execute(
                "SELECT COUNT(*) FROM bars"
            ).fetchone()[0]
            result["bar_ticker_count"] = conn.execute(
                "SELECT COUNT(DISTINCT ticker) FROM bars"
            ).fetchone()[0]
            result["date_range"] = conn.execute(
                "SELECT MIN(date), MAX(date) FROM bars"
            ).fetchone()
            result["largest_tickers"] = [
                {"ticker": row[0], "rows": row[1], "first_date": row[2], "last_date": row[3]}
                for row in conn.execute(
                    "SELECT ticker, COUNT(*), MIN(date), MAX(date) FROM bars "
                    "GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 20"
                )
            ]
        return result


def max_rss_kib() -> int | None:
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, AttributeError, OSError):
        return None



def _iso_date(value) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]


def probe_yahoo_gap_responses(gap_dates_by_ticker: dict[str, list[str]]) -> dict:
    """Probe current Yahoo availability for detected gaps; never writes to SQLite."""
    tickers = sorted(gap_dates_by_ticker)
    detail_by_ticker: dict[str, dict] = {}
    batch_errors: list[dict] = []
    batch_size = YAHOO_PROBE_BATCH_SIZE
    for offset in range(0, len(tickers), batch_size):
        batch = tickers[offset:offset + batch_size]
        requested_start = min(day for ticker in batch for day in gap_dates_by_ticker[ticker])
        requested_end = (max(
            dt.date.fromisoformat(day)
            for ticker in batch for day in gap_dates_by_ticker[ticker]
        ) + dt.timedelta(days=1)).isoformat()
        try:
            frames = historical_ohlcv._download_batch(
                batch, start=requested_start, end=requested_end
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            batch_errors.append({
                "tickers": batch, "requested_start": requested_start,
                "requested_end_exclusive": requested_end, "error": message,
            })
            for ticker in batch:
                detail_by_ticker[ticker] = {
                    "ticker": ticker,
                    "calendar": historical_ohlcv._calendar_name_for_ticker(ticker),
                    "missing_dates": gap_dates_by_ticker[ticker],
                    "requested_start": requested_start,
                    "requested_end_exclusive": requested_end,
                    "yahoo_status": "request_error",
                    "yahoo_error": message,
                    "returned_row_count": 0,
                    "returned_dates": [],
                    "missing_dates_returned": [],
                    "missing_dates_not_returned": gap_dates_by_ticker[ticker],
                }
            continue

        for ticker in batch:
            frame = frames.get(ticker)
            returned_dates = sorted({_iso_date(value) for value in frame.index}) if frame is not None and not frame.empty else []
            missing_dates = gap_dates_by_ticker[ticker]
            returned_missing = sorted(set(missing_dates) & set(returned_dates))
            if not returned_dates:
                status = "no_rows"
            elif len(returned_missing) == len(missing_dates):
                status = "all_missing_dates_returned"
            elif returned_missing:
                status = "partial_missing_dates_returned"
            else:
                status = "no_missing_date_returned"
            detail_by_ticker[ticker] = {
                "ticker": ticker,
                "calendar": historical_ohlcv._calendar_name_for_ticker(ticker),
                "missing_dates": missing_dates,
                "requested_start": requested_start,
                "requested_end_exclusive": requested_end,
                "yahoo_status": status,
                "yahoo_error": None,
                "returned_row_count": len(returned_dates),
                "returned_first_date": returned_dates[0] if returned_dates else None,
                "returned_last_date": returned_dates[-1] if returned_dates else None,
                "returned_dates": returned_dates,
                "missing_dates_returned": returned_missing,
                "missing_dates_not_returned": sorted(set(missing_dates) - set(returned_missing)),
            }
    counts: dict[str, int] = {}
    for item in detail_by_ticker.values():
        key = item["yahoo_status"]
        counts[key] = counts.get(key, 0) + 1
    return {
        "probe_type": "current_yahoo_read_only_probe",
        "probe_completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "interval": "1d",
        "auto_adjust": False,
        "batch_size": batch_size,
        "ticker_count": len(tickers),
        "status_counts": counts,
        "batch_errors": batch_errors,
        "tickers": detail_by_ticker,
        "interpretation": (
            "Yahoo-Abfrage ist eine aktuelle Verfügbarkeitsprobe und kein historischer Mitschnitt "
            "des Reparaturlaufs. Sie verändert weder die SQLite-Datenbank noch bestehende Kurse."
        ),
    }


def write_gap_detail_outputs(gap_audit: dict, yahoo_probe: dict) -> None:
    """Write machine-readable and human-readable per-ticker/per-date reports."""
    tickers = yahoo_probe.get("tickers", {})
    payload = {
        "diagnostic": "OHLCV_gap_cause_detail",
        "diagnostic_version": 1,
        "lookback_start": gap_audit.get("lookback_start"),
        "lookback_end": gap_audit.get("lookback_end"),
        "gap_ticker_count": gap_audit.get("gap_ticker_count", 0),
        "gap_session_count": gap_audit.get("gap_session_count", 0),
        "calendar_unverified_tickers": gap_audit.get("calendar_unverified_tickers", []),
        "calendar_unverified_reasons": gap_audit.get("calendar_unverified_reasons", {}),
        "yahoo_probe": yahoo_probe,
    }
    GAP_DETAIL_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    columns = [
        "ticker", "calendar", "missing_date", "requested_start", "requested_end_exclusive",
        "yahoo_status", "returned_row_count", "returned_first_date", "returned_last_date",
        "missing_date_returned", "yahoo_error",
    ]
    with GAP_DETAIL_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for ticker in sorted(tickers):
            item = tickers[ticker]
            returned = set(item.get("missing_dates_returned", []))
            for missing_date in item.get("missing_dates", []):
                writer.writerow({
                    **item,
                    "missing_date": missing_date,
                    "missing_date_returned": missing_date in returned,
                })
    lines = [
        "# OHLCV-Kurslücken – Detaildiagnose", "",
        f"- Lookback: {payload['lookback_start']} bis {payload['lookback_end']}",
        f"- Betroffene Ticker: {payload['gap_ticker_count']}",
        f"- Fehlende Handelssitzungen: {payload['gap_session_count']}",
        f"- Kalender nicht verifiziert: {len(payload['calendar_unverified_tickers'])}",
        "", "## Yahoo-Verfügbarkeitsprobe", "",
        "| Ergebnis | Ticker |", "|---|---:|",
    ]
    for status, count in sorted(yahoo_probe.get("status_counts", {}).items()):
        lines.append(f"| {status} | {count} |")
    lines += [
        "", "Die Yahoo-Abfrage ist eine aktuelle Verfügbarkeitsprobe, kein Mitschnitt des damaligen Reparaturlaufs.",
        "Sie schreibt nichts in die Datenbank und überschreibt keine vorhandenen OHLCV-Werte.",
        "Ein aktuell zurückgelieferter Tag belegt nicht, dass Yahoo ihn beim früheren Lauf ebenfalls geliefert hat.",
        "", "## Ticker mit den meisten offenen Lücken", "",
        "| Ticker | Kalender | Lücken | Yahoo-Status | Zurückgelieferte Lückentage |",
        "|---|---|---:|---|---:|",
    ]
    for ticker, item in sorted(tickers.items(), key=lambda pair: (-len(pair[1].get("missing_dates", [])), pair[0]))[:50]:
        lines.append(
            f"| {ticker} | {item.get('calendar') or 'unbekannt'} | "
            f"{len(item.get('missing_dates', []))} | {item.get('yahoo_status')} | "
            f"{len(item.get('missing_dates_returned', []))} |"
        )
    lines += ["", "Vollständige Einzelheiten stehen in `OHLCV_Gap_Detail.csv` und `OHLCV_Gap_Ursachen.json`.", ""]
    GAP_DETAIL_MD.write_text("\n".join(lines), encoding="utf-8")



INVALID_REASON_SQL = """
    trim(
        CASE WHEN open IS NULL THEN 'open_null|' ELSE '' END ||
        CASE WHEN high IS NULL THEN 'high_null|' ELSE '' END ||
        CASE WHEN low IS NULL THEN 'low_null|' ELSE '' END ||
        CASE WHEN close IS NULL THEN 'close_null|' ELSE '' END ||
        CASE WHEN volume IS NULL THEN 'volume_null|' ELSE '' END ||
        CASE WHEN open <= 0 THEN 'open_non_positive|' ELSE '' END ||
        CASE WHEN high <= 0 THEN 'high_non_positive|' ELSE '' END ||
        CASE WHEN low <= 0 THEN 'low_non_positive|' ELSE '' END ||
        CASE WHEN close <= 0 THEN 'close_non_positive|' ELSE '' END ||
        CASE WHEN volume < 0 THEN 'volume_negative|' ELSE '' END ||
        CASE WHEN high < low THEN 'high_below_low|' ELSE '' END ||
        CASE WHEN high < open THEN 'high_below_open|' ELSE '' END ||
        CASE WHEN high < close THEN 'high_below_close|' ELSE '' END ||
        CASE WHEN low > open THEN 'low_above_open|' ELSE '' END ||
        CASE WHEN low > close THEN 'low_above_close|' ELSE '' END,
        '|'
    )
"""


def collect_invalid_candle_details(db_path: Path, invalid_tickers: list[str]) -> dict:
    """Return every invalid candle using the exact predicates in audit_snapshot."""
    tickers = sorted({str(t).strip().upper() for t in invalid_tickers if str(t).strip()})
    if not tickers:
        return {"diagnostic": "OHLCV_invalid_candle_detail", "ticker_count": 0,
                "invalid_candle_count": 0, "recent_90d_count": 0,
                "historical_only_count": 0, "tickers": [], "reason_counts": {}, "rows": []}
    placeholders = ",".join("?" for _ in tickers)
    invalid_predicate = """
        open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL OR
        open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR volume < 0 OR
        high < low OR high < open OR high < close OR low > open OR low > close
    """
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=180) as conn:
        rows = conn.execute(
            f"""SELECT ticker, date, open, high, low, close, volume, source,
                       {INVALID_REASON_SQL} AS invalid_reasons,
                       CASE WHEN date >= date('now', '-90 days') THEN 1 ELSE 0 END AS recent_90d
                FROM bars
                WHERE ticker IN ({placeholders}) AND ({invalid_predicate})
                ORDER BY ticker, date""",
            tickers,
        ).fetchall()
    columns = ["ticker", "date", "open", "high", "low", "close", "volume", "source", "invalid_reasons", "recent_90d"]
    records = [dict(zip(columns, row)) for row in rows]
    reason_counts: dict[str, int] = {}
    by_ticker: dict[str, dict] = {}
    for row in records:
        ticker = row["ticker"]
        item = by_ticker.setdefault(ticker, {"ticker": ticker, "invalid_candle_count": 0,
                                             "recent_90d_count": 0, "historical_only_count": 0,
                                             "first_invalid_date": row["date"], "last_invalid_date": row["date"]})
        item["invalid_candle_count"] += 1
        if row["recent_90d"]:
            item["recent_90d_count"] += 1
        else:
            item["historical_only_count"] += 1
        item["last_invalid_date"] = row["date"]
        for reason in filter(None, row["invalid_reasons"].split("|")):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "diagnostic": "OHLCV_invalid_candle_detail",
        "diagnostic_version": 1,
        "classification_window_days": 90,
        "ticker_count": len(by_ticker),
        "invalid_candle_count": len(records),
        "recent_90d_count": sum(int(row["recent_90d"]) for row in records),
        "historical_only_count": sum(1 for row in records if not row["recent_90d"]),
        "reason_counts": dict(sorted(reason_counts.items())),
        "tickers": [by_ticker[key] for key in sorted(by_ticker)],
        "rows": records,
        "predicate_note": "Die Fehlerbedingungen entsprechen den INVALID-Prädikaten aus historical_ohlcv.audit_snapshot.",
    }


def probe_yahoo_missing_tickers(tickers: list[str], *, lookback_days: int = 90) -> dict[str, dict]:
    """Probe each missing universe ticker separately; never writes to SQLite."""
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    end = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    results: dict[str, dict] = {}
    for ticker in sorted(set(tickers)):
        try:
            frames = historical_ohlcv._download_batch([ticker], start=start, end=end)
            frame = frames.get(ticker)
            dates = sorted({_iso_date(value) for value in frame.index}) if frame is not None and not frame.empty else []
            results[ticker] = {
                "provider_status": "provider_has_recent_data" if dates else "no_provider_rows",
                "provider_row_count": len(dates),
                "provider_first_date": dates[0] if dates else None,
                "provider_last_date": dates[-1] if dates else None,
                "provider_error": None,
                "requested_start": start, "requested_end_exclusive": end,
            }
        except Exception as exc:
            results[ticker] = {
                "provider_status": "request_error", "provider_row_count": 0,
                "provider_first_date": None, "provider_last_date": None,
                "provider_error": f"{type(exc).__name__}: {exc}",
                "requested_start": start, "requested_end_exclusive": end,
            }
    return results


def collect_missing_instrument_details(db_path: Path, missing_tickers: list[str], provider_probe: dict[str, dict]) -> list[dict]:
    tickers = sorted({str(t).strip().upper() for t in missing_tickers if str(t).strip()})
    if not tickers:
        return []
    values_select = " UNION ALL ".join("SELECT ? AS ticker" if index == 0 else "SELECT ?" for index in range(len(tickers)))
    with sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=180) as conn:
        rows = conn.execute(
            f"""SELECT u.ticker, i.ticker IS NOT NULL AS instrument_record_exists,
                       COALESCE(i.row_count, 0), COUNT(b.date), MIN(b.date), MAX(b.date)
                FROM ({values_select}) u
                LEFT JOIN instruments i ON i.ticker=u.ticker
                LEFT JOIN bars b ON b.ticker=u.ticker
                GROUP BY u.ticker, i.ticker, i.row_count
                ORDER BY u.ticker""",
            tickers,
        ).fetchall()
    columns = ["ticker", "instrument_record_exists", "instrument_row_count", "bar_count", "first_date", "last_date"]
    details = []
    for row in rows:
        item = dict(zip(columns, row))
        item.update(provider_probe.get(item["ticker"], {}))
        details.append(item)
    return details


def write_quality_detail_outputs(invalid_detail: dict, missing_details: list[dict]) -> None:
    summary = {key: value for key, value in invalid_detail.items() if key != "rows"}
    INVALID_SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    invalid_columns = ["ticker", "date", "open", "high", "low", "close", "volume", "source", "invalid_reasons", "recent_90d"]
    with INVALID_DETAIL_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=invalid_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(invalid_detail.get("rows", []))
    missing_columns = ["ticker", "instrument_record_exists", "instrument_row_count", "bar_count", "first_date", "last_date",
                       "provider_status", "provider_row_count", "provider_first_date", "provider_last_date",
                       "provider_error", "requested_start", "requested_end_exclusive"]
    with MISSING_INSTRUMENTS_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=missing_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(missing_details)
    lines = [
        "# OHLCV-Qualitätsdetails", "",
        "## INVALID-Kerzen", "",
        f"- Betroffene Ticker: {invalid_detail.get('ticker_count', 0)}",
        f"- Fehlerhafte Kerzen insgesamt: {invalid_detail.get('invalid_candle_count', 0)}",
        f"- Davon innerhalb der letzten 90 Tage: {invalid_detail.get('recent_90d_count', 0)}",
        f"- Davon älter als 90 Tage: {invalid_detail.get('historical_only_count', 0)}",
        "", "### Fehlerursachen (Mehrfachnennungen pro Kerze möglich)", "",
        "| Fehlergrund | Anzahl betroffener Kerzen |", "|---|---:|",
    ]
    for reason, count in sorted(invalid_detail.get("reason_counts", {}).items()):
        lines.append(f"| `{reason}` | {count} |")
    lines += ["", "### Tickerübersicht", "", "| Ticker | Fehlerhafte Kerzen | letzte 90 Tage | älter als 90 Tage |", "|---|---:|---:|---:|"]
    for item in invalid_detail.get("tickers", []):
        lines.append(f"| {item['ticker']} | {item['invalid_candle_count']} | {item['recent_90d_count']} | {item['historical_only_count']} |")
    lines += ["", "## Fehlende Universums-Ticker", "",
              "| Ticker | Instrument-Datensatz | gespeicherte Bars | letzte Bar | Yahoo-Probe | Yahoo-Bars |",
              "|---|---|---:|---|---|---:|"]
    for item in missing_details:
        lines.append(f"| {item['ticker']} | {'ja' if item['instrument_record_exists'] else 'nein'} | {item['bar_count']} | {item['last_date'] or '—'} | {item.get('provider_status', 'nicht geprüft')} | {item.get('provider_row_count', 0)} |")
    lines += [
        "", "Die Yahoo-Probe ist eine aktuelle, read-only Verfügbarkeitsprüfung über die letzten 90 Kalendertage.",
        "Sie belegt nicht rückwirkend, ob ein Symbol beim früheren Datenbanklauf verfügbar war.",
        "Die Diagnose liest die Arbeitskopie; die Release-Quelldatenbank bleibt unverändert.",
        "", "Ein INVALID-Ticker kann mehrere fehlerhafte Kerzen enthalten. Die CSV enthält jede betroffene Kerze einzeln.", "",
    ]
    QUALITY_REPORT_MD.write_text("\n".join(lines), encoding="utf-8")

def main() -> int:
    started = time.monotonic()
    result: dict = {
        "diagnostic": "historical_ohlcv_release_database",
        "diagnostic_version": 4,
        "python": sys.version,
        "platform": sys.platform,
        "database_path": str(SOURCE_DB),
        "package2_api_present": all(hasattr(historical_ohlcv, name) for name in (
            "audit_snapshot", "_analyse_trading_day_gaps", "_load_bar_dates_by_ticker",
            "_detect_recent_trading_day_gaps", "_download_batch", "_calendar_name_for_ticker"
        )),
    }
    try:
        if not result["package2_api_present"]:
            raise RuntimeError(
                "Paket-2-Version von historical_ohlcv.py fehlt. Bitte zuerst die Paket-2-Dateien auf main hochladen."
            )
        if not SOURCE_FILE.is_file():
            raise FileNotFoundError(f"Universe-Quelldatei fehlt: {SOURCE_FILE}")
        if not SOURCE_DB.is_file() or SOURCE_DB.stat().st_size <= 0:
            raise FileNotFoundError(f"Release-Datenbank fehlt oder ist leer: {SOURCE_DB}")

        size = SOURCE_DB.stat().st_size
        free = shutil.disk_usage(ROOT).free
        result["file_size_bytes"] = size
        result["free_disk_bytes_before_copy"] = free
        if free < size + 512 * 1024 * 1024:
            raise OSError(
                f"Zu wenig freier Speicher fuer sichere Arbeitskopie: {free} Bytes frei, {size} Bytes benoetigt."
            )

        result["file_sha256"] = sha256_file(SOURCE_DB)
        result["database"] = inspect_database(SOURCE_DB)
        if not result["database"].get("integrity_ok"):
            raise RuntimeError("SQLite PRAGMA integrity_check ist nicht erfolgreich")
        if result["database"].get("missing_required_columns"):
            raise RuntimeError("SQLite-Schema weicht vom erwarteten OHLCV-Schema ab")

        # Run the application audit on an exact copy; its connection helper may
        # configure SQLite pragmas. The downloaded release asset stays untouched.
        shutil.copyfile(SOURCE_DB, AUDIT_DB)
        result["audit_copy_sha256"] = sha256_file(AUDIT_DB)
        if result["audit_copy_sha256"] != result["file_sha256"]:
            raise RuntimeError("Arbeitskopie stimmt nicht bytegenau mit dem Release-Asset ueberein")

        audit_started = time.monotonic()
        audit = historical_ohlcv.audit_snapshot(
            db_path=AUDIT_DB,
            source_file=SOURCE_FILE,
            dynamic_universe=DYNAMIC_UNIVERSE,
        )
        result["audit_elapsed_seconds"] = round(time.monotonic() - audit_started, 2)
        result["audit"] = audit
        result["audit_status"] = audit.get("status")
        result["audit_error"] = audit.get("error")
        for key in (
            "universe_count", "covered_count", "gap_tickers", "recent_gap_tickers",
            "historical_gap_tickers", "historical_only_gap_tickers",
            "calendar_unverified_tickers", "calendar_coverage_incomplete_tickers",
            "quality_status_counts", "affected_candidates",
        ):
            if key in audit:
                value = audit[key]
                result[f"audit_{key}"] = len(value) if isinstance(value, list) else value
        result["success"] = audit.get("status") in {"complete", "gaps"} and not audit.get("error")
        if not result["success"]:
            raise RuntimeError(
                f"audit_snapshot lieferte keinen verwertbaren Status: {audit.get('status')}; {audit.get('error')}"
            )

        # Explain every INVALID ticker at candle level, using the same predicates
        # as audit_snapshot. All detail files are written from the audit copy.
        invalid_tickers = audit.get("invalid_ohlcv_tickers", [])
        invalid_detail = collect_invalid_candle_details(AUDIT_DB, invalid_tickers)
        if invalid_detail.get("ticker_count") != len(invalid_tickers):
            raise RuntimeError(
                "INVALID-Detail passt nicht zur audit_snapshot-Tickerliste: "
                f"{invalid_detail.get('ticker_count')} Detail-Ticker bei {len(invalid_tickers)} Audit-Tickern"
            )
        result["invalid_detail_summary"] = {
            key: invalid_detail.get(key) for key in (
                "ticker_count", "invalid_candle_count", "recent_90d_count",
                "historical_only_count", "reason_counts"
            )
        }
        missing_tickers = audit.get("missing_tickers", [])
        missing_provider_probe = probe_yahoo_missing_tickers(missing_tickers)
        missing_details = collect_missing_instrument_details(
            AUDIT_DB, missing_tickers, missing_provider_probe
        )
        result["missing_instrument_details"] = missing_details
        write_quality_detail_outputs(invalid_detail, missing_details)
        result["quality_detail_outputs"] = [
            INVALID_DETAIL_CSV.name, INVALID_SUMMARY_JSON.name,
            MISSING_INSTRUMENTS_CSV.name, QUALITY_REPORT_MD.name,
        ]

        # Separate trailing+interior recent-gap scan. The historical audit above
        # intentionally only classifies interior gaps and therefore cannot
        # explain all remaining repair candidates.
        gap_audit = historical_ohlcv._detect_recent_trading_day_gaps(
            AUDIT_DB, audit.get("universe", []) or historical_ohlcv.discover_universe(
                SOURCE_FILE, DYNAMIC_UNIVERSE
            ),
            lookback_days=GAP_LOOKBACK_DAYS,
        )
        result["recent_gap_diagnostic"] = gap_audit
        result["recent_gap_ticker_count"] = gap_audit.get("gap_ticker_count", 0)
        result["recent_gap_session_count"] = gap_audit.get("gap_session_count", 0)
        result["recent_gap_lookback_start"] = gap_audit.get("lookback_start")
        result["recent_gap_lookback_end"] = gap_audit.get("lookback_end")
        yahoo_started = time.monotonic()
        try:
            yahoo_probe = probe_yahoo_gap_responses(gap_audit.get("gap_dates_by_ticker", {}))
            result["yahoo_probe"] = yahoo_probe
            result["yahoo_probe_elapsed_seconds"] = round(time.monotonic() - yahoo_started, 2)
            write_gap_detail_outputs(gap_audit, yahoo_probe)
            result["gap_detail_outputs"] = [
                GAP_DETAIL_JSON.name, GAP_DETAIL_CSV.name, GAP_DETAIL_MD.name
            ]
        except Exception as exc:
            result["yahoo_probe_error"] = f"{type(exc).__name__}: {exc}"
            # Keep the core read-only DB audit useful even if Yahoo is unavailable.
            fallback = {
                "probe_type": "current_yahoo_read_only_probe",
                "status_counts": {}, "tickers": {},
                "batch_errors": [{"error": result["yahoo_probe_error"]}],
            }
            write_gap_detail_outputs(gap_audit, fallback)
            result["gap_detail_outputs"] = [
                GAP_DETAIL_JSON.name, GAP_DETAIL_CSV.name, GAP_DETAIL_MD.name
            ]
        result["source_sha256_after_audit"] = sha256_file(SOURCE_DB)
        result["source_unchanged"] = result["source_sha256_after_audit"] == result["file_sha256"]
        if not result["source_unchanged"]:
            raise RuntimeError("Das heruntergeladene Release-Asset wurde unerwartet veraendert")
    except Exception as exc:
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if AUDIT_DB.exists():
                AUDIT_DB.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(AUDIT_DB) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            result["audit_copy_removed"] = not AUDIT_DB.exists()
        except OSError as exc:
            result["cleanup_warning"] = str(exc)
        result["elapsed_seconds_total"] = round(time.monotonic() - started, 2)
        result["max_rss_kib"] = max_rss_kib()
        REPORT.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        summary_keys = (
            "success", "file_size_bytes", "file_sha256", "database", "audit_status",
            "audit_error", "audit_elapsed_seconds", "audit_universe_count",
            "audit_covered_count", "audit_gap_tickers", "audit_recent_gap_tickers",
            "audit_historical_gap_tickers", "audit_calendar_unverified_tickers",
            "audit_calendar_coverage_incomplete_tickers", "audit_quality_status_counts",
            "recent_gap_ticker_count", "recent_gap_session_count", "recent_gap_lookback_start",
            "recent_gap_lookback_end", "yahoo_probe_error", "yahoo_probe_elapsed_seconds",
            "gap_detail_outputs", "quality_detail_outputs", "invalid_detail_summary",
            "missing_instrument_details", "elapsed_seconds_total", "max_rss_kib", "audit_copy_removed", "error",
            "cleanup_warning",
        )
        print(json.dumps({k: result[k] for k in summary_keys if k in result},
                         ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
