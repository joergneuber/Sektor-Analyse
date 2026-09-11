"""Regression tests fuer den prozessuebergreifenden Markt-Daten-Cache."""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_shared_batch_reuses_existing_cache(tmp_path, monkeypatch):
    import market_cache as mc
    mc = importlib.reload(mc)
    monkeypatch.setattr(mc, "CACHE_FILE", tmp_path / "market_cache.json")
    monkeypatch.setattr(mc, "LOCK_FILE", tmp_path / "market_cache.json.lock")

    index = pd.date_range("2026-09-01", periods=2, freq="D")
    cached = pd.DataFrame({"Open": [99.0, 100.0], "High": [101.0, 102.0],
                           "Low": [98.0, 99.0], "Close": [100.0, 101.0],
                           "Volume": [10, 11]}, index=index)
    mc.get_or_fetch_dataframe("yf:CL=F", lambda: cached)

    calls = []
    fake_yf = types.SimpleNamespace(download=lambda *args, **kwargs: calls.append((args, kwargs)) or cached)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    out = mc.get_yf_histories(["CL=F"])
    assert "CL=F" in out
    assert float(out["CL=F"]["Close"].iloc[-1]) == 101.0
    assert calls == []


def test_shared_batch_downloads_only_missing_tickers(tmp_path, monkeypatch):
    import market_cache as mc
    mc = importlib.reload(mc)
    monkeypatch.setattr(mc, "CACHE_FILE", tmp_path / "market_cache.json")
    monkeypatch.setattr(mc, "LOCK_FILE", tmp_path / "market_cache.json.lock")

    index = pd.date_range("2026-09-01", periods=2, freq="D")
    a = pd.DataFrame({"Open": [1, 2], "High": [2, 3], "Low": [0, 1], "Close": [1.5, 2.5], "Volume": [10, 11]}, index=index)
    b = pd.DataFrame({"Open": [10, 20], "High": [20, 30], "Low": [9, 19], "Close": [15, 25], "Volume": [12, 13]}, index=index)
    mc.get_or_fetch_dataframe("yf:CL=F", lambda: a)

    calls = []
    def fake_download(tickers, **kwargs):
        calls.append((list(tickers), kwargs))
        cols = pd.MultiIndex.from_product([["Close"], ["GC=F"]])
        return pd.DataFrame([[14], [25]], index=index, columns=cols)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=fake_download))

    out = mc.get_yf_histories(["CL=F", "GC=F"])
    assert set(out) == {"CL=F", "GC=F"}
    assert float(out["CL=F"]["Close"].iloc[-1]) == 2.5
    assert float(out["GC=F"]["Close"].iloc[-1]) == 25.0
    assert list(out["GC=F"].columns) == ["Close"]
    assert len(calls) == 1
    assert calls[0][0] == ["GC=F"]
    assert calls[0][1]["period"] == "max"


def test_macro_uses_shared_market_cache():
    text = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    assert "from market_cache import get_yf_histories" in text
    block = text[text.index("def market_snapshots_parallel():"):text.index("def ", text.index("def market_snapshots_parallel():") + 5)]
    assert "get_yf_histories(tickers)" in block
    assert "yf.download(" not in block


def test_macro_preserves_weekend_real_cached_provenance():
    source = (ROOT / "makro_szenario.py").read_text(encoding="utf-8-sig")
    start = source.index("def market_snapshots_parallel")
    active = source[start:]
    assert 'weekend_closed = today.weekday() >= 5 and ticker not in {"BTC-USD", "ETH-USD"}' in active
    assert 'close, provenance = macro_cached, "REAL_CACHED"' in active
    assert 'if weekend_closed and macro_cache_ok:' in active
    assert 'provenance = "REAL"' in active


def test_recent5d_refresh_is_written_back_to_shared_ticker_cache(tmp_path, monkeypatch):
    import market_cache as mc
    mc = importlib.reload(mc)
    monkeypatch.setattr(mc, "CACHE_FILE", tmp_path / "market_cache.json")
    monkeypatch.setattr(mc, "LOCK_FILE", tmp_path / "market_cache.json.lock")

    index_old = pd.date_range("2026-09-01", periods=2, freq="D")
    index_recent = pd.date_range("2026-09-01", periods=3, freq="D")
    old = pd.DataFrame({"Open": [1, 2], "High": [2, 3], "Low": [0, 1], "Close": [1.5, 2.5]}, index=index_old)
    recent = pd.DataFrame({"Open": [1, 2, 3], "High": [2, 3, 4], "Low": [0, 1, 2], "Close": [1.5, 2.5, 3.5]}, index=index_recent)
    mc.get_or_fetch_dataframe("yf:CL=F", lambda: old)
    mc.get_or_fetch_dataframe("yf:CL=F:recent5d", lambda: recent)

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, period):
            return old if period == "max" else recent

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))

    out = mc.get_yf_history("CL=F")
    assert float(out["Close"].iloc[-1]) == 3.5

    refreshed = mc._get_entry("yf:CL=F")
    assert refreshed is not None
    cached = pd.read_json(__import__("io").StringIO(refreshed["payload"]), orient="split")
    assert float(cached["Close"].iloc[-1]) == 3.5
