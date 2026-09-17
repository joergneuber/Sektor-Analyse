from __future__ import annotations
import json
import csv
import tempfile
from pathlib import Path

from trade_story_universum import build_trade_story_universe


def write_csv(path, header, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "Trade_Story_Setup_Rohuniversum(2026-09-15).csv"
        write_csv(raw, ["Ticker","Name","Status2","Sektor","Trend","CRV1","CRV2"], [
            ["WMB","The Williams Companies, Inc.","VALIDE","Energy","FAIL","2.0","2.1"],
            ["EOG","EOG Resources, Inc.","ACHTUNG","Energy","OK","1.2","1.4"],
        ])
        final = td / "Setups(2026-09-15).csv"
        write_csv(final, ["Ticker","Name","Status2","Status_Grund"], [
            ["EOG","EOG Resources, Inc.","ACHTUNG","Earnings-Gap-Risiko"],
        ])
        obs = td / "einzel_check_beobachtung.json"
        obs.write_text(json.dumps({
            "TSM":{"name":"Taiwan Semiconductor","status":"KAUFKANDIDAT A","quelle":"HEBELTRADER 164/26"},
            "ZS":{"name":"Zscaler","status":"KAUFKANDIDAT B","quelle":"HEBELTRADER 164/26"},
            "BAD":{"name":"Bad","status":"KAUFKANDIDAT C","quelle":"HEBELTRADER 164/26"},
            "NONE":{"name":"None","status":"KEIN KANDIDAT","quelle":"HEBELTRADER 164/26"},
        }), encoding="utf-8")
        hebel = td / "hebeltrader_einzel_check.json"
        hebel.write_text(json.dumps({
            "schema_version": 3,
            "candidates": [
                {"ticker":"TSM","name":"Taiwan Semiconductor","einzel_check":{"status":"KAUFKANDIDAT A"}},
                {"ticker":"ZS","name":"Zscaler","einzel_check":{"status":"KAUFKANDIDAT B"}},
                {"ticker":"BAD","name":"Bad","einzel_check":{"status":"KAUFKANDIDAT C"}},
            ]
        }), encoding="utf-8")
        trend = td / "Trendwende_Setups(2026-09-15).csv"
        write_csv(trend, ["Ticker","Name"], [["ARGX","argenx SE"]])
        short = td / "Short_Setups(2026-09-15).csv"
        write_csv(short, ["Ticker","Name","Status2"], [])
        metals = td / "Edelmetalle_Setups(2026-09-15).csv"
        write_csv(metals, ["Ticker","Name","Status2"], [["GOLD","Gold","ACHTUNG"]])
        btc = td / "Trade_Story_Bitcoin(2026-09-15).json"
        btc.write_text(json.dumps({
            "date":"2026-09-15", "asset":"Bitcoin", "ticker":"BTC-USD",
            "pi_cycle_bottom":{"signal_type":"BOTTOM_LONG","trade_action":"LONG"},
            "sma50w":{"signal_type":"PREALERT","trade_action":"LONG"}
        }), encoding="utf-8")
        portfolio = td / "Offene Positionen+Check.csv"
        write_csv(portfolio, ["Ticker","Status"], [["WMB","Offen"]])

        paths = {
            "Trade_Story_Setup_Rohuniversum(...).csv":str(raw),
            "Setups(...).csv":str(final),
            "Trendwende_Setups(...).csv":str(trend),
            "Short_Setups(...).csv":str(short),
            "Edelmetalle_Setups(...).csv":str(metals),
            "Trade_Story_Bitcoin(...).json":str(btc),
            "Offene Positionen+Check.csv":str(portfolio),
            "HEBELTRADER-Einzelcheck":str(hebel),
        }
        uni=build_trade_story_universe(paths,str(obs))
        by={x["ticker"]:x for x in uni["candidates"]}
        assert by["WMB"]["trade_story_status"]=="VALIDE SETUP"
        assert by["WMB"]["portfolio_status"]=="OFFENE POSITION"
        assert by["EOG"]["trade_story_status"]=="VORBEREITET"
        assert by["TSM"]["trade_story_status"]=="VALIDE SETUP"
        assert by["ZS"]["trade_story_status"]=="VORBEREITET"
        assert "BAD" not in by and "NONE" not in by
        assert by["ARGX"]["trade_story_status"]=="VALIDE SETUP"
        assert by["GOLD"]["trade_story_status"]=="VORBEREITET"
        assert by["BTC-USD"]["trade_story_status"]=="VALIDE SETUP"

        # Same company name with two tickers must remain two candidates.
        raw2 = td / "Trade_Story_Setup_Rohuniversum(duplicate).csv"
        write_csv(raw2, ["Ticker","Name","Status2","Sektor","Trend","CRV1","CRV2"], [
            ["GOOGL","Alphabet Inc.","VALIDE","Communication Services","OK","2.0","2.0"],
            ["GOOG","Alphabet Inc.","VALIDE","Communication Services","OK","2.0","2.0"],
        ])
        paths["Trade_Story_Setup_Rohuniversum(...).csv"] = str(raw2)
        uni=build_trade_story_universe(paths,str(obs))
        by={x["ticker"]:x for x in uni["candidates"]}
        assert "GOOGL" in by and "GOOG" in by

        # Manual/other Einzel-Check entries are not Hebeltrader candidates.
        obs_data = json.loads(obs.read_text(encoding="utf-8"))
        obs_data["MANUAL"] = {"name":"Manual Entry","status":"KAUFKANDIDAT A","quelle":"-"}
        obs.write_text(json.dumps(obs_data), encoding="utf-8")
        uni=build_trade_story_universe(paths,str(obs))
        by={x["ticker"]:x for x in uni["candidates"]}
        assert "MANUAL" not in by

        paths["Trade_Story_Setup_Rohuniversum(...).csv"] = str(raw)

        # Long/Short is a genuine directional conflict and must not be silently resolved.
        short2 = td / "Short_Setups_conflict.csv"
        write_csv(short2, ["Ticker","Name","Status2"], [["WMB","The Williams Companies, Inc.","VALIDE"]])
        paths["Short_Setups(...).csv"] = str(short2)
        uni=build_trade_story_universe(paths,str(obs))
        by={x["ticker"]:x for x in uni["candidates"]}
        assert by["WMB"]["trade_story_status"]=="STATUSKONFLIKT"
        assert by["WMB"]["direction"]=="CONFLICT"

        test_hebeltrader_nested_schema_variant()
        test_hebeltrader_observation_completeness_fallback()
        print("TRADE_STORY_UNIVERSUM_TESTS: 4 PASS")



def test_hebeltrader_nested_schema_variant():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "Trade_Story_Setup_Rohuniversum(2026-09-16).csv"
        write_csv(raw, ["Ticker","Name","Status2"], [])
        hebel = td / "hebeltrader_einzel_check.json"
        hebel.write_text(json.dumps({"issue_label":"HEBELTRADER 169/26","result":{"candidates":[
            {"ticker":"PFE","name":"Pfizer Inc.","einzel_check":{"status":"KAUFKANDIDAT A"}},
            {"ticker":"TMO","name":"Thermo Fisher Scientific Inc.","einzel_check":{"status":"KAUFKANDIDAT B"}},
            {"ticker":"BAD","name":"Bad","einzel_check":{"status":"KAUFKANDIDAT C"}}
        ]}}), encoding="utf-8")
        uni = build_trade_story_universe({
            "Trade_Story_Setup_Rohuniversum(...).csv":str(raw),
            "HEBELTRADER-Einzelcheck":str(hebel)
        })
        by={x["ticker"]:x for x in uni["candidates"]}
        assert by["PFE"]["trade_story_status"] == "VORBEREITET"
        assert by["TMO"]["trade_story_status"] == "VORBEREITET"
        assert "BAD" not in by


def test_hebeltrader_observation_completeness_fallback():
    """Current A/B observation entries must survive a reduced/missing JSON payload."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        raw = td / "Trade_Story_Setup_Rohuniversum(2026-09-16).csv"
        write_csv(raw, ["Ticker", "Name", "Status2"], [])
        obs = td / "einzel_check_beobachtung.json"
        from datetime import date
        today = date.today().isoformat()
        obs.write_text(json.dumps({
            "PLTR": {"name": "Palantir Technologies", "status": "KAUFKANDIDAT A", "letzter_check": today, "last_candidate_date": today, "quelle": "HEBELTRADER 170/26"},
            "RVTY": {"name": "Revvity, Inc.", "status": "KAUFKANDIDAT B", "letzter_check": today, "last_candidate_date": today, "quelle": "HEBELTRADER 170/26"},
            "OLD": {"name": "Old Candidate", "status": "KAUFKANDIDAT A", "letzter_check": "2026-09-15", "quelle": "HEBELTRADER 169/26"},
            "MANUAL": {"name": "Manual", "status": "KAUFKANDIDAT A", "letzter_check": today, "last_candidate_date": None, "quelle": "-"},
            "C": {"name": "Excluded", "status": "KAUFKANDIDAT C", "letzter_check": today, "last_candidate_date": today, "quelle": "HEBELTRADER 170/26"},
        }), encoding="utf-8")
        hebel = td / "hebeltrader_einzel_check.json"
        hebel.write_text(json.dumps({"schema_version": 3, "candidates": [
            {"ticker": "PLTR", "name": "Palantir Technologies", "einzel_check": {"status": "KAUFKANDIDAT A"}}
        ]}), encoding="utf-8")
        uni = build_trade_story_universe({
            "Trade_Story_Setup_Rohuniversum(...).csv": str(raw),
            "HEBELTRADER-Einzelcheck": str(hebel),
        }, str(obs))
        by = {x["ticker"]: x for x in uni["candidates"]}
        assert by["PLTR"]["trade_story_status"] == "VORBEREITET"
        assert by["RVTY"]["trade_story_status"] == "VORBEREITET"
        assert "OLD" not in by
        assert "MANUAL" not in by
        assert "C" not in by

if __name__ == "__main__":
    main()
