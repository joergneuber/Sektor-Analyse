
import importlib.util
from pathlib import Path
import pandas as pd
import numpy as np

# The test may live beside the module in a patch bundle or under tests/ in the repo.
HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "positionen_tracker.py").exists() else HERE.parent
# positionen_tracker imports data-provider clients at module load. These tests
# exercise local dataframe/price logic only, so provide minimal import stubs.
import sys
import types

def _stub_module(name, attrs=None):
    module = types.ModuleType(name)
    existing = sys.modules.get(name)
    if existing is None:
        sys.modules[name] = module
        existing = module
    for key, value in (attrs or {}).items():
        setattr(existing, key, value)
    return existing

_stub_module("yfinance")
for name in ("google", "google.oauth2", "google.oauth2.credentials",
             "google.auth", "google.auth.transport", "google.auth.transport.requests",
             "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
             "alpaca", "alpaca.data", "alpaca.data.historical",
             "alpaca.data.requests", "alpaca.data.timeframe"):
    _stub_module(name)
_stub_module("google.oauth2.credentials", {"Credentials": type("Credentials", (), {})})
_stub_module("google.auth.transport.requests", {"Request": type("Request", (), {})})
_stub_module("googleapiclient.discovery", {"build": lambda *a, **k: None})
_stub_module("googleapiclient.http", {
    "MediaIoBaseDownload": type("MediaIoBaseDownload", (), {}),
    "MediaIoBaseUpload": type("MediaIoBaseUpload", (), {}),
})
_stub_module("alpaca.data.historical", {
    "StockHistoricalDataClient": type("StockHistoricalDataClient", (), {}),
    "CryptoHistoricalDataClient": type("CryptoHistoricalDataClient", (), {}),
})
_stub_module("alpaca.data.requests", {
    "StockBarsRequest": type("StockBarsRequest", (), {}),
    "CryptoBarsRequest": type("CryptoBarsRequest", (), {}),
})
_stub_module("alpaca.data.timeframe", {"TimeFrame": type("TimeFrame", (), {})})
_stub_module("os_kurse", {"aktualisiere_optionsscheine": lambda *a, **k: None})

spec = importlib.util.spec_from_file_location("positionen_tracker", ROOT / "positionen_tracker.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _hist(n=20):
    dates = pd.date_range("2026-09-01", periods=n, freq="B")
    close = np.linspace(100, 110, n)
    low = close - 2
    high = close + 2
    # A confirmed swing low near the end, with two bars to its right.
    low[-4] = 104
    low[-3] = 102
    low[-2] = 105
    low[-1] = 106
    high[-4] = 108
    high[-3] = 109
    high[-2] = 111
    high[-1] = 112
    return pd.DataFrame({"Date": dates, "Open": close, "High": high, "Low": low, "Close": close, "Volume": 1000})


def _row(**extra):
    base = {c: "" for c in mod.SPALTEN}
    base.update({
        "Ticker": "TEST",
        "Markt": "US",
        "Ideen_Quelle": "Trendfolge",
        "Einstiegsdatum": "01.09.2026",
        "Einstieg": 100.0,
        "Stop": 95.0,
        "TP1": 105.0,
        "TP2": "",
        "Status": "Offen",
        "TP_Hinweis": "",
    })
    base.update(extra)
    return pd.Series(base)


def test_trendfolge_only():
    assert mod._ist_trendfolge_position(_row())
    assert not mod._ist_trendfolge_position(_row(Ideen_Quelle="Trendwende"))
    assert not mod._ist_trendfolge_position(_row(Ideen_Quelle="HEBELTRADER"))


def test_confirmed_swings_exclude_unconfirmed_right_edge():
    h = _hist()
    lows = mod._trendfolge_bestatigte_swing_lows(h, order=2)
    highs = mod._trendfolge_bestatigte_swing_highs(h, order=2)
    assert all(i <= len(h) - 3 for i in lows)
    assert all(i <= len(h) - 3 for i in highs)


def test_three_day_development_phase():
    h = _hist()
    row = _row(Einstiegsdatum=h.loc[0, "Date"].strftime("%d.%m.%Y"))
    days, done, end = mod._trendfolge_entwicklungsstatus(row, h)
    assert days == len(h) - 1
    assert done is True
    assert end == h.loc[3, "Date"]


def test_tp2_is_dynamic_and_not_entry_requirement(monkeypatch):
    h = _hist()
    # Force enough history for ATR and a clean dynamic target.
    df = pd.DataFrame([_row()])
    monkeypatch.setattr(mod, "_trendfolge_atr14", lambda hist: 1.0)
    stop, tp1, tp2 = mod._aktualisiere_trendfolge_management(
        df, 0, df.iloc[0], h, 100.0, 106.0, 95.0, 105.0, False, "22.09.2026"
    )
    assert "TF_Management" in df.columns
    assert "TP1 erreicht" in df.at[0, "TF_Management"]
    assert "TP1: 105.00" in df.at[0, "TF_Management"]
    assert tp2 is not None
    assert float(tp2) > 105.0
    assert pd.api.types.is_numeric_dtype(df["TP2"].dtype)
    assert float(stop) >= 100.0
    assert "Teilverkauf" not in df.at[0, "TF_Management"]
