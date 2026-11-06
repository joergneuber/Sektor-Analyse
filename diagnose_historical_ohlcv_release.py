#!/usr/bin/env python3
"""Read-only diagnosis of the historical OHLCV release database.

The downloaded release asset is inspected read-only. The application audit is
run against a byte-for-byte working copy because historical_ohlcv._connect()
sets SQLite journal pragmas and is not a read-only connection helper.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import historical_ohlcv

ROOT = Path.cwd()
SOURCE_DB = ROOT / "historical_ohlcv.sqlite"
AUDIT_DB = ROOT / "historical_ohlcv_diagnostic_copy.sqlite"
REPORT = ROOT / "historical_ohlcv_release_diagnostic.json"


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path) -> dict:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=120) as conn:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity = [str(row[0]) for row in integrity_rows]
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
        missing_schema = {
            table: sorted(columns - set(schema.get(table, [])))
            for table, columns in required.items()
            if columns - set(schema.get(table, []))
        }
        result = {
            "integrity_check": integrity,
            "integrity_ok": integrity == ["ok"],
            "tables": tables,
            "schema": schema,
            "missing_required_columns": missing_schema,
        }
        if not missing_schema and "bars" in tables and "instruments" in tables:
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
                    "GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 15"
                )
            ]
        return result


def main() -> int:
    started = time.monotonic()
    result: dict = {
        "diagnostic": "historical_ohlcv_release_database",
        "python": sys.version,
        "platform": sys.platform,
        "database_path": str(SOURCE_DB),
    }
    try:
        if not SOURCE_DB.is_file():
            raise FileNotFoundError(f"Release-Datenbank fehlt: {SOURCE_DB}")
        result["file_size_bytes"] = SOURCE_DB.stat().st_size
        result["file_sha256"] = sha256_file(SOURCE_DB)
        result["database"] = inspect_database(SOURCE_DB)
        if not result["database"].get("integrity_ok"):
            raise RuntimeError("SQLite integrity_check ist nicht erfolgreich")
        if result["database"].get("missing_required_columns"):
            raise RuntimeError("Das SQLite-Schema weicht vom erwarteten OHLCV-Schema ab")

        # Audit runs on a copy to ensure the downloaded release asset is never
        # modified by the module's normal read/write connection helper.
        shutil.copyfile(SOURCE_DB, AUDIT_DB)
        copy_hash = sha256_file(AUDIT_DB)
        result["audit_copy_sha256"] = copy_hash
        if copy_hash != result["file_sha256"]:
            raise RuntimeError("Arbeitskopie stimmt nicht bytegenau mit Release-Datei überein")

        audit_started = time.monotonic()
        audit = historical_ohlcv.audit_snapshot(
            db_path=AUDIT_DB,
            source_file=ROOT / "analyse.py",
            dynamic_universe=ROOT / "historical_ohlcv_universe.json",
        )
        result["audit_elapsed_seconds"] = round(time.monotonic() - audit_started, 2)
        result["audit"] = audit
        result["audit_status"] = audit.get("status")
        result["audit_error"] = audit.get("error")
        result["audit_gap_ticker_count"] = len(audit.get("gap_tickers", []))
        result["audit_recent_gap_ticker_count"] = len(audit.get("recent_gap_tickers", []))
        result["audit_unverified_calendar_count"] = len(audit.get("calendar_unverified_tickers", []))
        result["audit_coverage_incomplete_count"] = len(audit.get("calendar_coverage_incomplete_tickers", []))
        result["audit_quality_status_counts"] = audit.get("quality_status_counts", {})
        result["success"] = audit.get("status") in {"complete", "gaps"} and not audit.get("error")
        result["audit_copy_removed"] = False
    except Exception as exc:  # report diagnostic failures as an artifact
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["elapsed_seconds_total"] = round(time.monotonic() - started, 2)
        result["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
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
        REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({
            key: result.get(key) for key in (
                "success", "file_size_bytes", "file_sha256", "database",
                "audit_status", "audit_error", "audit_elapsed_seconds",
                "audit_gap_ticker_count", "audit_recent_gap_ticker_count",
                "audit_unverified_calendar_count", "audit_coverage_incomplete_count",
                "audit_quality_status_counts", "elapsed_seconds_total", "max_rss_kib",
                "error", "cleanup_warning",
            ) if key in result
        }, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
