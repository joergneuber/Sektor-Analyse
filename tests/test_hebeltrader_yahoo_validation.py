from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pandas as pd


def _load_module(monkeypatch):
    """Load the target without requiring yfinance to be installed in the test env."""
    fake_yf = SimpleNamespace(Ticker=None)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=None))
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))
    sys.modules.pop("hebeltrader_einzel_check", None)
    return importlib.import_module("hebeltrader_einzel_check"), fake_yf


def test_yahoo_validation_requests_calendar_safe_history_and_preserves_gate(monkeypatch):
    module, fake_yf = _load_module(monkeypatch)
    calls = {}

    class FakeTicker:
        def history(self, **kwargs):
            calls["history"] = kwargs
            return pd.DataFrame(
                {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
                index=pd.DatetimeIndex(["2026-11-06"]),
            )

    fake_yf.Ticker = lambda ticker: FakeTicker()
    gate = SimpleNamespace(
        quality_checked_frame=lambda frame, ticker, **kwargs: (
            calls.setdefault("gate", {"ticker": ticker, **kwargs})
            or frame
        )
    )
    # Use a distinct stub that returns the input frame after recording arguments.
    def checked_frame(frame, ticker, **kwargs):
        calls["gate"] = {"ticker": ticker, **kwargs}
        return frame.copy()

    gate.quality_checked_frame = checked_frame
    monkeypatch.setitem(sys.modules, "ohlcv_quality_gate", gate)

    candidates = [{"ticker": "TEST", "role": "primary", "confidence": 0.9}]
    result = module.yahoo_validiere(candidates)

    assert calls["history"] == {"period": "1mo", "auto_adjust": False}
    assert calls["gate"] == {
        "ticker": "TEST",
        "window_sessions": 5,
        "require_structure": True,
        "full_history": True,
    }
    assert [x["ticker"] for x in result] == ["TEST"]
    assert candidates[0]["yahoo_valid"] is True


def test_yahoo_validation_reports_quality_gate_reasons_without_bypassing_gate(monkeypatch):
    module, fake_yf = _load_module(monkeypatch)

    class FakeTicker:
        def history(self, **kwargs):
            return pd.DataFrame(
                {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
                index=pd.DatetimeIndex(["2026-11-06"]),
            )

    fake_yf.Ticker = lambda ticker: FakeTicker()

    def rejected(frame, ticker, **kwargs):
        result = pd.DataFrame()
        result.attrs["ohlcv_quality"] = {
            "quality_status": "INCOMPLETE",
            "reason_codes": ["INSUFFICIENT_EXPECTED_SESSIONS"],
        }
        return result

    monkeypatch.setitem(
        sys.modules,
        "ohlcv_quality_gate",
        SimpleNamespace(quality_checked_frame=rejected),
    )
    candidates = [{"ticker": "TEST", "role": "primary", "confidence": 0.9}]
    result = module.yahoo_validiere(candidates)

    assert result == []
    assert candidates[0]["yahoo_valid"] is False
    assert candidates[0]["yahoo_reason"] == (
        "OHLCV-Quality-Gate INCOMPLETE (INSUFFICIENT_EXPECTED_SESSIONS)"
    )
