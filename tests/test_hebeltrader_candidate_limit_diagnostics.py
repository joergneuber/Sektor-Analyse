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


def test_llm_candidate_universe_is_limited_to_25(monkeypatch):
    module = _module(monkeypatch)
    calls = []
    first_candidates = [
        {
            "name": f"Company {i:02d}",
            "ticker": f"T{i:02d}",
            "role": "primary" if i == 0 else "peer",
            "confidence": 1.0 - i / 100,
            "evidence": "Relevant to the teaser",
        }
        for i in range(30)
    ]

    def fake_llm_call(client, system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        if len(calls) == 1:
            return {"candidates": first_candidates}
        # Simulate the audit stage returning all candidates it was given.
        import json
        audited = json.loads(user_prompt.split("ZU AUDITIERENDE KANDIDATEN:", 1)[1])
        return {"candidates": audited}

    monkeypatch.setattr(module, "_groq_client", lambda: object())
    monkeypatch.setattr(module, "_llm_call", fake_llm_call)

    result = module.extrahiere_kandidaten({
        "issue_label": "HEBELTRADER 210/26",
        "issue_date": "10.11.2026",
        "headline": "Test",
        "teaser": "Test teaser",
    })

    assert module.MAX_LLM_KANDIDATEN == 25
    assert "Liefere 10 bis 25 Kandidaten" in calls[0][0]
    assert len(calls) == 2
    assert len(result) == 25
    assert result[0]["ticker"] == "T00"
    assert result[-1]["ticker"] == "T24"
    assert "T25" not in calls[1][1]


def test_yahoo_validation_returns_all_25_valid_candidates(monkeypatch):
    import pandas as pd

    module = _module(monkeypatch)
    seen = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            return pd.DataFrame(
                {
                    "Open": [10.0],
                    "High": [11.0],
                    "Low": [9.0],
                    "Close": [10.5],
                    "Volume": [100],
                },
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
        for i in range(25)
    ]

    valid = module.yahoo_validiere(candidates)

    assert module.MAX_VALIDIERTE_KANDIDATEN == 25
    assert len(seen) == 25
    assert len(valid) == 25
    assert all(item["yahoo_valid"] is True for item in candidates)
