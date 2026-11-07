from __future__ import annotations

import ast
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ohlcv_quality_gate as gate


def _dates():
    return pd.bdate_range("2025-10-01", periods=270)


def _frame(dates):
    close = np.linspace(100.0, 150.0, len(dates))
    return pd.DataFrame(
        {
            "Open": close - 0.2,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(len(dates), 100_000.0),
        },
        index=pd.DatetimeIndex(dates),
    )


def _install_real_gate(monkeypatch, tmp_path, dates):
    registry = tmp_path / "quality_registry.csv"
    registry.write_text(
        "ticker,date,classification,usage_decision,approved_by,evidence_ref,rule_version,allowed_reason_codes\n",
        encoding="utf-8",
    )
    gaps = tmp_path / "gap_registry.csv"
    gaps.write_text(
        "ticker,missing_date,timeframe,quality_class\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "DEFAULT_REGISTRY", registry)
    monkeypatch.setattr(gate, "DEFAULT_GAP_REGISTRY", gaps)
    gate._load_registry.cache_clear()
    gate._load_gap_registry.cache_clear()
    monkeypatch.setattr(
        gate,
        "_calendar_sessions",
        lambda ticker, start, end: [
            d.date().isoformat()
            for d in dates
            if start <= d.date().isoformat() <= end
        ],
    )
    monkeypatch.setattr(
        gate, "_last_completed_session_date",
        lambda ticker, as_of=None: dates[-1].date().isoformat(),
    )
    import historical_ohlcv

    monkeypatch.setattr(
        historical_ohlcv, "_calendar_name_for_ticker", lambda ticker: "XNYS"
    )


def _load_analysis_functions():
    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_return_over_expected_sessions", "get_perf", "get_perf_yf", "_hebeltrader_finalisieren"}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {node.name for node in nodes} == wanted
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {
        "pd": pd,
        "np": np,
        "datetime": datetime,
        "HEBELTRADER_KANDIDATEN": [],
        "HEBELTRADER_FUNNEL": {},
        "HEBELTRADER_SCHWELLE": 5,
    }
    exec(compile(module, "analyse.py", "exec"), namespace)
    return namespace


def _bars_for(ticker, frame):
    multi = pd.concat({ticker: frame}, names=["symbol", "timestamp"])
    return SimpleNamespace(df=multi)


def _namespace_for_perf(monkeypatch, ticker, alpaca_frame, yahoo_fetch):
    ns = _load_analysis_functions()

    class FakeRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    ns["StockBarsRequest"] = FakeRequest
    ns["TimeFrame"] = SimpleNamespace(Day="1Day")
    ns["alpaca_client"] = SimpleNamespace(
        get_stock_bars=lambda request: _bars_for(ticker, alpaca_frame)
    )
    ns["get_yf_history_from_yahoo"] = yahoo_fetch
    return ns


def _quality_checked_yahoo(frame, ticker):
    checked, quality = gate.apply_ohlcv_quality_gate(
        frame,
        ticker,
        registry_path=gate.DEFAULT_REGISTRY,
        gap_registry_path=gate.DEFAULT_GAP_REGISTRY,
        window_sessions=252,
        preserve_expected_sessions=True,
    )
    return checked


def test_get_perf_uses_alpaca_when_gate_and_rotation_windows_pass(monkeypatch, tmp_path):
    dates = _dates()
    _install_real_gate(monkeypatch, tmp_path, dates)
    raw = _frame(dates)
    yahoo_calls = []
    ns = _namespace_for_perf(
        monkeypatch, "XLK", raw,
        lambda ticker: yahoo_calls.append(ticker) or pd.DataFrame(),
    )

    result = ns["get_perf"]("XLK", "Technologie")

    assert result["Rotation-Score"] is not None
    assert result["5T"] is not None and result["12T"] is not None
    assert yahoo_calls == []


def test_get_perf_falls_back_to_direct_yahoo_for_missing_alpaca_rotation_session(
    monkeypatch, tmp_path
):
    dates = _dates()
    _install_real_gate(monkeypatch, tmp_path, dates)
    alpaca = _frame(dates).drop(index=dates[-4])
    yahoo = _frame(dates)
    yahoo_calls = []

    def yahoo_fetch(ticker):
        yahoo_calls.append(ticker)
        return _quality_checked_yahoo(yahoo, ticker)

    ns = _namespace_for_perf(monkeypatch, "XLK", alpaca, yahoo_fetch)
    result = ns["get_perf"]("XLK", "Technologie")

    expected_5t = round(
        (float(yahoo["Close"].iloc[-1]) / float(yahoo["Close"].iloc[-6]) - 1) * 100,
        2,
    )
    assert yahoo_calls == ["XLK"]
    assert result["5T"] == expected_5t
    assert result["Rotation-Score"] is not None


def test_get_perf_blocks_rotation_when_both_sources_have_incomplete_windows(
    monkeypatch, tmp_path
):
    dates = _dates()
    _install_real_gate(monkeypatch, tmp_path, dates)
    alpaca = _frame(dates).drop(index=[dates[-4], dates[-3]])
    yahoo = _frame(dates).drop(index=[dates[-4], dates[-3]])

    ns = _namespace_for_perf(
        monkeypatch, "XLK", alpaca,
        lambda ticker: _quality_checked_yahoo(yahoo, ticker),
    )
    result = ns["get_perf"]("XLK", "Technologie")

    assert result["5T"] is None
    assert result["12T"] is None
    assert result["Rotation-Score"] is None



def test_eu_performance_missing_data_does_not_create_zero_rotation_score():
    ns = _load_analysis_functions()
    ns["get_yf_history"] = lambda ticker: pd.DataFrame()
    result = ns["get_perf_yf"]("EXV1.DE", "Finanzen")
    assert result["5T"] is None
    assert result["12T"] is None
    assert result["Rotation-Score"] is None


def test_eu_performance_provider_exception_does_not_create_zero_rotation_score():
    ns = _load_analysis_functions()

    def fail(ticker):
        raise TimeoutError("provider timeout")

    ns["get_yf_history"] = fail
    result = ns["get_perf_yf"]("EXV1.DE", "Finanzen")
    assert result["5T"] is None
    assert result["12T"] is None
    assert result["Rotation-Score"] is None

def test_hebeltrader_sector_mapping_ignores_incomplete_rotation_rows():
    ns = _load_analysis_functions()
    ns["HEBELTRADER_KANDIDATEN"] = [{
        "Ticker": "GOOD", "Name": "Good", "Sektor": "GoodSector",
        "Markt": "US", "Waehrung": "USD", "Kurs": 100.0,
        "Eigene_5T": 10.0, "CRV1": 2.0, "CRV2": None,
        "Kriterien": {
            "A": (True, "ok"), "B": (True, "ok"),
            "C": (True, "ok"), "D": (True, "ok"),
        },
    }]
    performance = pd.DataFrame([
        {"Ticker": "BAD", "Sektor": "BadSector", "5T": None,
         "12T": None, "Rotation-Score": None},
        {"Ticker": "GOOD", "Sektor": "GoodSector", "5T": 8.0,
         "12T": 6.0, "Rotation-Score": 7.4},
    ])
    empty_eu = pd.DataFrame(columns=["Sektor", "5T", "12T", "Rotation-Score"])

    hits = ns["_hebeltrader_finalisieren"](performance, empty_eu)

    assert len(hits) == 1
    assert hits[0]["Sektor"] == "GoodSector"


def test_direct_yahoo_fallback_bypasses_project_cache_and_runs_quality_gate(
    monkeypatch,
):
    import market_cache

    calls = {}

    class FakeTicker:
        def __init__(self, symbol):
            calls["symbol"] = symbol

        def history(self, **kwargs):
            calls["history_kwargs"] = kwargs
            return _frame(_dates())

    monkeypatch.setitem(
        sys.modules, "yfinance", SimpleNamespace(Ticker=FakeTicker)
    )
    marker = pd.DataFrame({"Close": [123.0]})
    marker.attrs["ohlcv_quality"] = {"quality_status": "PASS"}
    gate_calls = {}

    def fake_quality(ticker, raw, *, window_sessions):
        gate_calls.update(ticker=ticker, window_sessions=window_sessions)
        return marker

    monkeypatch.setattr(market_cache, "_quality_checked_frame", fake_quality)

    result = market_cache.get_yf_history_from_yahoo("XLK")

    assert calls["symbol"] == "XLK"
    assert calls["history_kwargs"] == {
        "period": "1y", "interval": "1d", "auto_adjust": False, "actions": False
    }
    assert gate_calls == {"ticker": "XLK", "window_sessions": 252}
    assert result.attrs["ohlcv_quality"]["quality_status"] == "PASS"


def test_incomplete_rotation_rows_are_excluded_from_top_sector_selection():
    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "df_perf_valid"
                for target in node.targets)
    ]
    assert assignments
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dropna"
        and any(
            isinstance(value, ast.Name) and value.id == "df_perf"
            for value in ast.walk(node.func.value)
        )
        for node in ast.walk(assignments[0].value)
    )



def test_eu_performance_requires_six_closes_for_5t_and_thirteen_for_12t():
    ns = _load_analysis_functions()
    dates = _dates()
    complete = _frame(dates)
    # Gate-kompatible NaN-Markierung: fehlende erwartete Sessions bleiben
    # im Index erhalten und duerfen nicht durch kuerzere Fenster verschwinden.
    missing_5t = complete.copy()
    missing_5t.iloc[-6, missing_5t.columns.get_loc("Close")] = np.nan
    missing_12t = complete.copy()
    missing_12t.iloc[-13, missing_12t.columns.get_loc("Close")] = np.nan
    frames = {"5T": missing_5t, "12T": missing_12t}
    ns["get_yf_history"] = lambda ticker: frames[ticker]
    result_5t = ns["get_perf_yf"]("5T", "Test")
    result_12t = ns["get_perf_yf"]("12T", "Test")
    assert result_5t["5T"] is None
    assert result_5t["Rotation-Score"] is None
    assert result_12t["12T"] is None
    assert result_12t["Rotation-Score"] is None


def test_eu_performance_uses_same_n_plus_one_session_semantics_as_us():
    ns = _load_analysis_functions()
    dates = _dates()
    frame = _frame(dates)
    ns["get_yf_history"] = lambda ticker: frame
    result = ns["get_perf_yf"]("EXV1.DE", "Finanzen")
    expected_5t = round(
        (float(frame["Close"].iloc[-1]) / float(frame["Close"].iloc[-6]) - 1) * 100,
        2,
    )
    expected_12t = round(
        (float(frame["Close"].iloc[-1]) / float(frame["Close"].iloc[-13]) - 1) * 100,
        2,
    )
    assert result["5T"] == expected_5t
    assert result["12T"] == expected_12t
    assert result["Rotation-Score"] == round(expected_5t * 0.7 + expected_12t * 0.3, 3)


def test_alpaca_wrong_single_symbol_is_rejected_and_yahoo_fallback_used(
    monkeypatch, tmp_path
):
    dates = _dates()
    _install_real_gate(monkeypatch, tmp_path, dates)
    wrong = _frame(dates) * 4
    yahoo = _frame(dates)
    yahoo_calls = []
    ns = _namespace_for_perf(
        monkeypatch, "XLK", _frame(dates),
        lambda ticker: yahoo_calls.append(ticker) or _quality_checked_yahoo(yahoo, ticker),
    )
    wrong_multi = pd.concat({"WRONG": wrong}, names=["symbol", "timestamp"])
    ns["alpaca_client"] = SimpleNamespace(
        get_stock_bars=lambda request: SimpleNamespace(df=wrong_multi)
    )

    result = ns["get_perf"]("XLK", "Technologie")

    assert yahoo_calls == ["XLK"]
    assert result["Rotation-Score"] is not None
    expected_yahoo_5t = round(
        (float(yahoo["Close"].iloc[-1]) / float(yahoo["Close"].iloc[-6]) - 1) * 100,
        2,
    )
    assert result["5T"] == expected_yahoo_5t


def test_alpaca_exact_symbol_is_still_accepted_after_strict_symbol_check(
    monkeypatch, tmp_path
):
    dates = _dates()
    _install_real_gate(monkeypatch, tmp_path, dates)
    yahoo_calls = []
    ns = _namespace_for_perf(
        monkeypatch, "XLK", _frame(dates),
        lambda ticker: yahoo_calls.append(ticker) or pd.DataFrame(),
    )
    result = ns["get_perf"]("XLK", "Technologie")
    assert result["Rotation-Score"] is not None
    assert yahoo_calls == []



def test_eu_missing_expected_session_is_blocked_after_real_quality_gate(
    monkeypatch, tmp_path
):
    dates = _dates()
    _install_real_gate(monkeypatch, tmp_path, dates)
    raw = _frame(dates).drop(index=dates[-13])
    gated = _quality_checked_yahoo(raw, "EXV1.DE")
    assert gated.attrs["ohlcv_quality"]["quality_status"] == "PASS_WITH_EXCLUSIONS"
    assert pd.isna(gated["Close"].iloc[-13])

    ns = _load_analysis_functions()
    ns["get_yf_history"] = lambda ticker: gated
    result = ns["get_perf_yf"]("EXV1.DE", "Finanzen")

    assert result["12T"] is None
    assert result["Rotation-Score"] is None
