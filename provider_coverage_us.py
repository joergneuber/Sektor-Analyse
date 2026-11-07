#!/usr/bin/env python3
"""Read-only Alpaca-vs-Yahoo coverage comparison for the complete US universe.

The production SQLite database is never opened for writing. A byte-for-byte
copy is used for the existing OHLCV audit. Provider requests are read-only.
Reports are written as JSON/CSV/Markdown in the current working directory.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path.cwd()
DB_PATH = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DB", "historical_ohlcv.sqlite"))
SOURCE_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_SOURCE", "analyse.py"))
DYNAMIC_UNIVERSE = Path(os.environ.get(
    "NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE", "historical_ohlcv_universe.json"
))
REPORT_JSON = ROOT / "US_Provider_Coverage.json"
REPORT_CSV = ROOT / "US_Provider_Coverage.csv"
REPORT_MD = ROOT / "US_Provider_Coverage.md"
ALPACA_BATCH_SIZE = 100
YAHOO_BATCH_SIZE = 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_readonly_check(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=180) as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"bars", "instruments"}.issubset(tables):
            raise RuntimeError(f"SQLite-Schema unvollständig: {sorted(tables)}")
        return {
            "integrity_check": integrity,
            "bar_count": int(conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]),
            "instrument_count": int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]),
        }


def load_db_ranges(db_path: Path, tickers: list[str]) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=180) as conn:
        rows = conn.execute(
            f"""SELECT ticker, MIN(date), MAX(date), COUNT(*)
                FROM bars WHERE ticker IN ({placeholders}) GROUP BY ticker""",
            tickers,
        ).fetchall()
    return {
        str(ticker): {"first_date": first, "last_date": last, "bar_count": int(count)}
        for ticker, first, last, count in rows
    }


def load_observed_dates(db_path: Path, ticker: str) -> set[str]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=180) as conn:
        rows = conn.execute("SELECT date FROM bars WHERE ticker=? ORDER BY date", (ticker,)).fetchall()
    return {str(row[0])[:10] for row in rows}


def _frame_dates(frame: pd.DataFrame | None) -> list[str]:
    if frame is None or frame.empty:
        return []
    dates: set[str] = set()
    for value in frame.index:
        try:
            stamp = pd.Timestamp(value)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert("UTC")
            dates.add(stamp.date().isoformat())
        except Exception:
            continue
    return sorted(dates)


def _normalise_alpaca_frames(
    raw: pd.DataFrame | None,
    original_symbols: list[str],
    request_symbols: list[str],
) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return result
    for original, requested in zip(original_symbols, request_symbols):
        frame = None
        if isinstance(raw.index, pd.MultiIndex):
            for level in range(raw.index.nlevels):
                values = raw.index.get_level_values(level)
                for candidate in (requested, original):
                    if candidate in values:
                        try:
                            frame = raw.xs(candidate, level=level, drop_level=True).copy()
                            break
                        except (KeyError, ValueError):
                            pass
                if frame is not None:
                    break
        elif len(original_symbols) == 1:
            frame = raw.copy()
        if frame is None or frame.empty:
            continue
        rename = {
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        }
        frame = frame.rename(columns=rename)
        required = {"Open", "High", "Low", "Close", "Volume"}
        if required.issubset(frame.columns):
            result[original] = frame
    return result


def fetch_alpaca(
    client: Any,
    tickers: list[str],
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Fetch all US symbols with the same unspecified feed as production.

    Production StockBarsRequest currently omits `feed`; this diagnostic does
    the same (API default), and explicitly requests RAW adjustment, which is
    also the API default. Invalid-symbol chunks are recursively isolated.
    """
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    aliases = {"BRK-B": "BRK.B", "BF-B": "BF.B"}
    frames: dict[str, pd.DataFrame] = {}
    statuses: dict[str, str] = {}

    def request_chunk(symbols: list[str], depth: int = 0) -> None:
        if not symbols:
            return
        request_symbols = [aliases.get(symbol, symbol) for symbol in symbols]
        request = StockBarsRequest(
            symbol_or_symbols=request_symbols,
            start=start,
            end=end,
            timeframe=TimeFrame.Day,
            adjustment=Adjustment.RAW,
            # Deliberately no feed=...: mirror production's current default.
        )
        response = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.get_stock_bars(request)
                last_error = None
                break
            except Exception as exc:  # provider/API errors are report data
                last_error = exc
                if "invalid symbol" in str(exc).lower():
                    if len(symbols) > 1:
                        middle = len(symbols) // 2
                        request_chunk(symbols[:middle], depth + 1)
                        request_chunk(symbols[middle:], depth + 1)
                        return
                    break
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
        if last_error is not None:
            message = f"{type(last_error).__name__}: {str(last_error)[:240]}"
            status = "invalid_symbol" if "invalid symbol" in str(last_error).lower() else "request_error: " + message
            for symbol in symbols:
                statuses[symbol] = status
            return
        raw = getattr(response, "df", None)
        parsed = _normalise_alpaca_frames(raw, symbols, request_symbols)
        for symbol in symbols:
            frame = parsed.get(symbol)
            if frame is not None and not frame.empty:
                frames[symbol] = frame
                statuses[symbol] = "rows_returned"
            else:
                statuses[symbol] = "no_rows"

    for offset in range(0, len(tickers), ALPACA_BATCH_SIZE):
        request_chunk(tickers[offset:offset + ALPACA_BATCH_SIZE])
    return frames, statuses


def fetch_yahoo(
    tickers: list[str], start_date: str, end_date: str
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    import historical_ohlcv

    frames: dict[str, pd.DataFrame] = {}
    statuses: dict[str, str] = {}
    for offset in range(0, len(tickers), YAHOO_BATCH_SIZE):
        chunk = tickers[offset:offset + YAHOO_BATCH_SIZE]
        result = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                result = historical_ohlcv._download_batch(
                    chunk, start=start_date, end=end_date
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(2)
        if last_error is not None:
            message = f"{type(last_error).__name__}: {str(last_error)[:240]}"
            for ticker in chunk:
                statuses[ticker] = "request_error: " + message
            continue
        for ticker in chunk:
            frame = (result or {}).get(ticker)
            if frame is not None and not frame.empty:
                frames[ticker] = frame
                statuses[ticker] = "rows_returned"
            else:
                statuses[ticker] = "no_rows"
    return frames, statuses


def _invalid_bar_count(frame: pd.DataFrame | None, ticker: str) -> int:
    if frame is None or frame.empty:
        return 0
    import historical_ohlcv

    count = 0
    for _, row in frame.iterrows():
        try:
            reasons = historical_ohlcv._ohlcv_validation_reasons(
                row, ticker=ticker, date_key=pd.Timestamp(row.name).date().isoformat()
            )
        except Exception:
            reasons = ["validation_error"]
        if reasons:
            count += 1
    return count


def _calendar_sessions(calendar: str, start: str, end: str) -> list[str]:
    import historical_ohlcv

    sessions = historical_ohlcv._calendar_session_dates(calendar, start, end)
    return sorted(historical_ohlcv._expected_daily_bar_dates(calendar, sessions))


def _provider_summary(
    frame: pd.DataFrame | None,
    status: str,
    expected_sessions: list[str],
    ticker: str,
) -> dict[str, Any]:
    dates = _frame_dates(frame)
    date_set = set(dates)
    expected = set(expected_sessions)
    covered = sorted(date_set & expected)
    missing = sorted(expected - date_set)
    return {
        "status": status,
        "bar_count": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "expected_sessions_covered": len(covered),
        "expected_sessions_missing": len(missing),
        "missing_dates_sample": missing[:25],
        "missing_dates_truncated": len(missing) > 25,
        "invalid_bars": _invalid_bar_count(frame, ticker),
        "has_latest_expected_session": bool(expected_sessions and expected_sessions[-1] in date_set),
    }


def _exact_us_historical_gaps(
    audit: dict[str, Any], db_path: Path, us_tickers: list[str]
) -> dict[str, list[str]]:
    import historical_ohlcv

    calendars = audit.get("calendar_by_ticker", {})
    detail = audit.get("trading_day_gap_details", {})
    result: dict[str, list[str]] = {}
    for ticker in us_tickers:
        item = detail.get(ticker)
        if not item:
            continue
        dates = list(item.get("missing_dates_sample", []))
        if item.get("dates_truncated"):
            first, last = item.get("first_date"), item.get("last_date")
            if not first or not last:
                raise RuntimeError(f"Historische Gap-Daten für {ticker} sind unvollständig.")
            calendar = calendars.get(ticker) or historical_ohlcv._calendar_name_for_ticker(ticker)
            expected = set(_calendar_sessions(calendar, first, last))
            observed = load_observed_dates(db_path, ticker)
            dates = sorted(expected - observed)
        if dates:
            result[ticker] = sorted(set(str(value)[:10] for value in dates))
    return result


def _probe_historical_gaps(
    client: Any,
    yahoo_existing: dict[str, pd.DataFrame],
    alpaca_existing: dict[str, pd.DataFrame],
    gaps: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """Probe exact old gap dates outside the rolling coverage window."""
    import historical_ohlcv
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    result: dict[str, dict[str, Any]] = {}
    for ticker, dates in gaps.items():
        result[ticker] = {}
        for provider, frames in (("alpaca", alpaca_existing), ("yahoo", yahoo_existing)):
            if provider == "alpaca" and ticker.startswith("^"):
                result[ticker][provider] = {
                    "requested_gap_dates": dates,
                    "returned_gap_dates": [],
                    "not_returned_gap_dates": dates,
                    "request_status": "not_applicable_index_symbol",
                }
                continue
            frame = frames.get(ticker)
            available = set(_frame_dates(frame)) if frame is not None else set()
            outside = [date for date in dates if date not in available]
            if not outside:
                result[ticker][provider] = {
                    "requested_gap_dates": dates,
                    "returned_gap_dates": [date for date in dates if date in available],
                    "not_returned_gap_dates": [],
                    "request_status": "covered_by_rolling_window",
                }
                continue
            start_date = min(outside)
            end_date = (dt.date.fromisoformat(max(outside)) + dt.timedelta(days=1)).isoformat()
            if provider == "yahoo":
                try:
                    targeted = historical_ohlcv._download_batch(
                        [ticker], start=start_date, end=end_date
                    ).get(ticker)
                    target_dates = set(_frame_dates(targeted))
                    error = None
                except Exception as exc:
                    target_dates = set()
                    error = f"{type(exc).__name__}: {str(exc)[:240]}"
            else:
                request_symbol = {"BRK-B": "BRK.B", "BF-B": "BF.B"}.get(ticker, ticker)
                try:
                    request = StockBarsRequest(
                        symbol_or_symbols=[request_symbol],
                        start=dt.datetime.combine(dt.date.fromisoformat(start_date), dt.time.min),
                        end=dt.datetime.combine(dt.date.fromisoformat(end_date), dt.time.min),
                        timeframe=TimeFrame.Day,
                        adjustment=Adjustment.RAW,
                    )
                    raw = client.get_stock_bars(request).df
                    targeted_frame = _normalise_alpaca_frames(raw, [ticker], [request_symbol]).get(ticker)
                    target_dates = set(_frame_dates(targeted_frame))
                    error = None
                except Exception as exc:
                    target_dates = set()
                    error = f"{type(exc).__name__}: {str(exc)[:240]}"
            returned = sorted(set(dates) & (available | target_dates))
            not_returned = sorted(set(dates) - set(returned))
            result[ticker][provider] = {
                "requested_gap_dates": dates,
                "returned_gap_dates": returned,
                "not_returned_gap_dates": not_returned,
                "request_status": "request_error" if error else "targeted_probe_completed",
                "request_error": error,
            }
    return result


def run_comparison(lookback_days: int) -> dict[str, Any]:
    import historical_ohlcv
    from alpaca.data.historical import StockHistoricalDataClient

    api_key = os.environ.get("ALPACA_KEY", "").strip()
    api_secret = os.environ.get("ALPACA_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("ALPACA_KEY oder ALPACA_SECRET fehlt in den GitHub Secrets.")
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Historische SQLite nicht gefunden: {DB_PATH}")
    if not SOURCE_FILE.is_file():
        raise FileNotFoundError(f"Universumsquelle nicht gefunden: {SOURCE_FILE}")
    if not DYNAMIC_UNIVERSE.is_file():
        raise FileNotFoundError(
            f"Autoritatives dynamisches Universum fehlt: {DYNAMIC_UNIVERSE}; "
            "bitte das Release-Asset historische_ohlcv_universe.json bereitstellen."
        )

    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    source_hash_before = sha256_file(DB_PATH)
    source_check = sqlite_readonly_check(DB_PATH)

    with tempfile.TemporaryDirectory(prefix="us-provider-audit-") as temp_dir:
        audit_db = Path(temp_dir) / "historical_ohlcv_audit_copy.sqlite"
        shutil.copy2(DB_PATH, audit_db)
        copy_hash = sha256_file(audit_db)
        if copy_hash != source_hash_before:
            raise RuntimeError("Read-only-Prüfkopie stimmt byteweise nicht mit der Release-SQLite überein.")

        audit = historical_ohlcv.audit_snapshot(
            db_path=audit_db,
            source_file=SOURCE_FILE,
            dynamic_universe=DYNAMIC_UNIVERSE,
        )
        if audit.get("status") == "unavailable" or audit.get("error"):
            raise RuntimeError(f"OHLCV-Audit der Prüfkopie fehlgeschlagen: {audit.get('error')}")

        calendars = audit.get("calendar_by_ticker", {})
        us_universe = sorted(
            ticker for ticker, calendar in calendars.items()
            if calendar in {"XNYS", "XNAS"}
        )
        if not us_universe:
            raise RuntimeError("Das vollständige US-Universum (XNYS/XNAS) ist leer.")
        # Index benchmarks are part of the project universe, but Alpaca's
        # StockHistoricalDataClient is a stock/ETF endpoint, not an index feed.
        # Keep them visible in the report and Yahoo probe; exclude them only
        # from the Alpaca-equity denominator.
        us_index_tickers = [ticker for ticker in us_universe if ticker.startswith("^")]
        us_equity_tickers = [ticker for ticker in us_universe if not ticker.startswith("^")]
        db_ranges = load_db_ranges(audit_db, us_universe)
        historical_gaps = _exact_us_historical_gaps(audit, audit_db, us_universe)

        today = dt.datetime.now(dt.timezone.utc).date()
        start_day = today - dt.timedelta(days=lookback_days)
        end_day = today + dt.timedelta(days=1)
        start_dt = dt.datetime.combine(start_day, dt.time.min)
        end_dt = dt.datetime.combine(end_day, dt.time.min)
        yahoo_start = start_day.isoformat()
        yahoo_end = end_day.isoformat()

        alpaca_client = StockHistoricalDataClient(api_key, api_secret)
        alpaca_frames, alpaca_status = fetch_alpaca(
            alpaca_client, us_equity_tickers, start_dt, end_dt
        )
        yahoo_frames, yahoo_status = fetch_yahoo(us_universe, yahoo_start, yahoo_end)

        rows: list[dict[str, Any]] = []
        for ticker in us_universe:
            calendar = calendars[ticker]
            db_info = db_ranges.get(ticker, {})
            db_first = db_info.get("first_date")
            db_last = db_info.get("last_date")
            coverage_start = max(start_day.isoformat(), str(db_first)[:10]) if db_first else yahoo_start
            latest_expected = None
            try:
                latest_expected = historical_ohlcv._previous_completed_session_date(
                    calendar, today.isoformat()
                )
            except Exception:
                latest_expected = None
            # Do not count today's still-open session as missing when this
            # workflow is manually started during market hours.
            expected_end = latest_expected or today.isoformat()
            expected = _calendar_sessions(calendar, coverage_start, expected_end)
            if latest_expected is None and expected:
                latest_expected = expected[-1]

            is_index = ticker.startswith("^")
            alpaca_summary = _provider_summary(
                alpaca_frames.get(ticker),
                "not_applicable_index_symbol" if is_index else alpaca_status.get(ticker, "missing_status"),
                expected, ticker,
            )
            yahoo_summary = _provider_summary(
                yahoo_frames.get(ticker), yahoo_status.get(ticker, "missing_status"), expected, ticker
            )
            gap_dates = historical_gaps.get(ticker, [])
            rows.append({
                "ticker": ticker,
                "calendar": calendar,
                "db_first_date": db_first,
                "db_last_date": db_last,
                "db_bar_count": db_info.get("bar_count", 0),
                "coverage_start": coverage_start,
                "coverage_end_exclusive": yahoo_end,
                "expected_sessions": len(expected),
                "latest_expected_session": latest_expected,
                "db_has_latest_expected": bool(latest_expected and db_last and str(db_last)[:10] >= latest_expected),
                "alpaca_status": alpaca_summary["status"],
                "alpaca_applicable": not is_index,
                "alpaca_bar_count": alpaca_summary["bar_count"],
                "alpaca_first_date": alpaca_summary["first_date"],
                "alpaca_last_date": alpaca_summary["last_date"],
                "alpaca_sessions_covered": alpaca_summary["expected_sessions_covered"],
                "alpaca_sessions_missing": alpaca_summary["expected_sessions_missing"],
                "alpaca_invalid_bars": alpaca_summary["invalid_bars"],
                "alpaca_has_latest_expected": alpaca_summary["has_latest_expected_session"],
                "yahoo_status": yahoo_summary["status"],
                "yahoo_bar_count": yahoo_summary["bar_count"],
                "yahoo_first_date": yahoo_summary["first_date"],
                "yahoo_last_date": yahoo_summary["last_date"],
                "yahoo_sessions_covered": yahoo_summary["expected_sessions_covered"],
                "yahoo_sessions_missing": yahoo_summary["expected_sessions_missing"],
                "yahoo_invalid_bars": yahoo_summary["invalid_bars"],
                "yahoo_has_latest_expected": yahoo_summary["has_latest_expected_session"],
                "historical_gap_count": len(gap_dates),
                "historical_gap_dates": ";".join(gap_dates),
                "historical_gap_dates_truncated": False,
                "alpaca_historical_gap_dates_returned": "",
                "yahoo_historical_gap_dates_returned": "",
            })

        targeted_gaps = _probe_historical_gaps(
            alpaca_client, yahoo_frames, alpaca_frames, historical_gaps
        )
        row_by_ticker = {row["ticker"]: row for row in rows}
        for ticker, provider_results in targeted_gaps.items():
            for provider, key in (("alpaca", "alpaca_historical_gap_dates_returned"),
                                  ("yahoo", "yahoo_historical_gap_dates_returned")):
                item = provider_results.get(provider, {})
                row_by_ticker[ticker][key] = ";".join(item.get("returned_gap_dates", []))

        source_hash_after = sha256_file(DB_PATH)
        if source_hash_after != source_hash_before:
            raise RuntimeError("Die originale historische SQLite hat sich während des Diagnose-Laufs verändert.")

    def count_true(field: str) -> int:
        return sum(bool(row[field]) for row in rows)

    def sum_field(field: str) -> int:
        return sum(int(row[field] or 0) for row in rows)

    summary = {
        "universe_count": len(us_universe),
        "alpaca_equity_universe_count": len(us_equity_tickers),
        "index_benchmark_count_excluded_from_alpaca_stock_endpoint": len(us_index_tickers),
        "us_historical_gap_ticker_count": len(historical_gaps),
        "us_historical_gap_session_count": sum(len(v) for v in historical_gaps.values()),
        "alpaca_rows_ticker_count": sum(row["alpaca_applicable"] and row["alpaca_status"] == "rows_returned" for row in rows),
        "alpaca_no_rows_ticker_count": sum(row["alpaca_applicable"] and row["alpaca_status"] == "no_rows" for row in rows),
        "alpaca_invalid_symbol_ticker_count": sum(row["alpaca_applicable"] and row["alpaca_status"] == "invalid_symbol" for row in rows),
        "alpaca_request_error_ticker_count": sum(row["alpaca_applicable"] and str(row["alpaca_status"]).startswith("request_error") for row in rows),
        "alpaca_latest_session_ticker_count": sum(row["alpaca_applicable"] and row["alpaca_has_latest_expected"] for row in rows),
        "alpaca_missing_expected_sessions": sum(int(row["alpaca_sessions_missing"] or 0) for row in rows if row["alpaca_applicable"]),
        "alpaca_invalid_bars": sum(int(row["alpaca_invalid_bars"] or 0) for row in rows if row["alpaca_applicable"]),
        "yahoo_rows_ticker_count": sum(row["yahoo_status"] == "rows_returned" for row in rows),
        "yahoo_no_rows_ticker_count": sum(row["yahoo_status"] == "no_rows" for row in rows),
        "yahoo_request_error_ticker_count": sum(str(row["yahoo_status"]).startswith("request_error") for row in rows),
        "yahoo_latest_session_ticker_count": count_true("yahoo_has_latest_expected"),
        "yahoo_missing_expected_sessions": sum_field("yahoo_sessions_missing"),
        "yahoo_invalid_bars": sum_field("yahoo_invalid_bars"),
        "db_latest_session_ticker_count": count_true("db_has_latest_expected"),
        "alpaca_only_latest": sum(row["alpaca_applicable"] and row["alpaca_has_latest_expected"] and not row["yahoo_has_latest_expected"] for row in rows),
        "yahoo_only_latest": sum(row["alpaca_applicable"] and row["yahoo_has_latest_expected"] and not row["alpaca_has_latest_expected"] for row in rows),
        "both_latest": sum(row["alpaca_applicable"] and row["alpaca_has_latest_expected"] and row["yahoo_has_latest_expected"] for row in rows),
        "neither_latest": sum(row["alpaca_applicable"] and not row["alpaca_has_latest_expected"] and not row["yahoo_has_latest_expected"] for row in rows),
        "alpaca_feed": "API default (feed omitted to mirror production StockBarsRequest)",
        "alpaca_adjustment": "RAW",
        "yahoo_auto_adjust": False,
        "lookback_days": lookback_days,
        "lookback_start": yahoo_start,
        "lookback_end_exclusive": yahoo_end,
    }
    report = {
        "diagnostic": "US_Alpaca_Yahoo_Provider_Coverage",
        "diagnostic_version": 1,
        "created_at_utc": started.isoformat(),
        "github_sha": os.environ.get("GITHUB_SHA"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "database_path": str(DB_PATH),
        "database_sha256_before": source_hash_before,
        "database_sha256_after": source_hash_after,
        "database_unchanged": source_hash_before == source_hash_after,
        "database_readonly_integrity": source_check,
        "audit_copy_sha256": copy_hash,
        "audit_status": audit.get("status"),
        "audit_checked_at": audit.get("checked_at"),
        "full_project_universe_count": audit.get("universe_count"),
        "full_project_covered_count": audit.get("covered_count"),
        "us_ticker_count": len(us_universe),
        "us_equity_ticker_count": len(us_equity_tickers),
        "us_index_benchmark_tickers_excluded_from_alpaca": us_index_tickers,
        "us_tickers": us_universe,
        "us_historical_gaps": historical_gaps,
        "targeted_historical_gap_probe": targeted_gaps,
        "provider_comparison_summary": summary,
        "ticker_results": rows,
        "interpretation": [
            "Alpaca wird ohne explizites feed-Argument abgefragt, um den aktuellen Produktionsaufruf nachzubilden.",
            "Fünf Index-Benchmarks mit ^-Präfix bleiben im Yahoo-/DB-Vergleich sichtbar, werden aber nicht als Aktien beim Alpaca-Stock-Endpunkt angefragt.",
            "Alpaca adjustment=RAW und Yahoo auto_adjust=False werden verwendet.",
            "Ein leerer Provider-Abruf ist kein Beweis für generelle Nichtunterstützung; Request-Fehler werden separat ausgewiesen.",
            "Die historische SQLite wird nur gelesen; das vorhandene Audit läuft auf einer bytegleichen temporären Kopie.",
            "Der Vergleich misst Datenabdeckung und einfache OHLCV-Strukturfehler, nicht Preisgleichheit oder Ausführungsqualität.",
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if rows:
        with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    s = report["provider_comparison_summary"]
    lines = [
        "# US Provider Coverage – Alpaca vs. Yahoo",
        "",
        f"- Erstellt (UTC): {report['created_at_utc']}",
        f"- GitHub SHA: {report.get('github_sha') or 'lokal/unbekannt'}",
        f"- Datenbank-SHA256: `{report['database_sha256_before']}`",
        f"- Originaldatenbank unverändert: **{report['database_unchanged']}**",
        f"- Vollständiges US-Universum: **{s['universe_count']} Symbole**, davon **{s['alpaca_equity_universe_count']} Aktien/ETFs** und **{s['index_benchmark_count_excluded_from_alpaca_stock_endpoint']} Index-Benchmarks** (nicht an Alpaca Stock API gesendet)",
        f"- Vergleichsfenster: {s['lookback_start']} bis {s['lookback_end_exclusive']} (Ende exklusiv)",
        f"- Alpaca-Feed: {s['alpaca_feed']}; Adjustment: {s['alpaca_adjustment']}",
        f"- Yahoo auto_adjust: `{s['yahoo_auto_adjust']}`",
        "",
        "## Zusammenfassung",
        "",
        "| Kennzahl | Alpaca | Yahoo |",
        "|---|---:|---:|",
        f"| Ticker mit Daten | {s['alpaca_rows_ticker_count']} | {s['yahoo_rows_ticker_count']} |",
        f"| Ticker ohne Daten | {s['alpaca_no_rows_ticker_count']} | {s['yahoo_no_rows_ticker_count']} |",
        f"| Von Alpaca als ungültiges Symbol abgelehnt | {s['alpaca_invalid_symbol_ticker_count']} | – |",
        f"| Ticker mit Request-Fehler | {s['alpaca_request_error_ticker_count']} | {s['yahoo_request_error_ticker_count']} |",
        f"| Ticker mit letztem erwarteten Handelstag | {s['alpaca_latest_session_ticker_count']} | {s['yahoo_latest_session_ticker_count']} |",
        f"| Fehlende erwartete Sitzungen im Fenster | {s['alpaca_missing_expected_sessions']} | {s['yahoo_missing_expected_sessions']} |",
        f"| Strukturell ungültige Bars | {s['alpaca_invalid_bars']} | {s['yahoo_invalid_bars']} |",
        "",
        f"- Beide Provider liefern den letzten erwarteten Handelstag: **{s['both_latest']}**",
        f"- Nur Alpaca liefert ihn: **{s['alpaca_only_latest']}**",
        f"- Nur Yahoo liefert ihn: **{s['yahoo_only_latest']}**",
        f"- Keiner liefert ihn: **{s['neither_latest']}**",
        "",
        "## Offene historische US-Lücken",
        "",
        f"- Betroffene US-Ticker: **{s['us_historical_gap_ticker_count']}**",
        f"- Fehlende US-Handelssitzungen: **{s['us_historical_gap_session_count']}**",
        "",
        "| Ticker | Lückentage | Alpaca geliefert | Yahoo geliefert |",
        "|---|---|---|---|",
    ]
    gaps = report.get("targeted_historical_gap_probe", {})
    if not gaps:
        lines.append("| – | Keine offenen historischen US-Lücken im Audit | – | – |")
    else:
        for ticker, providers in sorted(gaps.items()):
            requested = ", ".join(providers.get("alpaca", {}).get("requested_gap_dates", []))
            a = ", ".join(providers.get("alpaca", {}).get("returned_gap_dates", [])) or "–"
            y = ", ".join(providers.get("yahoo", {}).get("returned_gap_dates", [])) or "–"
            lines.append(f"| {ticker} | {requested} | {a} | {y} |")
    lines.extend([
        "",
        "## Einordnung",
        "",
        "- `no_rows` bedeutet: der Provider hat in diesem Request keine Bars zurückgegeben; es ist keine definitive Aussage über die generelle Symbolunterstützung.",
        "- `request_error` wird getrennt gezählt und darf nicht als fehlende Marktabdeckung interpretiert werden.",
        "- Der Vergleich prüft Abdeckung und einfache OHLCV-Struktur, keine Preisparität zwischen den Anbietern.",
        "- ^-Indexbenchmarks sind im Projektuniversum enthalten, werden aber nicht beim Alpaca-Stock-Endpunkt angefragt.",
        "- Die SQLite wurde nicht aktualisiert, repariert oder überschrieben.",
        "",
        "## Dateien",
        "",
        "- `US_Provider_Coverage.json`: vollständige Ergebnisse je Ticker und Auditdetails.",
        "- `US_Provider_Coverage.csv`: tabellarischer Vergleich je Ticker.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=365)
    args = parser.parse_args()
    if args.lookback_days < 30 or args.lookback_days > 730:
        parser.error("--lookback-days muss zwischen 30 und 730 liegen.")
    report = run_comparison(args.lookback_days)
    print(json.dumps(report["provider_comparison_summary"], ensure_ascii=False, indent=2))
    print(f"US_PROVIDER_COVERAGE_DATABASE_UNCHANGED={report['database_unchanged']}")
    print(f"US_PROVIDER_COVERAGE_TICKERS={report['us_ticker_count']}")
    if not report["database_unchanged"]:
        return 2
    summary = report["provider_comparison_summary"]
    if summary["alpaca_request_error_ticker_count"] or summary["yahoo_request_error_ticker_count"]:
        return 1  # Reports are still uploaded by the workflow's always() artifact step.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
