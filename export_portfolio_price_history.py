#!/usr/bin/env python3
"""Export verified daily OHLCV rows for positions active since a system-start date.

The script is deliberately an exporter, not a performance calculator. It reads
positions from the production Google Drive master via performance_benchmark
and reads historical bars from the restored SQLite release in read-only mode.
It never downloads prices, repairs data, changes the database, or substitutes
underlying prices for an option's own quote.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

BENCHMARK_TICKER = "EUNL.DE"
OPTION_TYPES = {"optionsschein", "option", "warrant"}
OPEN_STATUSES = {"offen", "open", "aktiv", "active"}
CLOSED_STATUSES = {"gestoppt", "verkauft", "geschlossen", "closed", "sold", "stopped"}


def parse_date(value: Any) -> dt.date | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = pd.to_datetime(str(value).strip(), dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _text(row: pd.Series, name: str) -> str:
    value = row.get(name, "")
    return "" if pd.isna(value) else str(value).strip()


def select_relevant_positions(positions: pd.DataFrame, start: dt.date, end: dt.date) -> tuple[list[dict], list[dict]]:
    required = {"Ticker", "Status", "Einstiegsdatum", "Richtung"}
    missing = sorted(required - set(positions.columns))
    if missing:
        raise ValueError(f"Positionsquelle hat fehlende Pflichtspalten: {', '.join(missing)}")

    selected: list[dict] = []
    issues: list[dict] = []
    for row_number, row in positions.iterrows():
        ticker = _text(row, "Ticker").upper()
        if not ticker or ticker == "NAN":
            continue
        status = _text(row, "Status").casefold()
        entry_date = parse_date(row.get("Einstiegsdatum"))
        exit_date = parse_date(row.get("Ausstiegsdatum"))
        if status not in OPEN_STATUSES | CLOSED_STATUSES:
            issues.append({"row": int(row_number), "ticker": ticker, "issue": f"Unbekannter Status: {_text(row, 'Status')}"})
            continue
        if entry_date is None:
            issues.append({"row": int(row_number), "ticker": ticker, "issue": "Einstiegsdatum fehlt/ungültig"})
            continue
        if entry_date > end or (exit_date is not None and exit_date < start):
            continue
        if status in CLOSED_STATUSES and exit_date is None:
            issues.append({"row": int(row_number), "ticker": ticker, "issue": "Geschlossene Position ohne Ausstiegsdatum"})
            continue
        if status in OPEN_STATUSES and exit_date is not None:
            issues.append({"row": int(row_number), "ticker": ticker, "issue": "Offene Position besitzt ein Ausstiegsdatum"})
            continue

        product_type = _text(row, "Produkt_Typ").casefold()
        is_option = product_type in OPTION_TYPES or bool(_text(row, "OS_WKN"))
        active_from = max(entry_date, start)
        active_to = min(exit_date, end) if exit_date else end
        status_at_cutoff = (
            "CLOSED_BY_CUTOFF" if exit_date is not None and exit_date <= end
            else "OPEN_AT_CUTOFF"
        )
        selected.append({
            "source_row": int(row_number),
            "ticker": ticker,
            "name": _text(row, "Name"),
            "status": _text(row, "Status"),
            "status_at_cutoff": status_at_cutoff,
            "direction": _text(row, "Richtung"),
            "entry_date": entry_date.isoformat(),
            "entry_price": _text(row, "Einstieg"),
            "exit_date": exit_date.isoformat() if exit_date else "",
            "exit_price": _text(row, "Ausstiegskurs"),
            "current_price": _text(row, "Aktueller_Kurs"),
            "product_type": _text(row, "Produkt_Typ"),
            "os_wkn": _text(row, "OS_WKN"),
            "os_entry_price": _text(row, "OS_Einstiegskurs"),
            "os_current_price": _text(row, "OS_Aktueller_Kurs"),
            "os_performance_pct": _text(row, "OS_Performance%"),
            "active_from": active_from.isoformat(),
            "active_to": active_to.isoformat(),
            "ohlcv_required": not is_option,
            "benchmark_position": ticker == BENCHMARK_TICKER,
            "note": "Optionsschein: nur eigener OS-Kurs zulässig; Basiswert-OHLCV nicht zur OS-Rendite verwenden" if is_option else "",
        })

    # EUNL is also always included as benchmark series. If it is actually held,
    # its real position row remains in selected exactly once; this synthetic
    # marker is only a market-data request and is not a second portfolio trade.
    if not any(item["ticker"] == BENCHMARK_TICKER for item in selected):
        selected.append({
            "source_row": -1, "ticker": BENCHMARK_TICKER,
            "name": "iShares Core MSCI World UCITS ETF USD (Acc)",
            "status": "BENCHMARK_ONLY", "status_at_cutoff": "BENCHMARK_ONLY", "direction": "Long",
            "entry_date": start.isoformat(), "entry_price": "",
            "exit_date": "", "exit_price": "", "current_price": "",
            "product_type": "ETF", "os_wkn": "", "os_entry_price": "",
            "os_current_price": "", "os_performance_pct": "",
            "active_from": start.isoformat(), "active_to": end.isoformat(),
            "ohlcv_required": True, "benchmark_position": True,
            "note": "Nur Benchmark-Marktpreisreihe; keine synthetische Portfolio-Position",
        })
    return selected, issues


def read_prices(db_path: Path, tickers: list[str], start: dt.date, end: dt.date) -> tuple[list[dict], list[dict]]:
    if not db_path.is_file():
        raise FileNotFoundError(f"OHLCV-Datenbank fehlt: {db_path}")
    if not tickers:
        return [], []
    placeholders = ",".join("?" for _ in tickers)
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=60) as conn:
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"bars", "instruments"}.issubset(tables):
            raise RuntimeError("OHLCV-Schema unvollständig: bars/instruments fehlen")
        bars = conn.execute(
            f"""SELECT b.ticker,b.date,b.open,b.high,b.low,b.close,b.adj_close,b.volume,b.source
                FROM bars b WHERE b.ticker IN ({placeholders}) AND b.date >= ? AND b.date <= ?
                ORDER BY b.ticker,b.date""",
            (*tickers, start.isoformat(), end.isoformat()),
        ).fetchall()
        instrument_rows = conn.execute(
            f"SELECT ticker,first_date,last_date,row_count,last_update_utc,source FROM instruments WHERE ticker IN ({placeholders}) ORDER BY ticker",
            tuple(tickers),
        ).fetchall()
    price_rows = [dict(zip(("ticker","date","open","high","low","close","adj_close","volume","source"), row)) for row in bars]
    instrument_rows_out = [dict(zip(("ticker","first_date","last_date","row_count","last_update_utc","source"), row)) for row in instrument_rows]
    return price_rows, instrument_rows_out


def export(db_path: Path, positions: pd.DataFrame, start: dt.date, end: dt.date, out_dir: Path) -> dict:
    if start > end:
        raise ValueError("Startdatum liegt nach Enddatum")
    selected, issues = select_relevant_positions(positions, start, end)
    required_tickers = sorted({item["ticker"] for item in selected if item["ohlcv_required"]})
    price_rows, instruments = read_prices(db_path, required_tickers, start, end)
    rows_by_ticker: dict[str, list[dict]] = {ticker: [] for ticker in required_tickers}
    for row in price_rows:
        rows_by_ticker.setdefault(row["ticker"], []).append(row)

    coverage = []
    for ticker in required_tickers:
        rows = rows_by_ticker.get(ticker, [])
        coverage.append({
            "ticker": ticker,
            "status": "BARS_PRESENT" if rows else "NO_BARS_IN_WINDOW",
            "bar_count": len(rows),
            "first_bar": rows[0]["date"] if rows else None,
            "last_bar": rows[-1]["date"] if rows else None,
            "missing_expected_dates_not_assessed": True,
            "quality_gate_not_run": True,
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "portfolio_positions.csv", selected)
    _write_csv(out_dir / "portfolio_historical_prices.csv", price_rows)
    _write_csv(out_dir / "portfolio_instrument_coverage.csv", coverage)
    _write_csv(out_dir / "portfolio_instruments_metadata.csv", instruments)
    (out_dir / "portfolio_export_issues.json").write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": "EXPORTED_WITH_ISSUES" if issues or any(x["status"] != "BARS_PRESENT" for x in coverage) else "EXPORTED",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "position_rows_exported": len(selected),
        "ohlcv_tickers_requested": required_tickers,
        "price_rows_exported": len(price_rows),
        "issues_count": len(issues),
        "coverage": coverage,
        "calculation_performed": False,
        "quality_gate_performed": False,
        "source_database": db_path.name,
        "source_tables": ["bars", "instruments"],
        "price_fields": ["close", "adj_close"],
        "price_adjustment_note": "Close and Adj Close are both exported. No dividend/currency/adjustment assumptions are applied by this exporter.",
    }
    (out_dir / "portfolio_export_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("historical_ohlcv.sqlite"))
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out-dir", type=Path, default=Path("portfolio_price_export"))
    args = parser.parse_args()
    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    from performance_benchmark import _load_positions
    positions = _load_positions()
    summary = export(args.db, positions, start, end, args.out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # Coverage gaps are part of the requested result, not an exporter crash.
    # The machine-readable summary carries EXPORTED_WITH_ISSUES for fail-closed review.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
