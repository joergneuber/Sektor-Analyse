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


def main() -> int:
    started = time.monotonic()
    result: dict = {
        "diagnostic": "historical_ohlcv_release_database",
        "diagnostic_version": 3,
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
            "gap_detail_outputs", "elapsed_seconds_total", "max_rss_kib", "audit_copy_removed", "error",
            "cleanup_warning",
        )
        print(json.dumps({k: result[k] for k in summary_keys if k in result},
                         ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
