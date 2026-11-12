"""Forensische Regressionstests fuer die historische OHLCV-Datenbank."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def test_universe_is_discovered_without_importing_analyse(tmp_path):
    import historical_ohlcv as ho
    tickers = ho.discover_universe(ROOT / "analyse.py", tmp_path / "empty-dynamic-universe.json")
    assert len(tickers) >= 790
    for ticker in ("AAPL", "NVDA", "SAP.DE", "EXSA.DE", "^GSPC", "GC=F", "BTC-USD",
                   "VMRK", "SKYD", "ACT0.DE"):
        assert ticker in tickers
    assert not {"EA", "AVB", "EQR", "WBS", "QRVO", "WBD", "PSKY", "KVUE", "ACT.DE"}.intersection(tickers)
    assert all(ho._calendar_name_for_ticker(ticker) for ticker in tickers)


def test_database_upsert_and_load(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    idx = pd.date_range("2026-09-17", periods=2, freq="B")
    frame = pd.DataFrame({
        "Open": [100, 101], "High": [102, 103], "Low": [99, 100],
        "Close": [101, 102], "Adj Close": [101, 102], "Volume": [1000, 1100],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"TEST": frame}) == 2
    out = ho.load_history("TEST", db)
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(out) == 2
    assert float(out.iloc[-1]["Close"]) == 102.0


def test_yfinance_multilevel_extraction():
    import historical_ohlcv as ho
    idx = pd.date_range("2026-09-17", periods=2, freq="B")
    cols = pd.MultiIndex.from_product([["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    values = [[1, 2, 0, 1.5, 1.5, 10, 2, 3, 1, 2.5, 2.5, 20],
              [2, 3, 1, 2.5, 2.5, 11, 3, 4, 2, 3.5, 3.5, 21]]
    hist = pd.DataFrame(values, index=idx, columns=cols)
    out = ho._extract_ticker_frame(hist, "AAPL")
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert len(out) == 2
    assert float(out.iloc[-1]["Close"]) == 2.5


def test_update_database_bootstrap_and_incremental_without_network(tmp_path, monkeypatch):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"XLK":"Technologie"}\nsektoren_aktien={"XLK":["AAA"]}\neu_sektoren_etf={"EXSA.DE":"EU"}\ndax_aktien={"EU":["SAP.DE"]}\neu_benchmark_ticker="EXSA.DE"\ndef get_index_benchmark_yf(t,l): pass\n', encoding="utf-8")
    idx = pd.date_range("2026-09-17", periods=2, freq="B")
    frame = pd.DataFrame({"Open":[1,2],"High":[2,3],"Low":[0,1],"Close":[1.5,2.5],"Adj Close":[1.5,2.5],"Volume":[10,11]}, index=idx)
    calls=[]
    def fake_download(tickers, **kwargs):
        calls.append((tickers, kwargs))
        # The helper is intentionally called once per missing batch in this test.
        return frame if isinstance(tickers, str) else pd.concat({t: frame for t in tickers}, axis=1)
    monkeypatch.setattr(ho, "BATCH_SIZE", 10)
    monkeypatch.setattr(ho, "REQUEST_PAUSE_SECONDS", 0)
    monkeypatch.setattr(ho, "_download_batch", lambda tickers, start=None, end=None: {t: frame for t in tickers})
    stats = ho.update_database(db, source)
    assert stats["missing"] >= 2
    assert stats["updated"] >= 2
    cov = ho.coverage(db, source)
    assert (cov["Status"] == "OK").all()
    stats2 = ho.update_database(db, source)
    assert stats2["missing"] == 0


def test_dynamic_universe_registration_and_discovery(tmp_path):
    import historical_ohlcv as ho
    dynamic = tmp_path / "historical_ohlcv_universe.json"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"XLK":"Technologie"}\n', encoding="utf-8")
    assert "ZZZ.TEST" not in ho.discover_universe(source, dynamic)
    assert ho.register_ticker("zzz.test", dynamic) is True
    assert ho.register_ticker("ZZZ.TEST", dynamic) is False
    assert "ZZZ.TEST" in ho.discover_universe(source, dynamic)


def test_coverage_summary_detects_missing_dynamic_ticker(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    dynamic = tmp_path / "historical_ohlcv_universe.json"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"XLK":"Technologie"}\n', encoding="utf-8")
    ho.register_ticker("ZZZ.TEST", dynamic)
    ho.init_db(db)
    report = ho.coverage(db, source, dynamic)
    assert "ZZZ.TEST" in set(report.loc[report["Status"] == "MISSING", "Ticker"])

def test_market_cache_normalises_mixed_timezone_indices():
    import market_cache as mc

    naive = pd.DataFrame({"Close": [100.0]}, index=pd.DatetimeIndex(["2026-09-18"]))
    aware = pd.DataFrame({"Close": [101.0]}, index=pd.DatetimeIndex(["2026-09-19"], tz="UTC"))

    naive_out = mc._normalise_datetime_index(naive)
    aware_out = mc._normalise_datetime_index(aware)

    assert naive_out.index.tz is None
    assert aware_out.index.tz is None
    assert aware_out.index[0] == pd.Timestamp("2026-09-19")

    combined = pd.concat([naive_out, aware_out])
    assert combined.index.tz is None
    assert len(combined) == 2



def test_excluded_35_tickers_are_removed_from_all_universe_sources(tmp_path):
    import historical_ohlcv as ho
    source = tmp_path / "analyse.py"
    dynamic = tmp_path / "dynamic.json"
    source.write_text(
        'sektoren_map={"10O.DE":"x", "ABB":"y", "AAPL":"z"}\n'
        'sektoren_aktien={"XLRE":["AVB","EQR","VMRK"], "XLC":["EA","SKYD"]}\n'
        'dax_aktien={"Chemie":["ACT.DE","ACT0.DE"]}\n',
        encoding="utf-8",
    )
    dynamic.write_text(
        '{"tickers":["1TRA.DE", "ANSS", "EA", "AVB", "EQR", "WBS", '
        '"QRVO", "WBD", "PSKY", "KVUE", "ACT.DE", "ZZZ.TEST"]}',
        encoding="utf-8",
    )
    tickers = set(ho.discover_universe(source, dynamic))
    assert len(ho.EXCLUDED_TICKERS) == 35
    assert ho.EXCLUDED_TICKERS.isdisjoint(tickers)
    assert {"AAPL", "VMRK", "SKYD", "ACT0.DE", "ZZZ.TEST"}.issubset(tickers)
    for old_ticker in ("EA", "AVB", "EQR", "WBS", "QRVO", "WBD", "PSKY", "KVUE", "ACT.DE"):
        assert ho.register_ticker(old_ticker, dynamic) is False
        assert old_ticker not in ho.discover_universe(source, dynamic)


def test_exclusion_list_matches_35_obsolete_tickers():
    import historical_ohlcv as ho
    expected = {
        "10O.DE", "1TRA.DE", "ABB", "ACT.DE", "ANSS", "ARND.DE", "ASTA.DE",
        "AUM.DE", "AVB", "CMA", "DFS", "EA", "EQR", "FLIR", "FRAG.DE", "HES",
        "JNPR", "KCOG.DE", "KVUE", "MAXN", "MRO", "NAFG.DE", "PNE.DE", "PSAG.DE",
        "PSKY", "QRVO", "RTL.DE", "SHA.DE", "SHELL.SO", "SNV", "STM1.DE", "WBS",
        "WBD", "WRK", "X",
    }
    assert ho.EXCLUDED_TICKERS == expected


def test_audit_snapshot_separates_historical_invalid_from_current_quality_states(tmp_path, monkeypatch):
    import historical_ohlcv as ho
    db = tmp_path / "quality.sqlite"
    source = tmp_path / "universe.py"
    dynamic = tmp_path / "dynamic.json"
    source.write_text(
        'sektoren_map={"VALID":"x","MISSING":"x","STALE":"x","INVALID":"x","HISTORICAL":"x"}\n',
        encoding="utf-8",
    )
    ho.init_db(db)
    monkeypatch.setattr(
        ho, "_analyse_trading_day_gaps",
        lambda **kwargs: {
            "gap_tickers": [], "recent_gap_tickers": [],
            "historical_gap_tickers": [], "historical_only_gap_tickers": [],
            "calendar_unverified_tickers": [],
            "calendar_coverage_incomplete_tickers": [],
            "calendar_unverified_reasons": {}, "calendar_by_ticker": {},
            "trading_day_gap_details": {}, "trading_day_gap_policy_days": 90,
        },
    )
    import datetime
    today = datetime.date.today()
    recent = today - datetime.timedelta(days=1)
    old = today - datetime.timedelta(days=30)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            for ticker, date_value, values in (
                ("VALID", recent.isoformat(), (10, 12, 9, 11, 100)),
                ("STALE", old.isoformat(), (10, 12, 9, 11, 100)),
                ("INVALID", recent.isoformat(), (10, 8, 9, 11, 100)),
            ):
                conn.execute(
                    "INSERT INTO instruments(ticker, first_date, last_date, row_count, last_update_utc, source) VALUES(?,?,?,?,?,?)",
                    (ticker, date_value, date_value, 1, datetime.datetime.now(datetime.timezone.utc).isoformat(), "test"),
                )
                conn.execute(
                    "INSERT INTO bars(ticker,date,open,high,low,close,volume,source) VALUES(?,?,?,?,?,?,?,?)",
                    (ticker, date_value, *values, "test"),
                )
            historical_date = today - datetime.timedelta(days=365)
            conn.execute(
                "INSERT INTO instruments(ticker, first_date, last_date, row_count, last_update_utc, source) VALUES(?,?,?,?,?,?)",
                ("HISTORICAL", historical_date.isoformat(), recent.isoformat(), 2,
                 datetime.datetime.now(datetime.timezone.utc).isoformat(), "test"),
            )
            conn.executemany(
                "INSERT INTO bars(ticker,date,open,high,low,close,volume,source) VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("HISTORICAL", historical_date.isoformat(), 10, 8, 9, 11, 100, "test"),
                    ("HISTORICAL", recent.isoformat(), 10, 12, 9, 11, 100, "test"),
                ],
            )
            conn.commit()
    audit = ho.audit_snapshot(
        db, source, dynamic, candidate_tickers=["HISTORICAL", "INVALID"]
    )
    counts = audit["quality_status_counts"]
    assert counts["VALID"] >= 1
    assert counts["MISSING"] >= 1
    assert counts["STALE"] >= 1
    assert counts["INVALID"] >= 1
    assert counts["TOTAL"] == audit["universe_count"]
    assert sum(counts[key] for key in ("VALID", "MISSING", "STALE", "INVALID")) == audit["universe_count"]
    assert len(audit["quality_status_by_ticker"]) == audit["universe_count"]
    assert audit["quality_status_by_ticker"]["VALID"] == "VALID"
    assert audit["quality_status_by_ticker"]["MISSING"] == "MISSING"
    assert audit["quality_status_by_ticker"]["STALE"] == "STALE"
    assert audit["quality_status_by_ticker"]["INVALID"] == "INVALID"
    assert audit["quality_status_by_ticker"]["HISTORICAL"] == "VALID"
    assert audit["historical_only_invalid_ohlcv_tickers"] == ["HISTORICAL"]
    assert audit["historical_only_invalid_ohlcv_candle_count"] == 1
    assert audit["recent_invalid_ohlcv_candle_count"] == 1
    assert audit["invalid_ohlcv_candle_count"] == 2
    assert audit["affected_candidates"] == ["INVALID"]
    assert audit["candidate_impact"] == "affected_candidates_detected"
    assert audit["candidate_mapping_complete"] is True



def test_connect_closes_database_handle_after_normal_and_exceptional_exit(tmp_path):
    import historical_ohlcv as ho

    db = tmp_path / "connection-lifecycle.sqlite"
    with ho._connect(db) as conn:
        conn.execute("CREATE TABLE sample (value INTEGER)")
        conn.execute("INSERT INTO sample(value) VALUES (1)")
    closed_after_success = conn

    try:
        closed_after_success.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("SQLite-Verbindung blieb nach normalem Exit offen")

    exceptional_conn = None
    try:
        with ho._connect(db) as conn:
            exceptional_conn = conn
            raise RuntimeError("test rollback")
    except RuntimeError as exc:
        assert str(exc) == "test rollback"

    try:
        exceptional_conn.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("SQLite-Verbindung blieb nach Exception offen")



def test_calendar_resolver_uses_exchange_specific_and_continuous_market_calendars():
    import historical_ohlcv as ho

    assert ho._calendar_name_for_ticker("AAPL") == "XNYS"
    assert ho._calendar_name_for_ticker("QQQ") == "XNAS"
    assert ho._calendar_name_for_ticker("SAP.DE") == "XETR"
    assert ho._calendar_name_for_ticker("^GDAXI") == "XETR"
    assert ho._calendar_name_for_ticker("GC=F") == "COMEX"
    assert ho._calendar_name_for_ticker("BZ=F") == "IFEU"
    assert ho._calendar_name_for_ticker("EURUSD=X") == "24/5"
    assert ho._calendar_name_for_ticker("BTC-USD") == "24/7"
    assert ho._calendar_name_for_ticker("ABC.UNKNOWN") is None


def test_previous_completed_session_is_market_specific_on_us_thanksgiving():
    import historical_ohlcv as ho

    # 2026-11-26 is Thanksgiving: US exchanges are closed, Xetra is open.
    assert ho._previous_completed_session_date("XNYS", "2026-11-27") == "2026-11-25"
    assert ho._previous_completed_session_date("XETR", "2026-11-27") == "2026-11-26"
    assert ho._previous_completed_session_date("24/7", "2026-11-27") == "2026-11-26"


def test_trading_day_gap_analysis_respects_exchange_holidays_and_crypto_weekends():
    import historical_ohlcv as ho

    result = ho._analyse_trading_day_gaps(
        {
            # US Thanksgiving 2025-11-27 is not an XNYS session: no gap.
            "AAPL": {"2025-11-26", "2025-11-28", "2025-12-01"},
            # XETR traded on 2025-11-27: that absent bar is a real gap.
            "SAP.DE": {"2025-11-26", "2025-11-28", "2025-12-01"},
            # A continuous 24/7 instrument also expects Saturday/Sunday bars.
            "BTC-USD": {"2026-10-30", "2026-11-02"},
        },
        as_of_date="2026-11-05",
        recent_days=90,
    )

    assert "AAPL" not in result["gap_tickers"]
    assert "SAP.DE" in result["gap_tickers"]
    assert result["trading_day_gap_details"]["SAP.DE"]["calendar"] == "XETR"
    assert result["trading_day_gap_details"]["SAP.DE"]["missing_dates_sample"] == ["2025-11-27"]
    assert "BTC-USD" in result["recent_gap_tickers"]
    assert result["trading_day_gap_details"]["BTC-USD"]["calendar"] == "24/7"
    assert result["trading_day_gap_details"]["BTC-USD"]["missing_dates_sample"] == [
        "2026-10-31", "2026-11-01"
    ]

    # CME published no COMEX settlement on Labor Day 2026; do not classify it
    # as a missing bar even though the generic exchange calendar has a session.
    cme_result = ho._analyse_trading_day_gaps(
        {"GC=F": {"2026-09-04", "2026-09-08", "2026-09-09"}},
        as_of_date="2026-09-10",
        recent_days=90,
    )
    assert "GC=F" not in cme_result["gap_tickers"]


def test_calendar_history_before_exchange_start_is_reported_as_partial_not_unmapped():
    import historical_ohlcv as ho

    result = ho._analyse_trading_day_gaps(
        {"SAP.DE": {"1996-12-30", "1997-01-06"}},
        as_of_date="2026-11-05",
        recent_days=90,
    )

    assert result["calendar_coverage_incomplete_tickers"] == ["SAP.DE"]
    assert result["calendar_unverified_tickers"] == []
    assert result["calendar_unverified_reasons"]["SAP.DE"].endswith("1997-01-01 ab")
    assert result["gap_tickers"] == ["SAP.DE"]


def test_audit_snapshot_detects_recent_gaps_and_candidate_impact(tmp_path, monkeypatch):
    import historical_ohlcv as ho

    db = tmp_path / "gaps.sqlite"
    source = tmp_path / "universe.py"
    dynamic = tmp_path / "dynamic.json"
    source.write_text("sektoren_map={}\\n", encoding="utf-8")
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-10-26", "2026-10-28"])
    frame = pd.DataFrame(
        {
            "Open": [100, 101], "High": [102, 103], "Low": [99, 100],
            "Close": [101, 102], "Adj Close": [101, 102], "Volume": [1000, 1100],
        },
        index=idx,
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(
                conn,
                {"AAPL": frame, "SAP.DE": frame, "BTC-USD": frame},
            )
    monkeypatch.setattr(ho, "discover_universe", lambda *args, **kwargs: ["AAPL", "SAP.DE", "BTC-USD"])

    audit = ho.audit_snapshot(
        db, source, dynamic, stale_after_days=90, candidate_tickers=["AAPL"]
    )

    assert audit["status"] == "gaps"
    assert audit["recent_gap_tickers"] == ["AAPL", "BTC-USD", "SAP.DE"]
    assert audit["affected_candidates"] == ["AAPL"]
    assert audit["candidate_impact"] == "affected_candidates_detected"
    # Gap detection is an orthogonal dimension; the four legacy quality states remain disjoint.
    assert audit["quality_status_counts"] == {
        "VALID": 3, "MISSING": 0, "STALE": 0, "INVALID": 0, "TOTAL": 3
    }
    assert set(audit["quality_status_by_ticker"].values()) == {"VALID"}


def test_audit_snapshot_fails_closed_for_unknown_exchange_suffix(tmp_path, monkeypatch):
    import historical_ohlcv as ho

    db = tmp_path / "unknown-calendar.sqlite"
    source = tmp_path / "universe.py"
    dynamic = tmp_path / "dynamic.json"
    source.write_text("sektoren_map={}\\n", encoding="utf-8")
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-10-26", "2026-10-28"])
    frame = pd.DataFrame(
        {
            "Open": [100, 101], "High": [102, 103], "Low": [99, 100],
            "Close": [101, 102], "Adj Close": [101, 102], "Volume": [1000, 1100],
        },
        index=idx,
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"ABC.UNKNOWN": frame})
    monkeypatch.setattr(ho, "discover_universe", lambda *args, **kwargs: ["ABC.UNKNOWN"])

    audit = ho.audit_snapshot(
        db, source, dynamic, stale_after_days=90, candidate_tickers=["ABC.UNKNOWN"]
    )

    assert audit["status"] == "gaps"
    assert audit["calendar_unverified_tickers"] == ["ABC.UNKNOWN"]
    assert audit["affected_candidates"] == ["ABC.UNKNOWN"]
    assert "ABC.UNKNOWN" in audit["calendar_unverified_reasons"]



def test_gap_repair_inserts_only_missing_rows_and_preserves_existing_values(tmp_path):
    import historical_ohlcv as ho

    db = tmp_path / "gap-insert.sqlite"
    ho.init_db(db)
    existing = pd.DataFrame(
        {
            "Open": [100.0], "High": [102.0], "Low": [99.0],
            "Close": [101.0], "Adj Close": [101.0], "Volume": [1000.0],
        },
        index=pd.DatetimeIndex(["2026-11-02"]),
    )
    downloaded = pd.DataFrame(
        {
            "Open": [900.0, 102.0], "High": [902.0, 104.0], "Low": [899.0, 101.0],
            "Close": [901.0, 103.0], "Adj Close": [901.0, 103.0], "Volume": [9000.0, 1100.0],
        },
        index=pd.DatetimeIndex(["2026-11-02", "2026-11-03"]),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"AAPL": existing}) == 1
            assert ho._insert_missing_frames(conn, {"AAPL": downloaded}) == 1

    result = ho.load_history("AAPL", db)
    assert len(result) == 2
    assert float(result.loc[pd.Timestamp("2026-11-02"), "Close"]) == 101.0
    assert float(result.loc[pd.Timestamp("2026-11-03"), "Close"]) == 103.0


def test_recent_gap_detector_uses_exchange_sessions_and_detects_missing_session(tmp_path, monkeypatch):
    import historical_ohlcv as ho

    db = tmp_path / "gap-detection.sqlite"
    ho.init_db(db)
    frame = pd.DataFrame(
        {
            "Open": [100.0, 102.0, 103.0], "High": [102.0, 104.0, 105.0],
            "Low": [99.0, 101.0, 102.0], "Close": [101.0, 103.0, 104.0],
            "Adj Close": [101.0, 103.0, 104.0], "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=pd.DatetimeIndex(["2026-11-02", "2026-11-04", "2026-11-05"]),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"AAPL": frame})

    monkeypatch.setattr(
        ho, "_previous_completed_session_date",
        lambda calendar_name, as_of_date: "2026-11-05",
    )
    result = ho._detect_recent_trading_day_gaps(
        db, ["AAPL"], lookback_days=4, as_of_date="2026-11-06"
    )

    assert result["gap_dates_by_ticker"] == {"AAPL": ["2026-11-03"]}
    assert result["gap_session_count"] == 1


def test_update_database_automatically_repairs_recent_gap_without_overwriting_existing_data(
    tmp_path, monkeypatch
):
    import historical_ohlcv as ho

    db = tmp_path / "self-healing.sqlite"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"AAPL":"US"}\n', encoding="utf-8")
    ho.init_db(db)
    initial = pd.DataFrame(
        {
            "Open": [100.0, 102.0, 103.0], "High": [102.0, 104.0, 105.0],
            "Low": [99.0, 101.0, 102.0], "Close": [101.0, 103.0, 104.0],
            "Adj Close": [101.0, 103.0, 104.0], "Volume": [1000.0, 1100.0, 1200.0],
        },
        index=pd.DatetimeIndex(["2026-11-02", "2026-11-04", "2026-11-05"]),
    )
    repaired = pd.DataFrame(
        {
            "Open": [101.0], "High": [103.0], "Low": [100.0],
            "Close": [102.0], "Adj Close": [102.0], "Volume": [1050.0],
        },
        index=pd.DatetimeIndex(["2026-11-03"]),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"AAPL": initial})

    monkeypatch.setattr(ho, "discover_universe", lambda *args, **kwargs: ["AAPL"])
    monkeypatch.setattr(
        ho, "_previous_completed_session_date",
        lambda calendar_name, as_of_date: "2026-11-05",
    )

    # update_database() uses the real current date when it invokes the recent
    # gap detector. Keep this integration fixture anchored to the same
    # reference date as the standalone detector test above; otherwise the
    # 2026-11-03 gap eventually falls outside the four-calendar-day lookback.
    original_gap_detector = ho._detect_recent_trading_day_gaps

    def fixed_reference_date_gap_detector(
        db_path, tickers, *, lookback_days, as_of_date=None
    ):
        return original_gap_detector(
            db_path, tickers, lookback_days=lookback_days,
            as_of_date="2026-11-06",
        )

    monkeypatch.setattr(
        ho, "_detect_recent_trading_day_gaps", fixed_reference_date_gap_detector
    )
    calls = []

    def fake_download(tickers, start=None, end=None):
        calls.append((list(tickers), start, end))
        return {"AAPL": repaired}

    monkeypatch.setattr(ho, "_download_batch", fake_download)
    monkeypatch.setattr(ho, "REQUEST_PAUSE_SECONDS", 0)

    stats = ho.update_database(
        db, source, repair_gaps=True, gap_lookback_days=4
    )

    history = ho.load_history("AAPL", db)
    assert len(history) == 4
    assert float(history.loc[pd.Timestamp("2026-11-02"), "Close"]) == 101.0
    assert float(history.loc[pd.Timestamp("2026-11-03"), "Close"]) == 102.0
    assert stats["gap_sessions_found"] == 1
    assert stats["gap_rows_added"] == 1
    assert stats["gap_sessions_remaining"] == 0
    assert stats["gap_tickers_remaining"] == 0
    assert calls == [(["AAPL"], "2026-11-03", "2026-11-04")]


def test_recent_gap_detector_skips_unmapped_calendars_and_known_nonpublication_sessions(
    tmp_path, monkeypatch
):
    import historical_ohlcv as ho

    db = tmp_path / "calendar-skip.sqlite"
    ho.init_db(db)
    frame = pd.DataFrame(
        {
            "Open": [100.0, 101.0], "High": [102.0, 103.0],
            "Low": [99.0, 100.0], "Close": [101.0, 102.0],
            "Adj Close": [101.0, 102.0], "Volume": [1000.0, 1100.0],
        },
        index=pd.DatetimeIndex(["2026-09-04", "2026-09-08"]),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"GC=F": frame, "ABC.UNKNOWN": frame})

    monkeypatch.setattr(
        ho, "_previous_completed_session_date",
        lambda calendar_name, as_of_date: "2026-09-08",
    )
    result = ho._detect_recent_trading_day_gaps(
        db, ["GC=F", "ABC.UNKNOWN"], lookback_days=10, as_of_date="2026-09-09"
    )

    assert "ABC.UNKNOWN" in result["calendar_unverified_tickers"]
    assert "GC=F" not in result["gap_dates_by_ticker"]
    assert "GC=F" not in result["calendar_unverified_tickers"]


def test_crypto_and_additional_exchange_calendar_mappings():
    import historical_ohlcv as ho

    assert ho._calendar_name_for_ticker("ETH-USD") == "24/7"
    assert ho._calendar_name_for_ticker("SOL-USD") == "24/7"
    assert ho._calendar_name_for_ticker("005930.KS") == "XKRX"
    assert ho._calendar_name_for_ticker("DSV.CO") == "XCSE"
    assert ho._calendar_name_for_ticker("IBE.MC") == "XMAD"



def test_current_analyse_sector_universe_uses_replacement_tickers():
    import ast
    from pathlib import Path

    source_path = Path(__file__).resolve().parents[1] / "analyse.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"sektoren_aktien", "dax_aktien"}:
                    assignments[target.id] = ast.literal_eval(node.value)

    us_tickers = {
        ticker
        for values in assignments["sektoren_aktien"].values()
        for ticker in values
    }
    eu_tickers = {
        ticker
        for values in assignments["dax_aktien"].values()
        for ticker in values
    }

    assert {"VMRK", "SKYD"}.issubset(us_tickers)
    assert "ACT0.DE" in eu_tickers
    assert not {"EA", "AVB", "EQR", "WBS", "QRVO", "WBD", "PSKY", "KVUE"}.intersection(us_tickers)
    assert "ACT.DE" not in eu_tickers


def test_expected_daily_bar_dates_apply_only_confirmed_calendar_overrides():
    import historical_ohlcv as ho

    cme_dates = {
        "2025-11-27", "2026-01-19", "2026-02-16", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07",
    }
    assert ho._expected_daily_bar_dates("COMEX", cme_dates) == set()
    assert ho._expected_daily_bar_dates("NYMEX", cme_dates) == set()
    assert ho._expected_daily_bar_dates("ICEUS", cme_dates) == cme_dates
    assert ho._expected_daily_bar_dates("XKRX", {"2026-06-03", "2026-07-17"}) == set()
    assert ho._expected_daily_bar_dates("24/5", {"2025-12-25", "2026-01-01"}) == set()


def test_known_exchange_closures_and_cme_nonpublication_days_are_not_gap_sessions():
    import historical_ohlcv as ho

    import exchange_calendars as exchange_cal

    krx_calendar = exchange_cal.get_calendar("XKRX", start="2026-06-01", end="2026-07-20")
    krx_dates = {session.date().isoformat() for session in krx_calendar.sessions}
    fx_calendar = exchange_cal.get_calendar("24/5", start="2025-12-22", end="2026-01-02")
    fx_dates = {session.date().isoformat() for session in fx_calendar.sessions}

    result = ho._analyse_trading_day_gaps(
        {
            # CME did not disseminate COMEX/NYMEX settlement prices on these dates.
            "GC=F": {"2026-01-16", "2026-01-20"},
            "CL=F": {"2026-02-13", "2026-02-17"},
            "HG=F": {"2026-05-22", "2026-05-26"},
            "PL=F": {"2026-06-18", "2026-06-22"},
            "PA=F": {"2026-07-02", "2026-07-06"},
            # All KRX sessions are present except the two confirmed closures.
            "005930.KS": krx_dates - {"2026-06-03", "2026-07-17"},
            # All 24/5 weekdays are present except Christmas and New Year's Day.
            "EURUSD=X": fx_dates - {"2025-12-25", "2026-01-01"},
        },
        as_of_date="2026-11-06",
        recent_days=365,
    )

    assert not {"GC=F", "CL=F", "HG=F", "PL=F", "PA=F", "005930.KS", "EURUSD=X"}.intersection(
        result["gap_tickers"]
    )


def test_real_sessions_remain_gaps_even_when_they_fall_on_us_or_european_holidays():
    import historical_ohlcv as ho

    result = ho._analyse_trading_day_gaps(
        {
            # ICE Futures Europe Brent has trading and settlement on US holidays.
            "BZ=F": {"2026-01-16", "2026-01-20"},
            # Xetra explicitly trades on Ascension Day and Whit Monday in 2026.
            "^STOXX": {
                "2026-05-13", "2026-05-15", "2026-05-18", "2026-05-19",
                "2026-05-20", "2026-05-21", "2026-05-22", "2026-05-26",
            },
            # Ordinary Copenhagen and Xetra sessions: these remain genuine gaps.
            "DSV.CO": {"2026-03-20", "2026-03-24"},
            "PPFB.DE": {"2026-03-05", "2026-03-09"},
        },
        as_of_date="2026-11-06",
        recent_days=365,
    )

    assert result["trading_day_gap_details"]["BZ=F"]["missing_dates_sample"] == ["2026-01-19"]
    assert result["trading_day_gap_details"]["^STOXX"]["missing_dates_sample"] == [
        "2026-05-14", "2026-05-25"
    ]
    assert result["trading_day_gap_details"]["DSV.CO"]["missing_dates_sample"] == ["2026-03-23"]
    assert result["trading_day_gap_details"]["PPFB.DE"]["missing_dates_sample"] == ["2026-03-06"]


def test_recent_gap_detector_applies_market_closure_overrides(tmp_path, monkeypatch):
    import historical_ohlcv as ho

    db = tmp_path / "krx-holiday.sqlite"
    ho.init_db(db)
    calendar = __import__("exchange_calendars").get_calendar(
        "XKRX", start="2026-05-26", end="2026-06-04"
    )
    dates = [session for session in calendar.sessions if session.date().isoformat() != "2026-06-03"]
    frame = pd.DataFrame(
        {
            "Open": [100.0] * len(dates), "High": [102.0] * len(dates),
            "Low": [99.0] * len(dates), "Close": [101.0] * len(dates),
            "Adj Close": [101.0] * len(dates), "Volume": [1000.0] * len(dates),
        },
        index=pd.DatetimeIndex(dates),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"005930.KS": frame})

    monkeypatch.setattr(
        ho, "_previous_completed_session_date",
        lambda calendar_name, as_of_date: "2026-06-04",
    )
    result = ho._detect_recent_trading_day_gaps(
        db, ["005930.KS"], lookback_days=10, as_of_date="2026-06-05"
    )

    assert result["gap_dates_by_ticker"] == {}
    assert result["gap_session_count"] == 0



def test_ifeu_calendar_uses_ice_brent_schedule_not_us_or_london_proxy_days():
    import historical_ohlcv as ho

    sessions = set(ho._calendar_session_dates("IFEU", "2026-01-01", "2026-12-31"))

    # ICE's 2026 IFEU schedule marks these US/UK holidays as trading days
    # for Brent, including early-close sessions (A/B/C/D).
    expected_open = {
        "2026-01-19", "2026-02-16", "2026-04-06", "2026-05-04",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-08-31",
        "2026-09-07", "2026-11-26", "2026-11-27", "2026-12-28",
    }
    assert expected_open.issubset(sessions)

    # ICE's annual schedule marks New Year's Day, Good Friday and Christmas
    # as full IFEU closures. 26 December remains open for Brent.
    assert not {"2026-01-01", "2026-04-03", "2026-12-25"}.intersection(sessions)
    assert "2025-12-26" in ho._calendar_session_dates("IFEU", "2025-12-24", "2025-12-29")
    assert "2026-12-28" in sessions

    sessions_2027 = set(ho._calendar_session_dates("IFEU", "2027-03-25", "2027-12-28"))
    assert "2027-03-26" not in sessions_2027  # Good Friday
    assert "2027-03-29" in sessions_2027     # Easter Monday: Brent trading day
    assert "2027-12-27" in sessions_2027     # Boxing Day observed: Brent trading day
    assert ho._calendar_session_dates("IFEU", "2027-12-30", "2028-01-04") == [
        "2027-12-30", "2027-12-31"
    ]
    assert ho._previous_completed_session_date("IFEU", "2028-01-01") == "2027-12-31"
    assert ho._previous_completed_session_date("IFEU", "2028-01-04") is None


def test_ifeu_previous_completed_session_respects_brent_calendar():
    import historical_ohlcv as ho

    # Thanksgiving Thursday is a shortened IFEU Brent trading day, not a closure.
    assert ho._previous_completed_session_date("IFEU", "2026-11-27") == "2026-11-26"
    # On Good Friday, the prior completed Brent session is Thursday.
    assert ho._previous_completed_session_date("IFEU", "2026-04-04") == "2026-04-02"


def test_ifeu_gap_analysis_keeps_us_holiday_sessions_as_real_gaps():
    import historical_ohlcv as ho

    result = ho._analyse_trading_day_gaps(
        {"BZ=F": {"2026-01-16", "2026-01-20"}},
        as_of_date="2026-01-21",
        recent_days=30,
    )

    assert result["calendar_by_ticker"]["BZ=F"] == "IFEU"
    assert result["trading_day_gap_details"]["BZ=F"]["missing_dates_sample"] == [
        "2026-01-19"
    ]


def test_ifeu_recent_gap_detector_uses_ifeu_sessions(tmp_path):
    import historical_ohlcv as ho

    db = tmp_path / "ifeu-gap.sqlite"
    ho.init_db(db)
    frame = pd.DataFrame(
        {
            "Open": [100.0, 102.0], "High": [102.0, 104.0],
            "Low": [99.0, 101.0], "Close": [101.0, 103.0],
            "Adj Close": [101.0, 103.0], "Volume": [1000.0, 1100.0],
        },
        index=pd.DatetimeIndex(["2026-01-16", "2026-01-20"]),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"BZ=F": frame})

    result = ho._detect_recent_trading_day_gaps(
        db, ["BZ=F"], lookback_days=10, as_of_date="2026-01-21"
    )

    assert result["gap_dates_by_ticker"] == {"BZ=F": ["2026-01-19"]}
    assert result["gap_session_count"] == 1


def test_ifeu_calendar_marks_history_outside_verified_period_as_incomplete():
    import historical_ohlcv as ho

    result = ho._analyse_trading_day_gaps(
        {"BZ=F": {"2024-12-31", "2025-01-02", "2026-01-16", "2026-01-20"}},
        as_of_date="2026-11-06",
        recent_days=365,
    )

    assert result["calendar_coverage_incomplete_tickers"] == ["BZ=F"]
    assert result["calendar_unverified_tickers"] == []
    assert "2025-01-01" not in result["trading_day_gap_details"].get("BZ=F", {}).get(
        "missing_dates_sample", []
    )



def test_update_database_does_not_fallback_to_generic_bday_outside_ifeu_coverage(
    tmp_path, monkeypatch
):
    import historical_ohlcv as ho

    db = tmp_path / "ifeu-outside-coverage.sqlite"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"BZ=F":"Brent"}\n', encoding="utf-8")
    ho.init_db(db)
    frame = pd.DataFrame(
        {
            "Open": [100.0], "High": [102.0], "Low": [99.0],
            "Close": [101.0], "Adj Close": [101.0], "Volume": [1000.0],
        },
        index=pd.DatetimeIndex(["2027-12-31"]),
    )
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            ho._upsert_frames(conn, {"BZ=F": frame})

    real_date = ho.dt.date

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return cls(2028, 1, 4)

    monkeypatch.setattr(ho.dt, "date", FrozenDate)
    monkeypatch.setattr(ho, "discover_universe", lambda *args, **kwargs: ["BZ=F"])
    download_calls = []

    def fail_if_downloaded(*args, **kwargs):
        download_calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(ho, "_download_batch", fail_if_downloaded)
    monkeypatch.setattr(ho, "REQUEST_PAUSE_SECONDS", 0)

    stats = ho.update_database(db, source, repair_gaps=True, gap_lookback_days=10)

    assert download_calls == []
    assert stats["gap_calendar_unverified"] == 1


def test_upsert_rejects_structurally_invalid_ohlc_without_storing_it(tmp_path, capsys):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    idx = pd.date_range("2026-11-03", periods=3, freq="B")
    frame = pd.DataFrame({
        "Open": [10.0, 11.0, 12.0],
        "High": [12.0, 10.5, 13.0],
        "Low": [9.0, 10.0, 11.0],
        "Close": [11.0, 11.5, 12.5],
        "Adj Close": [11.0, 11.5, 12.5],
        "Volume": [100, 110, 120],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"TEST": frame}) == 2
        rows = conn.execute(
            "SELECT date, open, high, low, close FROM bars ORDER BY date"
        ).fetchall()
    assert [row[0] for row in rows] == ["2026-11-03", "2026-11-05"]
    assert "TEST: 1 strukturell ungueltige Kerze" in capsys.readouterr().err


def test_invalid_upsert_cannot_replace_existing_valid_bar(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05"])
    valid = pd.DataFrame({
        "Open": [10.0], "High": [12.0], "Low": [9.0], "Close": [11.0],
        "Adj Close": [11.0], "Volume": [100],
    }, index=idx)
    invalid = valid.copy()
    invalid.loc[idx[0], "Low"] = 11.5
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"TEST": valid}) == 1
            assert ho._upsert_frames(conn, {"TEST": invalid}) == 0
        row = conn.execute(
            "SELECT open, high, low, close FROM bars WHERE ticker='TEST' AND date='2026-11-05'"
        ).fetchone()
    assert row == (10.0, 12.0, 9.0, 11.0)


def test_insert_missing_rejects_invalid_bars_and_keeps_existing_rows(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05", "2026-11-06"])
    initial = pd.DataFrame({
        "Open": [10.0], "High": [12.0], "Low": [9.0],
        "Close": [11.0], "Adj Close": [11.0], "Volume": [100],
    }, index=idx[:1])
    incoming = pd.DataFrame({
        "Open": [10.0, 20.0], "High": [12.0, 19.0], "Low": [9.0, 18.0],
        "Close": [11.0, 20.0], "Adj Close": [11.0, 20.0], "Volume": [100, 200],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"TEST": initial}) == 1
            assert ho._insert_missing_frames(conn, {"TEST": incoming}) == 0
        rows = conn.execute(
            "SELECT date, open, high, low, close FROM bars ORDER BY date"
        ).fetchall()
    assert rows == [("2026-11-05", 10.0, 12.0, 9.0, 11.0)]


def test_ohlcv_validator_rejects_non_positive_and_non_finite_prices():
    import historical_ohlcv as ho
    base = pd.Series({"Open": 10.0, "High": 12.0, "Low": 9.0, "Close": 11.0})
    non_positive = base.copy()
    non_positive["Open"] = 0.0
    assert ho._ohlcv_validation_reasons(non_positive) == ["open_non_positive"]
    non_finite = base.copy()
    non_finite["Close"] = float("inf")
    assert ho._ohlcv_validation_reasons(non_finite) == ["close_not_finite"]
    inverted_range = base.copy()
    inverted_range["High"] = 8.0
    assert "high_below_low" in ho._ohlcv_validation_reasons(inverted_range)


def test_invalid_only_ticker_does_not_receive_fresh_instrument_metadata(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05"])
    valid = pd.DataFrame({
        "Open": [10.0], "High": [12.0], "Low": [9.0], "Close": [11.0],
        "Adj Close": [11.0], "Volume": [100],
    }, index=idx)
    invalid = valid.copy()
    invalid.loc[idx[0], "High"] = 8.0
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"GOOD": valid, "BAD": invalid}) == 1
        tickers = {row[0] for row in conn.execute("SELECT ticker FROM instruments")}
    assert tickers == {"GOOD"}


def test_volume_validation_rejects_missing_negative_and_non_finite_values():
    import historical_ohlcv as ho

    base = pd.Series({
        "Open": 10.0, "High": 12.0, "Low": 9.0, "Close": 11.0,
        "Adj Close": 11.0, "Volume": 100.0,
    })
    missing = base.copy()
    missing["Volume"] = float("nan")
    assert ho._ohlcv_validation_reasons(missing) == ["volume_missing"]

    negative = base.copy()
    negative["Volume"] = -1.0
    assert ho._ohlcv_validation_reasons(negative) == ["volume_negative"]

    infinite = base.copy()
    infinite["Volume"] = float("inf")
    assert ho._ohlcv_validation_reasons(infinite) == ["volume_not_finite"]

    zero = base.copy()
    zero["Volume"] = 0.0
    assert ho._ohlcv_validation_reasons(zero) == []


def test_upsert_rejects_invalid_volume_without_overwriting_valid_bar(tmp_path, capsys):
    import historical_ohlcv as ho

    db = tmp_path / "volume-validation.sqlite"
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05", "2026-11-06", "2026-11-09"])
    valid = pd.DataFrame({
        "Open": [10.0, 10.0, 10.0],
        "High": [12.0, 12.0, 12.0],
        "Low": [9.0, 9.0, 9.0],
        "Close": [11.0, 11.0, 11.0],
        "Adj Close": [11.0, 11.0, 11.0],
        "Volume": [100.0, 100.0, 100.0],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"TEST": valid}) == 3
        malformed = valid.copy()
        malformed.loc[idx[0], "Volume"] = -10.0
        malformed.loc[idx[1], "Volume"] = float("inf")
        malformed.loc[idx[2], "Volume"] = float("nan")
        with conn:
            assert ho._upsert_frames(conn, {"TEST": malformed}) == 0
        rows = conn.execute(
            "SELECT date, volume FROM bars WHERE ticker='TEST' ORDER BY date"
        ).fetchall()

    assert rows == [
        ("2026-11-05", 100.0),
        ("2026-11-06", 100.0),
        ("2026-11-09", 100.0),
    ]
    warning = capsys.readouterr().err
    assert "volume_negative" in warning
    assert "volume_not_finite" in warning
    assert "volume_missing" in warning


def test_insert_missing_rejects_invalid_volume(tmp_path):
    import historical_ohlcv as ho

    db = tmp_path / "volume-repair-validation.sqlite"
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05", "2026-11-06"])
    frame = pd.DataFrame({
        "Open": [10.0, 10.0], "High": [12.0, 12.0],
        "Low": [9.0, 9.0], "Close": [11.0, 11.0],
        "Adj Close": [11.0, 11.0], "Volume": [-1.0, 100.0],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._insert_missing_frames(conn, {"TEST": frame}) == 1
        rows = conn.execute(
            "SELECT date, volume FROM bars WHERE ticker='TEST' ORDER BY date"
        ).fetchall()
    assert rows == [("2026-11-06", 100.0)]


def test_update_database_updated_stat_excludes_tickers_with_only_invalid_volume(
    tmp_path, monkeypatch
):
    import historical_ohlcv as ho

    db = tmp_path / "invalid-volume-only.sqlite"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"AAPL":"US"}\n', encoding="utf-8")
    idx = pd.DatetimeIndex(["2026-11-05"])
    malformed = pd.DataFrame({
        "Open": [10.0], "High": [12.0], "Low": [9.0],
        "Close": [11.0], "Adj Close": [11.0], "Volume": [-1.0],
    }, index=idx)
    monkeypatch.setattr(ho, "discover_universe", lambda *args, **kwargs: ["AAPL"])
    monkeypatch.setattr(ho, "_download_batch", lambda tickers, start=None, end=None: {"AAPL": malformed})
    monkeypatch.setattr(ho, "REQUEST_PAUSE_SECONDS", 0)

    stats = ho.update_database(db, source, repair_gaps=False)

    assert stats["rows"] == 0
    assert stats["updated"] == 0
    assert ho.load_history("AAPL", db).empty


def test_gap_repair_updated_stat_counts_only_tickers_with_inserted_rows(tmp_path):
    import historical_ohlcv as ho

    db = tmp_path / "gap-updated-stat.sqlite"
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05"])
    frame = pd.DataFrame({
        "Open": [10.0], "High": [12.0], "Low": [9.0],
        "Close": [11.0], "Adj Close": [11.0], "Volume": [100.0],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"TEST": frame}) == 1
            updated = set()
            assert ho._insert_missing_frames(
                conn, {"TEST": frame}, updated_tickers=updated
            ) == 0
        assert updated == set()


def test_update_database_gap_repair_does_not_count_already_stored_rows_as_updated(
    tmp_path, monkeypatch
):
    import historical_ohlcv as ho

    db = tmp_path / "gap-duplicate-stat.sqlite"
    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"AAPL":"US"}\n', encoding="utf-8")
    ho.init_db(db)
    idx = pd.DatetimeIndex(["2026-11-05"])
    existing = pd.DataFrame({
        "Open": [10.0], "High": [12.0], "Low": [9.0],
        "Close": [11.0], "Adj Close": [11.0], "Volume": [100.0],
    }, index=idx)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            assert ho._upsert_frames(conn, {"AAPL": existing}) == 1

    scans = iter([
        {
            "gap_dates_by_ticker": {"AAPL": ["2026-11-05"]},
            "gap_ticker_count": 1,
            "gap_session_count": 1,
            "calendar_unverified_tickers": [],
        },
        {
            "gap_dates_by_ticker": {},
            "gap_ticker_count": 0,
            "gap_session_count": 0,
            "calendar_unverified_tickers": [],
        },
    ])
    monkeypatch.setattr(ho, "discover_universe", lambda *args, **kwargs: ["AAPL"])
    monkeypatch.setattr(ho, "_detect_recent_trading_day_gaps", lambda *args, **kwargs: next(scans))
    # Isolate the gap-repair path: the pre-existing bar is deliberately the
    # latest completed session, so the normal stale-update path must not run.
    monkeypatch.setattr(
        ho, "_previous_completed_session_date",
        lambda calendar_name, as_of_date: "2026-11-05",
    )
    download_calls = []

    def download_gap_only(tickers, start=None, end=None):
        download_calls.append((list(tickers), start, end))
        return {"AAPL": existing}

    monkeypatch.setattr(ho, "_download_batch", download_gap_only)
    monkeypatch.setattr(ho, "REQUEST_PAUSE_SECONDS", 0)

    stats = ho.update_database(db, source, repair_gaps=True)

    assert download_calls == [(["AAPL"], "2026-11-05", "2026-11-06")]

    assert stats["gap_tickers_attempted"] == 1
    assert stats["gap_rows_added"] == 0
    assert stats["updated"] == 0



def test_open_positions_csv_registers_only_unique_open_tickers(tmp_path):
    import csv
    import historical_ohlcv as ho

    positions = tmp_path / "Offene_Positionen.csv"
    rows = [
        {"Ticker": " aaa.de ", "Status": " Offen "},
        {"Ticker": "AAA.DE", "Status": "offen"},
        {"Ticker": "CLOSED.DE", "Status": "Verkauft"},
        {"Ticker": "ANLEITUNG", "Status": "Offen"},
        {"Ticker": "XAU/USD", "Status": "Offen"},
    ]
    with positions.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Ticker", "Status"], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    assert ho.load_open_position_tickers(positions) == ["AAA.DE", "XAU/USD"]


def test_open_position_csv_fails_closed_for_missing_ticker(tmp_path):
    import csv
    import historical_ohlcv as ho
    import pytest

    positions = tmp_path / "Offene_Positionen.csv"
    with positions.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Ticker", "Status"], delimiter=";")
        writer.writeheader()
        writer.writerow({"Ticker": "", "Status": "Offen"})
        writer.writerow({"Ticker": "", "Status": "Verkauft"})

    with pytest.raises(ValueError, match="ohne gueltigen Ticker"):
        ho.load_open_position_tickers(positions)


def test_sync_open_positions_registers_and_bootstraps_only_open_tickers(tmp_path, monkeypatch):
    import csv
    import json
    import sqlite3
    import historical_ohlcv as ho

    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"BASE.DE":"Basis"}\n', encoding="utf-8")
    positions = tmp_path / "Offene_Positionen.csv"
    with positions.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Ticker", "Status"], delimiter=";")
        writer.writeheader()
        writer.writerows([
            {"Ticker": "AAA.DE", "Status": "Offen"},
            {"Ticker": "AAA.DE", "Status": "Offen"},
            {"Ticker": "CLOSED.DE", "Status": "Verkauft"},
            {"Ticker": "XAU/USD", "Status": "Offen"},
        ])

    idx = pd.DatetimeIndex(["2026-11-10", "2026-11-11"])
    frame = pd.DataFrame({
        "Open": [100.0, 101.0], "High": [102.0, 103.0],
        "Low": [99.0, 100.0], "Close": [101.0, 102.0],
        "Adj Close": [101.0, 102.0], "Volume": [1000.0, 1100.0],
    }, index=idx)
    download_calls = []

    def fake_download(tickers, start=None, end=None):
        download_calls.append((list(tickers), start, end))
        return {ticker: frame.copy() for ticker in tickers}

    monkeypatch.setattr(ho, "_download_batch", fake_download)
    monkeypatch.setattr(ho, "_previous_completed_session_date", lambda cal, as_of: "2026-11-11")
    monkeypatch.setattr(ho, "REQUEST_PAUSE_SECONDS", 0)

    db = tmp_path / "historical_ohlcv.sqlite"
    dynamic = tmp_path / "historical_ohlcv_universe.json"
    stats = ho.sync_open_positions(
        db, source, dynamic, positions, repair_gaps=False
    )

    assert stats["open_position_tickers"] == 2
    assert stats["registered"] == 2
    assert stats["deferred_provider_tickers"] == 1
    assert download_calls == [(["AAA.DE"], None, None)]
    payload = json.loads(dynamic.read_text(encoding="utf-8"))
    assert payload["tickers"] == ["AAA.DE", "XAU/USD"]

    with sqlite3.connect(db) as conn:
        stored = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT ticker, first_date, last_date, row_count FROM instruments"
            )
        }
    assert set(stored) == {"AAA.DE", "XAU/USD"}
    assert stored["AAA.DE"][2] == 2
    assert stored["XAU/USD"] == (None, None, 0)
    assert stats["instrument_placeholders_created"] == 2
    assert "CLOSED.DE" not in payload["tickers"]


def test_update_database_only_tickers_rejects_unregistered_symbols(tmp_path):
    import pytest
    import historical_ohlcv as ho

    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"BASE.DE":"Basis"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="nicht registrierte Ticker"):
        ho.update_database(
            tmp_path / "db.sqlite", source, tmp_path / "dynamic.json",
            only_tickers=["UNKNOWN.DE"], repair_gaps=False,
        )


def test_persist_quality_accepted_history_records_provider_and_rejects_failed_gate(tmp_path):
    import sqlite3
    import pytest
    import historical_ohlcv as ho

    idx = pd.bdate_range("2026-08-20", periods=60)
    close = pd.Series(range(100, 160), index=idx, dtype=float)
    frame = pd.DataFrame({
        "Open": close - 0.5, "High": close + 1.0,
        "Low": close - 1.0, "Close": close,
        "Adj Close": close, "Volume": 1000.0,
    }, index=idx)
    frame.attrs["ohlcv_quality"] = {
        "quality_status": "PASS", "latest_bar_valid": True, "last_3_bars_valid": True,
    }

    db = tmp_path / "persist.sqlite"
    dynamic = tmp_path / "dynamic.json"
    stored = ho.persist_quality_accepted_history(
        "AAA.DE", frame, source="Yahoo-Fallback", db_path=db, dynamic_universe=dynamic
    )
    assert stored == 60
    with sqlite3.connect(db) as conn:
        bar_sources = {row[0] for row in conn.execute("SELECT DISTINCT source FROM bars")}
        instrument_source = conn.execute(
            "SELECT source FROM instruments WHERE ticker='AAA.DE'"
        ).fetchone()[0]
    assert bar_sources == {"Yahoo-Fallback"}
    assert instrument_source == "Yahoo-Fallback"

    rejected = frame.copy()
    rejected.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE", "latest_bar_valid": False,
        "last_3_bars_valid": False,
    }
    with pytest.raises(ValueError, match="nicht freigegeben"):
        ho.persist_quality_accepted_history(
            "BAD.DE", rejected, source="Yahoo-Fallback",
            db_path=db, dynamic_universe=dynamic,
        )
    assert "BAD.DE" not in ho._load_dynamic_universe(dynamic)



def test_update_database_defers_gold_spot_to_twelvedata_without_yahoo_request(tmp_path, monkeypatch):
    import historical_ohlcv as ho

    source = tmp_path / "analyse.py"
    source.write_text('sektoren_map={"BASE.DE":"Basis"}\n', encoding="utf-8")
    dynamic = tmp_path / "dynamic.json"
    ho.register_ticker("XAU/USD", dynamic)

    def forbidden_yahoo(*args, **kwargs):
        raise AssertionError("XAU/USD darf nicht an yfinance uebergeben werden")

    monkeypatch.setattr(ho, "_download_batch", forbidden_yahoo)
    stats = ho.update_database(
        tmp_path / "db.sqlite", source, dynamic,
        only_tickers=["XAU/USD"], repair_gaps=True,
    )

    assert stats["deferred_provider_tickers"] == 1
    assert stats["provider_tickers"] == 0
    assert stats["errors"] == 0
    assert stats["missing_after"] == 0



def test_corrupt_dynamic_universe_fails_closed_without_overwriting(tmp_path):
    import historical_ohlcv as ho
    import pytest

    dynamic = tmp_path / "dynamic.json"
    original = b'{"version":1,"tickers":["KEEP.DE"], broken'
    dynamic.write_bytes(original)
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        ho.register_ticker("NEW.DE", dynamic)
    assert dynamic.read_bytes() == original


def test_dynamic_universe_rejects_unknown_schema_without_overwriting(tmp_path):
    import historical_ohlcv as ho
    import pytest

    dynamic = tmp_path / "dynamic.json"
    original = '{"version":99,"tickers":["KEEP.DE"]}\n'
    dynamic.write_text(original, encoding="utf-8")
    with pytest.raises(RuntimeError, match="unbekanntes Schema"):
        ho.register_ticker("NEW.DE", dynamic)
    assert dynamic.read_text(encoding="utf-8") == original


def test_open_position_placeholder_is_created_without_fabricating_bars(tmp_path):
    import sqlite3
    import historical_ohlcv as ho

    db = tmp_path / "history.sqlite"
    created = ho._ensure_instrument_placeholders(db, ["NEW.DE", "XAU/USD"])
    assert created == 2
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ticker, first_date, last_date, row_count, source FROM instruments ORDER BY ticker"
        ).fetchall()
        bars = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    assert rows == [
        ("NEW.DE", None, None, 0, "pending-provider"),
        ("XAU/USD", None, None, 0, "pending-provider"),
    ]
    assert bars == 0


def test_verify_open_positions_fails_when_history_is_missing(tmp_path):
    import csv
    import historical_ohlcv as ho

    positions = tmp_path / "Offene_Positionen.csv"
    with positions.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Ticker", "Status"], delimiter=";")
        writer.writeheader()
        writer.writerow({"Ticker": "MISSING.DE", "Status": "Offen"})
    report = ho.verify_open_positions(tmp_path / "empty.sqlite", positions)
    assert report["ok"] is False
    assert report["failures"]["MISSING.DE"] == ["NO_STORED_BARS"]


def test_verify_open_positions_accepts_current_quality_gated_history(tmp_path, monkeypatch):
    import csv
    import historical_ohlcv as ho

    positions = tmp_path / "Offene_Positionen.csv"
    with positions.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Ticker", "Status"], delimiter=";")
        writer.writeheader()
        writer.writerow({"Ticker": "READY.DE", "Status": "Offen"})
    frame = pd.DataFrame(
        {
            "Open": [100.0] * 60,
            "High": [102.0] * 60,
            "Low": [99.0] * 60,
            "Close": [101.0] * 60,
            "Adj Close": [101.0] * 60,
            "Volume": [1000.0] * 60,
        },
        index=pd.bdate_range("2026-08-01", periods=60),
    )
    frame.attrs["ohlcv_quality"] = {
        "quality_status": "PASS",
        "latest_bar_valid": True,
        "last_3_bars_valid": True,
    }
    monkeypatch.setattr(ho, "load_history", lambda ticker, db_path: frame.copy())
    monkeypatch.setattr(
        "ohlcv_quality_gate._last_completed_session_date",
        lambda ticker: frame.index.max().date(),
    )

    report = ho.verify_open_positions(tmp_path / "unused.sqlite", positions)

    assert report["ok"] is True
    assert report["checked"] == 1
    assert report["results"]["READY.DE"]["rows"] == 60
    assert report["failures"] == {}
