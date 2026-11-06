from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "diagnose_historical_ohlcv_release.py"
SPEC = importlib.util.spec_from_file_location("ohlcv_release_diagnostic", SCRIPT)
DIAG = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(DIAG)


def test_probe_yahoo_responses_classifies_each_ticker(monkeypatch):
    def fake_download(tickers, *, start=None, end=None):
        assert start == "2026-11-02"
        assert end == "2026-11-05"
        return {
            "AAA": pd.DataFrame({"Close": [1.0, 1.1]}, index=pd.to_datetime(["2026-11-02", "2026-11-04"])),
            "BBB": pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-11-03"])),
            "CCC": pd.DataFrame(),
        }

    monkeypatch.setattr(DIAG.historical_ohlcv, "_download_batch", fake_download)
    monkeypatch.setattr(DIAG, "YAHOO_PROBE_BATCH_SIZE", 20)
    gaps = {
        "AAA": ["2026-11-02", "2026-11-04"],
        "BBB": ["2026-11-02", "2026-11-03"],
        "CCC": ["2026-11-04"],
    }
    result = DIAG.probe_yahoo_gap_responses(gaps)
    assert result["status_counts"] == {
        "all_missing_dates_returned": 1,
        "partial_missing_dates_returned": 1,
        "no_rows": 1,
    }
    assert result["tickers"]["BBB"]["missing_dates_returned"] == ["2026-11-03"]
    assert result["tickers"]["BBB"]["missing_dates_not_returned"] == ["2026-11-02"]


def test_probe_yahoo_batch_exception_is_recorded(monkeypatch):
    def fail_download(*args, **kwargs):
        raise TimeoutError("Yahoo timeout")

    monkeypatch.setattr(DIAG.historical_ohlcv, "_download_batch", fail_download)
    result = DIAG.probe_yahoo_gap_responses({"AAA": ["2026-11-02"]})
    assert result["tickers"]["AAA"]["yahoo_status"] == "request_error"
    assert "Yahoo timeout" in result["tickers"]["AAA"]["yahoo_error"]


def test_detail_outputs_contain_one_csv_row_per_missing_date(tmp_path, monkeypatch):
    monkeypatch.setattr(DIAG, "GAP_DETAIL_JSON", tmp_path / "detail.json")
    monkeypatch.setattr(DIAG, "GAP_DETAIL_CSV", tmp_path / "detail.csv")
    monkeypatch.setattr(DIAG, "GAP_DETAIL_MD", tmp_path / "detail.md")
    gap_audit = {
        "lookback_start": "2025-11-06", "lookback_end": "2026-11-05",
        "gap_ticker_count": 1, "gap_session_count": 2,
        "calendar_unverified_tickers": [], "calendar_unverified_reasons": {},
    }
    probe = {
        "status_counts": {"partial_missing_dates_returned": 1},
        "tickers": {"AAA": {
            "ticker": "AAA", "calendar": "XNYS",
            "missing_dates": ["2026-11-02", "2026-11-03"],
            "requested_start": "2026-11-02", "requested_end_exclusive": "2026-11-04",
            "yahoo_status": "partial_missing_dates_returned", "returned_row_count": 1,
            "returned_first_date": "2026-11-03", "returned_last_date": "2026-11-03",
            "missing_dates_returned": ["2026-11-03"], "yahoo_error": None,
        }},
    }
    DIAG.write_gap_detail_outputs(gap_audit, probe)
    assert (tmp_path / "detail.json").is_file()
    assert len((tmp_path / "detail.csv").read_text(encoding="utf-8-sig").splitlines()) == 3
    report = (tmp_path / "detail.md").read_text(encoding="utf-8")
    assert "partial_missing_dates_returned" in report


def test_recent_gap_scan_includes_trailing_sessions_without_mutating_db(tmp_path):
    import hashlib
    import historical_ohlcv as ho

    db = tmp_path / "release.sqlite"
    ho.init_db(db)
    with ho._connect(db) as conn:
        for day in ("2026-11-03", "2026-11-05"):
            conn.execute(
                "INSERT INTO bars(ticker,date,open,high,low,close,adj_close,volume,source) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("AAA", day, 10, 11, 9, 10, 10, 100, "test"),
            )
        conn.execute(
            "INSERT INTO instruments(ticker,first_date,last_date,row_count,source) "
            "VALUES('AAA','2026-11-03','2026-11-05',2,'test')"
        )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = ho._detect_recent_trading_day_gaps(
        db, ["AAA"], lookback_days=30, as_of_date="2026-11-06"
    )
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert result["gap_dates_by_ticker"]["AAA"] == ["2026-11-04"]
    assert before == after
