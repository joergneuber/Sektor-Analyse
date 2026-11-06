#!/usr/bin/env python3
"""Read-only release-DB diagnosis for Paket 2 (GitHub Actions runner)."""
from __future__ import annotations

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


def main() -> int:
    started = time.monotonic()
    result: dict = {
        "diagnostic": "historical_ohlcv_release_database",
        "diagnostic_version": 2,
        "python": sys.version,
        "platform": sys.platform,
        "database_path": str(SOURCE_DB),
        "package2_api_present": all(hasattr(historical_ohlcv, name) for name in (
            "audit_snapshot", "_analyse_trading_day_gaps", "_load_bar_dates_by_ticker"
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
            "elapsed_seconds_total", "max_rss_kib", "audit_copy_removed", "error",
            "cleanup_warning",
        )
        print(json.dumps({k: result[k] for k in summary_keys if k in result},
                         ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
