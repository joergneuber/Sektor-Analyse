from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


def _module(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=None))
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=None))
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))
    sys.modules.pop("hebeltrader_einzel_check", None)
    return importlib.import_module("hebeltrader_einzel_check")


def test_yahoo_validation_returns_all_valid_candidates_before_limit(monkeypatch):
    import pandas as pd

    module = _module(monkeypatch)
    seen = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            return pd.DataFrame(
                {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
                index=pd.DatetimeIndex(["2026-11-06"]),
            )

    def fake_ticker(ticker):
        seen.append(ticker)
        return FakeTicker(ticker)

    monkeypatch.setattr(sys.modules["yfinance"], "Ticker", fake_ticker)
    monkeypatch.setitem(
        sys.modules,
        "ohlcv_quality_gate",
        SimpleNamespace(quality_checked_frame=lambda frame, ticker, **kwargs: frame.copy()),
    )
    candidates = [
        {
            "name": f"Company {i:02d}",
            "ticker": f"T{i:02d}",
            "role": "primary" if i == 0 else "peer",
            "confidence": 1.0 - i / 100,
        }
        for i in range(16)
    ]

    valid = module.yahoo_validiere(candidates)

    assert len(seen) == 16
    assert len(valid) == 16
    assert all(item["yahoo_valid"] is True for item in candidates)


def test_validated_candidates_are_split_without_loss(monkeypatch):
    module = _module(monkeypatch)
    candidates = [
        {"name": f"Company {i:02d}", "ticker": f"T{i:02d}", "yahoo_valid": True}
        for i in range(16)
    ]

    selected, deferred = module.teile_validierte_kandidaten(candidates)

    assert len(selected) == 15
    assert len(deferred) == 1
    assert [item["ticker"] for item in selected + deferred] == [
        item["ticker"] for item in candidates
    ]
    assert deferred[0]["ticker"] == "T15"
    assert deferred[0]["einzel_check_status"] == "ZURUECKGESTELLT_LIMIT"
    assert "MAX_VALIDIERTE_KANDIDATEN=15" in deferred[0]["deferred_reason"]
    assert "einzel_check_status" not in candidates[-1]
