#!/usr/bin/env python3
"""Read-only forensic comparison: Alpaca SIP split-adjusted bars vs Yahoo OHLCV."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path.cwd()
DB_PATH = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DB", "historical_ohlcv.sqlite"))
SOURCE_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_SOURCE", "analyse.py"))
DYNAMIC_UNIVERSE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE", "historical_ohlcv_universe.json"))
OUT_JSON = ROOT / "US_Split_Adjustment_Diagnostic.json"
OUT_CSV = ROOT / "US_Split_Adjustment_Diagnostic.csv"
OUT_MD = ROOT / "US_Split_Adjustment_Diagnostic.md"
BATCH_SIZE = 100


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_universe() -> tuple[list[str], str]:
    import historical_ohlcv
    if not DB_PATH.is_file() or not SOURCE_FILE.is_file() or not DYNAMIC_UNIVERSE.is_file():
        raise FileNotFoundError("historical_ohlcv.sqlite, analyse.py und historical_ohlcv_universe.json müssen vorhanden sein.")
    before = sha256(DB_PATH)
    with tempfile.TemporaryDirectory(prefix="split-adjustment-audit-") as tmp:
        copy = Path(tmp) / "audit.sqlite"
        shutil.copy2(DB_PATH, copy)
        if sha256(copy) != before:
            raise RuntimeError("SQLite-Prüfkopie ist nicht bytegleich.")
        audit = historical_ohlcv.audit_snapshot(db_path=copy, source_file=SOURCE_FILE, dynamic_universe=DYNAMIC_UNIVERSE)
        if audit.get("status") == "unavailable" or audit.get("error"):
            raise RuntimeError(f"Universums-Audit fehlgeschlagen: {audit.get('error')}")
        calendars = audit.get("calendar_by_ticker", {})
        tickers = sorted(t for t, cal in calendars.items() if cal in {"XNYS", "XNAS"} and not t.startswith("^"))
    if sha256(DB_PATH) != before:
        raise RuntimeError("Original-SQLite hat sich während des Audits verändert.")
    if not tickers:
        raise RuntimeError("US-Aktien/ETF-Universum ist leer.")
    return tickers, before


def fetch_split_adjusted(client: Any, tickers: list[str], start: dt.datetime, end: dt.datetime):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    import provider_coverage_us as base
    frames: dict[str, pd.DataFrame] = {}
    statuses: dict[str, str] = {}
    aliases = {"BRK-B": "BRK.B", "BF-B": "BF.B"}
    def request_chunk(symbols: list[str]):
        if not symbols:
            return
        req_symbols = [aliases.get(s, s) for s in symbols]
        try:
            req = StockBarsRequest(symbol_or_symbols=req_symbols, start=start, end=end,
                timeframe=TimeFrame.Day, adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)
            raw = client.get_stock_bars(req).df
            parsed = base._normalise_alpaca_frames(raw, symbols, req_symbols)
            for s in symbols:
                if s in parsed and not parsed[s].empty:
                    frames[s] = parsed[s]; statuses[s] = "rows_returned"
                else:
                    statuses[s] = "no_rows"
        except Exception as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:220]}"
            if "invalid symbol" in str(exc).lower() and len(symbols) > 1:
                mid = len(symbols)//2; request_chunk(symbols[:mid]); request_chunk(symbols[mid:]); return
            for s in symbols: statuses[s] = "request_error: " + msg
    for i in range(0, len(tickers), BATCH_SIZE):
        request_chunk(tickers[i:i+BATCH_SIZE])
    return frames, statuses


def run(lookback_days: int = 365) -> dict[str, Any]:
    from alpaca.data.historical import StockHistoricalDataClient
    import provider_coverage_us as base
    key, secret = os.environ.get("ALPACA_KEY", "").strip(), os.environ.get("ALPACA_SECRET", "").strip()
    if not key or not secret: raise RuntimeError("ALPACA_KEY oder ALPACA_SECRET fehlt.")
    tickers, db_hash_before = load_universe()
    today = dt.datetime.now(dt.timezone.utc).date()
    start_day = today - dt.timedelta(days=lookback_days)
    start_dt = dt.datetime.combine(start_day, dt.time.min)
    end_dt = dt.datetime.combine(today, dt.time.min)  # last completed UTC date only
    alpaca = StockHistoricalDataClient(key, secret)
    alpaca_frames, alpaca_status = fetch_split_adjusted(alpaca, tickers, start_dt, end_dt)
    yahoo_frames, yahoo_status = base.fetch_yahoo(tickers, start_day.isoformat(), today.isoformat())
    rows = []
    for ticker in tickers:
        a, y = alpaca_frames.get(ticker), yahoo_frames.get(ticker)
        comparison = base._compare_ohlcv_frames(a, y, "Alpaca SIP SPLIT", "Yahoo auto_adjust=False")
        rows.append({"ticker": ticker, "alpaca_status": alpaca_status.get(ticker, "missing_status"),
            "yahoo_status": yahoo_status.get(ticker, "missing_status"),
            "alpaca_bars": len(a) if a is not None else 0, "yahoo_bars": len(y) if y is not None else 0,
            **comparison})
    db_hash_after = sha256(DB_PATH)
    summary = {
        "ticker_count": len(tickers),
        "alpaca_rows": sum(x["alpaca_status"] == "rows_returned" for x in rows),
        "alpaca_no_rows": sum(x["alpaca_status"] == "no_rows" for x in rows),
        "alpaca_errors": sum(x["alpaca_status"].startswith("request_error") for x in rows),
        "yahoo_rows": sum(x["yahoo_status"] == "rows_returned" for x in rows),
        "compared": sum(x["status"] == "compared" for x in rows),
        "close_mean_diff_gt_0_1_pct": sum((x.get("close_mean_abs_diff_pct") or 0) > .1 for x in rows),
        "close_mean_diff_gt_1_pct": sum((x.get("close_mean_abs_diff_pct") or 0) > 1 for x in rows),
        "ema20_diff_gt_0_1_pct": sum((x.get("ema20_last_abs_diff_pct") or 0) > .1 for x in rows),
        "atr14_diff_gt_1_pct": sum((x.get("atr14_last_abs_diff_pct") or 0) > 1 for x in rows),
        "lookback_start": start_day.isoformat(), "end_exclusive": today.isoformat(),
        "alpaca_adjustment": "SPLIT", "alpaca_feed": "SIP historical", "yahoo_auto_adjust": False,
        "database_sha256_before": db_hash_before, "database_sha256_after": db_hash_after,
        "database_unchanged": db_hash_before == db_hash_after,
    }
    report = {"diagnostic": "US split adjustment forensic comparison", "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "summary": summary, "ticker_results": rows}
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False)
    lines = ["# US Split-Adjustment Diagnostic", "", "## Ergebnis", "", f"- Symbole: {summary['ticker_count']}",
        f"- Vergleichbar: {summary['compared']}", f"- Close-Mittelabweichung >0,1 %: {summary['close_mean_diff_gt_0_1_pct']}",
        f"- Close-Mittelabweichung >1 %: {summary['close_mean_diff_gt_1_pct']}",
        f"- EMA20-Abweichung >0,1 %: {summary['ema20_diff_gt_0_1_pct']}",
        f"- ATR14-Abweichung >1 %: {summary['atr14_diff_gt_1_pct']}",
        f"- SQLite unverändert: {summary['database_unchanged']}", "", "## Größte Close-Abweichungen", "",
        "| Ticker | Status | gemeinsame Bars | Close Ø-Abw. % | Close Max % | EMA20 % | ATR14 % |", "|---|---:|---:|---:|---:|---:|---:|"]
    for x in sorted(rows, key=lambda z: z.get("close_mean_abs_diff_pct") or -1, reverse=True)[:30]:
        lines.append(f"| {x['ticker']} | {x['status']} | {x['overlap_bars']} | {x.get('close_mean_abs_diff_pct')} | {x.get('close_max_abs_diff_pct')} | {x.get('ema20_last_abs_diff_pct')} | {x.get('atr14_last_abs_diff_pct')} |")
    lines += ["", "## Interpretation", "", "Dieser Lauf vergleicht Alpaca SIP mit `adjustment=SPLIT` gegen Yahoo über den bestehenden `auto_adjust=False`-Pfad. Er ändert weder produktive Dateien noch SQLite-Daten. Eine geringe Restabweichung kann aus Feed-/Aggregationsunterschieden stammen; der Vergleich allein beweist keine einzelne Corporate Action.", ""]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    if not summary["database_unchanged"]: raise RuntimeError("Original-SQLite wurde verändert.")
    return report

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--lookback-days", type=int, default=365); a = p.parse_args()
    if not 30 <= a.lookback_days <= 730: p.error("lookback-days muss 30–730 sein")
    result = run(a.lookback_days)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
