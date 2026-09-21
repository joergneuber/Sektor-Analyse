"""Forensische Regressionstests fuer die historische OHLCV-Datenbank."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def test_universe_is_discovered_without_importing_analyse():
    import historical_ohlcv as ho
    tickers = ho.discover_universe(ROOT / "analyse.py")
    assert len(tickers) >= 800
    for ticker in ("AAPL", "NVDA", "SAP.DE", "EXSA.DE", "^GSPC", "GC=F", "BTC-USD"):
        assert ticker in tickers


def test_database_upsert_and_load(tmp_path):
    import historical_ohlcv as ho
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    idx = pd.date_range("2026-09-17", periods=2, freq="B")
    frame = pd.DataFrame({
        "Open": [100, 101], "High": [102, 103], "Low": [99, 100],
        "Close": [101, 102], "Adj Close": [101, 102], "Volume": [1000, 1100],
    }, index=idx)
    with sqlite3.connect(db) as conn:
        assert ho._upsert_frames(conn, {"TEST": frame}) == 2
        conn.commit()
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
