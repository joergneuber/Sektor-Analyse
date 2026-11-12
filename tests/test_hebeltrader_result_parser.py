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


def test_missing_samsung_result_cannot_consume_following_royal_gold_result(monkeypatch):
    module = _module(monkeypatch)
    stdout = """SSNLF - Samsung Electronics Co., Ltd.   (Sektor: N/A)
==============================================================
SSNLF -> Daten von Alpaca leer.
TRENDFOLGE: kein Setup (Grund siehe DEBUG-Zeilen oben)
==============================================================
RGLD - Royal Gold, Inc.   (Sektor: Gold-Miner | Sektor-ETF: GDX)
==============================================================
DEBUG-TRENDWENDE-VERWORFEN: RGLD | Spannen-Position 72%
  KAUFKANDIDATEN-BEWERTUNG
  Ergebnis: KEIN KANDIDAT (Momentum 1/4)
    ✓ Sektor-RS positiv (+8.5 %-Pkt.)
    ⚠ Momentum zu schwach (1/4)
==============================================================
"""

    parsed = {
        row["ticker"]: row
        for row in module._parse_einzel_ergebnisse(stdout, ["SSNLF", "RGLD"])
    }

    assert parsed["SSNLF"]["status"] == "NICHT AUSGELESEN"
    assert parsed["SSNLF"]["momentum"] is None
    assert "Alpaca leer" in " ".join(parsed["SSNLF"]["technischer_zustand"])
    assert "Kein Ergebnis im eigenen Ticker-Abschnitt" in parsed["SSNLF"]["auslesegrund"]

    assert parsed["RGLD"]["status"] == "KEIN KANDIDAT"
    assert parsed["RGLD"]["momentum"] == "1/4"
    assert any("Sektor-RS positiv (+8.5" in line
               for line in parsed["RGLD"]["technischer_zustand"])
    assert not any("Samsung" in line or "SSNLF" in line
                   for line in parsed["RGLD"]["technischer_zustand"])


def test_valid_a_b_c_results_are_parsed_from_their_own_blocks(monkeypatch):
    module = _module(monkeypatch)
    stdout = """AAA - Alpha Inc. (Sektor: Technologie)
  Ergebnis: KAUFKANDIDAT A (Momentum 4/4)
    ✓ bestätigtes Setup
==============================================================
BBB - Beta Inc. (Sektor: Technologie)
  Ergebnis: KAUFKANDIDAT B (Momentum 3/4)
    ✓ Vorbereitung
==============================================================
CCC - Gamma Inc. (Sektor: Technologie)
  Ergebnis: KAUFKANDIDAT C (Momentum 2/4)
    ⚠ noch kein Einstieg
==============================================================
"""

    parsed = module._parse_einzel_ergebnisse(stdout, ["AAA", "BBB", "CCC"])

    assert [(x["ticker"], x["status"], x["momentum"]) for x in parsed] == [
        ("AAA", "KAUFKANDIDAT A", "4/4"),
        ("BBB", "KAUFKANDIDAT B", "3/4"),
        ("CCC", "KAUFKANDIDAT C", "2/4"),
    ]


def test_missing_or_duplicate_ticker_blocks_fail_closed(monkeypatch):
    module = _module(monkeypatch)
    missing = "AAA - Alpha Inc. (Sektor: Technologie)\nErgebnis: KEIN KANDIDAT (Momentum 0/4)\n"
    parsed_missing = module._parse_einzel_ergebnisse(missing, ["ZZZ"])[0]
    assert parsed_missing["status"] == "NICHT AUSGELESEN"
    assert parsed_missing["momentum"] is None

    duplicate = """AAA - Alpha Inc. (Sektor: Technologie)
Ergebnis: KEIN KANDIDAT (Momentum 0/4)
BBB - Beta Inc. (Sektor: Technologie)
Ergebnis: KEIN KANDIDAT (Momentum 0/4)
AAA - Alpha Inc. (Sektor: Technologie)
Ergebnis: KAUFKANDIDAT A (Momentum 4/4)
"""
    parsed_duplicate = module._parse_einzel_ergebnisse(duplicate, ["AAA"])[0]
    assert parsed_duplicate["status"] == "NICHT AUSGELESEN"
    assert "2 eigene Ticker-Abschnitte" in parsed_duplicate["auslesegrund"]


def test_unreadable_hebeltrader_status_is_not_promoted_into_trade_story_universe(tmp_path):
    import json
    from pathlib import Path

    from trade_story_universum import build_trade_story_universe

    state = Path(tmp_path) / "hebeltrader_einzel_check.json"
    state.write_text(json.dumps({
        "candidates": [{
            "ticker": "SSNLF",
            "name": "Samsung Electronics Co., Ltd.",
            "einzel_check": {
                "ticker": "SSNLF",
                "status": "NICHT AUSGELESEN",
                "momentum": None,
            },
        }],
    }), encoding="utf-8")

    universe = build_trade_story_universe({"HEBELTRADER-Einzelcheck": str(state)})

    assert all(item.get("ticker") != "SSNLF" for item in universe["candidates"])
