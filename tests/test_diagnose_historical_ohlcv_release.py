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


def test_collect_invalid_candle_details_reports_exact_reasons_and_90_day_split(tmp_path):
    import datetime as dt
    import historical_ohlcv as ho

    db = tmp_path / "invalid-detail.sqlite"
    ho.init_db(db)
    recent = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    historical = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    with ho._connect(db) as conn:
        conn.execute(
            "INSERT INTO instruments(ticker,first_date,last_date,row_count,source) VALUES(?,?,?,?,?)",
            ("BAD", historical, recent, 2, "test"),
        )
        conn.executemany(
            "INSERT INTO bars(ticker,date,open,high,low,close,adj_close,volume,source) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("BAD", recent, 10, 8, 9, 11, 11, -1, "test"),
                ("BAD", historical, 10, 8, 9, 11, 11, 100, "test"),
            ],
        )
        conn.commit()

    result = DIAG.collect_invalid_candle_details(db, ["BAD"])
    assert result["ticker_count"] == 1
    assert result["invalid_candle_count"] == 2
    assert result["recent_90d_count"] == 1
    assert result["historical_only_count"] == 1
    recent_row = next(row for row in result["rows"] if row["date"] == recent)
    assert recent_row["invalid_reasons"] == "volume_negative|high_below_low|high_below_open|high_below_close"
    assert result["tickers"][0]["first_invalid_date"] == historical
    assert result["tickers"][0]["last_invalid_date"] == recent


def test_collect_missing_instrument_details_includes_absent_tickers(tmp_path):
    import historical_ohlcv as ho

    db = tmp_path / "missing-detail.sqlite"
    ho.init_db(db)
    with ho._connect(db) as conn:
        conn.execute(
            "INSERT INTO instruments(ticker,first_date,last_date,row_count,source) VALUES(?,?,?,?,?)",
            ("HASDATA", "2026-01-01", "2026-01-02", 1, "test"),
        )
        conn.execute(
            "INSERT INTO bars(ticker,date,open,high,low,close,adj_close,volume,source) VALUES(?,?,?,?,?,?,?,?,?)",
            ("HASDATA", "2026-01-02", 10, 11, 9, 10, 10, 100, "test"),
        )
        conn.commit()
    probe = {
        "ABSENT": {"provider_status": "provider_has_recent_data", "provider_row_count": 2},
        "HASDATA": {"provider_status": "no_provider_rows", "provider_row_count": 0},
    }
    rows = DIAG.collect_missing_instrument_details(db, ["ABSENT", "HASDATA"], probe)
    assert [row["ticker"] for row in rows] == ["ABSENT", "HASDATA"]
    assert rows[0]["instrument_record_exists"] == 0
    assert rows[0]["bar_count"] == 0
    assert rows[0]["provider_status"] == "provider_has_recent_data"
    assert rows[1]["instrument_record_exists"] == 1
    assert rows[1]["bar_count"] == 1


def test_probe_yahoo_missing_tickers_isolates_provider_errors(monkeypatch):
    def fake_download(tickers, *, start=None, end=None):
        ticker = tickers[0]
        if ticker == "ERR":
            raise TimeoutError("provider timeout")
        if ticker == "EMPTY":
            return {ticker: pd.DataFrame()}
        return {ticker: pd.DataFrame({"Close": [10.0]}, index=pd.to_datetime(["2026-11-05"]))}

    monkeypatch.setattr(DIAG.historical_ohlcv, "_download_batch", fake_download)
    result = DIAG.probe_yahoo_missing_tickers(["HASDATA", "EMPTY", "ERR"])
    assert result["HASDATA"]["provider_status"] == "provider_has_recent_data"
    assert result["HASDATA"]["provider_row_count"] == 1
    assert result["EMPTY"]["provider_status"] == "no_provider_rows"
    assert result["ERR"]["provider_status"] == "request_error"
    assert "provider timeout" in result["ERR"]["provider_error"]


def test_quality_detail_outputs_write_all_requested_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(DIAG, "INVALID_DETAIL_CSV", tmp_path / "invalid.csv")
    monkeypatch.setattr(DIAG, "INVALID_SUMMARY_JSON", tmp_path / "invalid.json")
    monkeypatch.setattr(DIAG, "MISSING_INSTRUMENTS_CSV", tmp_path / "missing.csv")
    monkeypatch.setattr(DIAG, "QUALITY_REPORT_MD", tmp_path / "quality.md")
    invalid = {
        "ticker_count": 1, "invalid_candle_count": 1, "recent_90d_count": 1,
        "historical_only_count": 0, "reason_counts": {"high_below_open": 1},
        "tickers": [{"ticker": "BAD", "invalid_candle_count": 1,
                     "recent_90d_count": 1, "historical_only_count": 0}],
        "rows": [{"ticker": "BAD", "date": "2026-11-05", "open": 10,
                  "high": 8, "low": 9, "close": 9, "volume": 100,
                  "source": "test", "invalid_reasons": "high_below_open", "recent_90d": 1}],
    }
    missing = [{"ticker": "NEW", "instrument_record_exists": 0, "bar_count": 0,
                "last_date": None, "provider_status": "provider_has_recent_data", "provider_row_count": 2}]
    DIAG.write_quality_detail_outputs(invalid, missing)
    assert all((tmp_path / name).is_file() for name in
               ("invalid.csv", "invalid.json", "missing.csv", "quality.md"))
    assert "high_below_open" in (tmp_path / "quality.md").read_text(encoding="utf-8")
    assert "provider_has_recent_data" in (tmp_path / "missing.csv").read_text(encoding="utf-8-sig")


def test_main_runs_quality_details_against_copy_and_keeps_release_db_unchanged(tmp_path, monkeypatch):
    import hashlib
    import historical_ohlcv as ho

    source_db = tmp_path / "release.sqlite"
    ho.init_db(source_db)
    old = "2026-01-01"
    with ho._connect(source_db) as conn:
        conn.execute(
            "INSERT INTO instruments(ticker,first_date,last_date,row_count,source) VALUES(?,?,?,?,?)",
            ("BAD", old, old, 1, "test"),
        )
        conn.execute(
            "INSERT INTO bars(ticker,date,open,high,low,close,adj_close,volume,source) VALUES(?,?,?,?,?,?,?,?,?)",
            ("BAD", old, 10, 8, 9, 11, 11, 100, "test"),
        )
        conn.commit()
    source_file = tmp_path / "analyse.py"
    source_file.write_text('sektoren_map={"BAD":"x","NEW":"x"}\n', encoding="utf-8")
    before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    for name, value in {
        "ROOT": tmp_path,
        "SOURCE_DB": source_db,
        "AUDIT_DB": tmp_path / "audit-copy.sqlite",
        "REPORT": tmp_path / "diagnostic.json",
        "SOURCE_FILE": source_file,
        "DYNAMIC_UNIVERSE": tmp_path / "universe.json",
        "GAP_DETAIL_JSON": tmp_path / "gap.json",
        "GAP_DETAIL_CSV": tmp_path / "gap.csv",
        "GAP_DETAIL_MD": tmp_path / "gap.md",
        "INVALID_DETAIL_CSV": tmp_path / "invalid.csv",
        "INVALID_SUMMARY_JSON": tmp_path / "invalid.json",
        "MISSING_INSTRUMENTS_CSV": tmp_path / "missing.csv",
        "QUALITY_REPORT_MD": tmp_path / "quality.md",
    }.items():
        monkeypatch.setattr(DIAG, name, value)

    audit = {
        "status": "gaps", "error": None, "universe": ["BAD", "NEW"],
        "invalid_ohlcv_tickers": ["BAD"], "missing_tickers": ["NEW"],
        "quality_status_counts": {"VALID": 0, "MISSING": 1, "STALE": 0, "INVALID": 1, "TOTAL": 2},
    }
    monkeypatch.setattr(ho, "audit_snapshot", lambda **kwargs: audit)
    monkeypatch.setattr(DIAG, "probe_yahoo_missing_tickers", lambda tickers: {
        "NEW": {"provider_status": "provider_has_recent_data", "provider_row_count": 1}
    })
    monkeypatch.setattr(ho, "_detect_recent_trading_day_gaps", lambda *args, **kwargs: {
        "gap_ticker_count": 0, "gap_session_count": 0,
        "lookback_start": "2026-01-01", "lookback_end": "2026-11-06",
        "gap_dates_by_ticker": {}, "calendar_unverified_tickers": [],
        "calendar_unverified_reasons": {},
    })
    monkeypatch.setattr(DIAG, "probe_yahoo_gap_responses", lambda gaps: {
        "probe_type": "current_yahoo_read_only_probe", "status_counts": {},
        "batch_errors": [], "tickers": {},
    })

    assert DIAG.main() == 0
    after = hashlib.sha256(source_db.read_bytes()).hexdigest()
    assert before == after
    assert not (tmp_path / "audit-copy.sqlite").exists()
    assert all((tmp_path / name).is_file() for name in
               ("diagnostic.json", "invalid.csv", "invalid.json", "missing.csv", "quality.md"))
    result = __import__("json").loads((tmp_path / "diagnostic.json").read_text(encoding="utf-8"))
    assert result["source_unchanged"] is True
    assert result["invalid_detail_summary"]["invalid_candle_count"] == 1
    assert result["missing_instrument_details"][0]["ticker"] == "NEW"
