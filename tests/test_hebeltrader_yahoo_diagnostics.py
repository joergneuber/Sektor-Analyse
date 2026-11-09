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


def test_known_yahoo_aliases_are_corrected_only_for_matching_company(monkeypatch):
    module = _module(monkeypatch)
    cases = [
        ({"name": "Xinjiang Goldwind Science & Technology", "ticker": "02208.HK"}, "2208.HK"),
        ({"name": "Suzlon Energy Limited", "ticker": "SZL.NS"}, "SUZLON.NS"),
        ({"name": "ABB Ltd", "ticker": "ABB"}, "ABBN.SW"),
    ]
    for candidate, expected in cases:
        original = candidate["ticker"]
        result = module.normalisiere_yahoo_alias(candidate)
        assert result["ticker"] == expected
        assert result["ticker_original"] == original
        assert result["ticker_alias_applied"] == f"{original} -> {expected}"

    for unrelated_name in ("Another Business", "AbbVie Inc."):
        unrelated = {"name": unrelated_name, "ticker": "ABB"}
        module.normalisiere_yahoo_alias(unrelated)
        assert unrelated["ticker"] == "ABB"
        assert "nicht automatisch angewendet" in unrelated["yahoo_alias_review"]


def test_yahoo_rejections_are_retained_with_reason(monkeypatch):
    module = _module(monkeypatch)
    candidates = [
        {"name": "Valid Co", "ticker": "GOOD", "yahoo_valid": True},
        {
            "name": "Broken Co", "ticker": "BAD", "yahoo_valid": False,
            "yahoo_reason": "keine Yahoo-Kursdaten",
        },
    ]
    rejected = module.yahoo_ablehnungen(candidates)
    assert rejected == [candidates[1]]
    assert rejected[0]["yahoo_reason"] == "keine Yahoo-Kursdaten"
    assert rejected[0] is not candidates[1]


def test_yahoo_validation_queries_corrected_symbol(monkeypatch):
    import pandas as pd

    module = _module(monkeypatch)
    seen = []

    class FakeTicker:
        def history(self, **kwargs):
            return pd.DataFrame(
                {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]},
                index=pd.DatetimeIndex(["2026-11-06"]),
            )

    fake_yf = sys.modules["yfinance"]
    fake_yf.Ticker = lambda ticker: (seen.append(ticker) or FakeTicker())

    def checked(frame, ticker, **kwargs):
        return frame.copy()

    monkeypatch.setitem(
        sys.modules, "ohlcv_quality_gate", SimpleNamespace(quality_checked_frame=checked)
    )
    candidates = [{
        "name": "Suzlon Energy Limited", "ticker": "SZL.NS",
        "role": "primary", "confidence": 0.9,
    }]
    valid = module.yahoo_validiere(candidates)

    assert seen == ["SUZLON.NS"]
    assert [item["ticker"] for item in valid] == ["SUZLON.NS"]
    assert candidates[0]["ticker_original"] == "SZL.NS"
