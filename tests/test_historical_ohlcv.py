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


def test_audit_snapshot_returns_four_disjoint_quality_states(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "quality.sqlite"
    source = tmp_path / "universe.py"
    dynamic = tmp_path / "dynamic.json"
    source.write_text('sektoren_map={"VALID":"x","MISSING":"x","STALE":"x","INVALID":"x"}\n', encoding="utf-8")
    ho.init_db(db)
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
            conn.commit()
    audit = ho.audit_snapshot(db, source, dynamic, candidate_tickers=[], explicit_no_candidates=True)
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
    assert ho._calendar_name_for_ticker("BZ=F") == "ICEUS"
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
