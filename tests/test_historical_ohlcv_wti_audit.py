from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import historical_ohlcv as ho


def _empty_gap_audit():
    return {
        "gap_tickers": [],
        "recent_gap_tickers": [],
        "historical_gap_tickers": [],
        "historical_only_gap_tickers": [],
        "calendar_unverified_tickers": [],
        "calendar_coverage_incomplete_tickers": [],
        "trading_day_gap_details": {},
    }


def _audit(tmp_path, monkeypatch, rows):
    db = tmp_path / "audit.sqlite"
    ho.init_db(db)
    with ho._connect(db) as conn:
        grouped = {}
        for ticker, date, open_, high, low, close, volume in rows:
            grouped.setdefault(ticker, []).append(
                (date, open_, high, low, close, volume)
            )
            conn.execute(
                """INSERT INTO bars
                   (ticker, date, open, high, low, close, adj_close, volume, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')""",
                (ticker, date, open_, high, low, close, close, volume),
            )
        for ticker, ticker_rows in grouped.items():
            dates = [row[0] for row in ticker_rows]
            conn.execute(
                """INSERT INTO instruments
                   (ticker, first_date, last_date, row_count, source)
                   VALUES (?, ?, ?, ?, 'test')""",
                (ticker, min(dates), max(dates), len(dates)),
            )
    universe = sorted(grouped)
    monkeypatch.setattr(ho, "discover_universe", lambda *_: universe)
    monkeypatch.setattr(ho, "_calendar_name_for_ticker", lambda _ticker: None)
    monkeypatch.setattr(ho, "_load_bar_dates_by_ticker",
                        lambda *_: {
                            ticker: sorted(row[0] for row in ticker_rows)
                            for ticker, ticker_rows in grouped.items()
                        })
    monkeypatch.setattr(ho, "_analyse_trading_day_gaps",
                        lambda **_: _empty_gap_audit())
    return ho.audit_snapshot(
        db_path=db,
        source_file=tmp_path / "unused.py",
        dynamic_universe=tmp_path / "unused.json",
        explicit_no_candidates=True,
    )


def test_audit_accepts_only_the_two_exact_wti_negative_bars(tmp_path, monkeypatch):
    result = _audit(tmp_path, monkeypatch, [
        ("CL=F", "2020-04-20", 17.73, 17.85, -40.32, -37.63, 247947),
        ("CL=F", "2020-04-21", -14.00, 13.86, -16.74, 10.01, 2288230),
    ])
    assert result["invalid_ohlcv_tickers"] == []
    assert result["historical_only_invalid_ohlcv_tickers"] == []


def test_audit_rejects_unapproved_negative_or_zero_values(tmp_path, monkeypatch):
    result = _audit(tmp_path, monkeypatch, [
        ("CL=F", "2020-04-20", 17.73, 17.85, -41.00, -37.63, 247947),
        ("CL=F", "2020-04-22", 10.0, 11.0, 0.0, 10.5, 1000),
        ("OTHER=F", "2020-04-20", 10.0, 11.0, -1.0, 10.5, 1000),
    ])
    assert result["invalid_ohlcv_tickers"] == ["CL=F", "OTHER=F"]
    assert result["historical_only_invalid_ohlcv_tickers"] == ["CL=F", "OTHER=F"]
