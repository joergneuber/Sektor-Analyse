from __future__ import annotations

import json
from pathlib import Path
import tempfile

from trade_story_universum import build_trade_story_universe


def _build(paths, obs=None):
    return {x["ticker"]: x for x in build_trade_story_universe(paths, obs)["candidates"]}


def test_current_stdout_and_nested_payload():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "raw.csv"
        raw.write_text("Ticker;Name;Status2\n", encoding="utf-8")
        h = td / "hebeltrader_einzel_check.json"
        h.write_text(json.dumps({
            "result": {"candidates": [
                {"ticker": "PFE", "name": "Pfizer", "einzel_check": {"status": "KAUFKANDIDAT A"}},
                {"ticker": "TMO", "name": "Thermo Fisher", "einzel_check": {"status": "KAUFKANDIDAT B"}},
                {"ticker": "BAD", "name": "Bad", "einzel_check": {"status": "KAUFKANDIDAT C"}},
            ]},
            "einzel_check_stdout": "SRT3.DE KAUFKANDIDAT A letzter Check 2026-09-16\nIBM KEIN KANDIDAT letzter Check 2026-09-16\n",
        }), encoding="utf-8")
        b = _build({"Trade_Story_Setup_Rohuniversum(...).csv": str(raw), "HEBELTRADER-Einzelcheck": str(h)})
        assert b["PFE"]["trade_story_status"] == "VORBEREITET"
        assert b["TMO"]["trade_story_status"] == "VORBEREITET"
        assert b["SRT3.DE"]["trade_story_status"] == "VORBEREITET"
        assert "BAD" not in b and "IBM" not in b


def test_time_shifted_observation_is_authoritative_for_next_main_run():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "raw.csv"
        raw.write_text("Ticker;Name;Status2\n", encoding="utf-8")
        h = td / "hebeltrader_einzel_check.json"
        h.write_text(json.dumps({"schema_version": 3, "candidates": []}), encoding="utf-8")
        obs = td / "einzel_check_beobachtung.json"
        obs.write_text(json.dumps({
            "EOG": {"name": "EOG", "status": "KAUFKANDIDAT B", "letzter_check": "2026-09-16", "last_candidate_date": "2026-09-16", "quelle": "HEBELTRADER 170/26"},
            "OLD": {"name": "Old", "status": "KAUFKANDIDAT A", "letzter_check": "2026-09-15", "last_candidate_date": "2026-09-15", "quelle": "HEBELTRADER 169/26"},
            "MANUAL": {"name": "Manual", "status": "KAUFKANDIDAT A", "letzter_check": "2026-09-16", "last_candidate_date": None, "quelle": "-"},
        }), encoding="utf-8")
        b = _build({"Trade_Story_Setup_Rohuniversum(...).csv": str(raw), "HEBELTRADER-Einzelcheck": str(h)}, str(obs))
        assert b["EOG"]["trade_story_status"] == "VORBEREITET"
        assert b["OLD"]["trade_story_status"] == "VORBEREITET"
        assert "MANUAL" not in b


def test_current_observation_negative_overrides_structured_snapshot_and_a_message_supplements():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "raw.csv"
        raw.write_text("Ticker;Name;Status2\n", encoding="utf-8")
        h = td / "hebeltrader_einzel_check.json"
        h.write_text(json.dumps({"einzel_check_stdout": "IBM KEIN KANDIDAT letzter Check 2026-09-16\n"}), encoding="utf-8")
        a = td / "Einzel_Check_A_Meldungen(2026-09-16).txt"
        a.write_text("Name: Palantir | Ticker: PLTR | KAUFKANDIDAT A\n", encoding="utf-8")
        obs = td / "obs.json"
        obs.write_text(json.dumps({
            "IBM": {"name": "IBM", "status": "KEIN KANDIDAT", "letzter_check": "2026-09-16", "last_candidate_date": "2026-09-16", "quelle": "HEBELTRADER 170/26"},
        }), encoding="utf-8")
        b = _build({"Trade_Story_Setup_Rohuniversum(...).csv": str(raw), "HEBELTRADER-Einzelcheck": str(h), "Einzel_Check_A_Meldungen(...).txt": str(a)}, str(obs))
        assert "IBM" not in b
        assert b["PLTR"]["trade_story_status"] == "VORBEREITET"


if __name__ == "__main__":
    test_current_stdout_and_nested_payload()
    test_time_shifted_observation_is_authoritative_for_next_main_run()
    test_current_observation_negative_overrides_structured_snapshot_and_a_message_supplements()
    print("HEBELTRADER_CURRENT_STATUS_TESTS: 3 PASS")


def test_a_requires_same_day_technical_confirmation_and_name_fallback():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "raw.csv"
        raw.write_text("Ticker;Name;Status2\n", encoding="utf-8")
        obs = td / "obs.json"
        obs.write_text(json.dumps({
            "DHL.DE": {"status": "KAUFKANDIDAT A", "letzter_check": "2026-09-17", "last_candidate_date": "2026-09-17", "quelle": "-"},
            "AMD": {"status": "KAUFKANDIDAT A", "letzter_check": "2026-09-17", "last_candidate_date": "2026-09-17", "quelle": "-"},
        }), encoding="utf-8")
        hist = td / "einzel_check_historie.jsonl"
        hist.write_text(
            json.dumps({
                "Datum": "2026-09-17", "Ticker": "DHL.DE", "Name": "DHL AG",
                "Status": "KAUFKANDIDAT A",
                "Trendfolge": {"Status2": "VALIDE"},
            }) + "\n" +
            json.dumps({
                "Datum": "2026-09-17", "Ticker": "AMD", "Name": "Advanced Micro Devices, Inc.",
                "Status": "KAUFKANDIDAT A",
                "Trendfolge": {"Status2": "ACHTUNG"},
            }) + "\n",
            encoding="utf-8",
        )
        b = _build({
            "Trade_Story_Setup_Rohuniversum(...).csv": str(raw),
            "Einzel-Check-Technikhistorie": str(hist),
        }, str(obs))
        assert b["DHL.DE"]["name"] == "DHL AG"
        assert b["DHL.DE"]["trade_story_status"] == "VALIDE SETUP"
        assert b["AMD"]["name"] == "Advanced Micro Devices, Inc."
        assert b["AMD"]["trade_story_status"] == "VORBEREITET"


