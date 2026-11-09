
import csv
import json
import tempfile
from pathlib import Path
import importlib.util

MODULE_PATH = Path(__file__).resolve().parents[1] / "trade_story_universum.py"

spec = importlib.util.spec_from_file_location("trade_story_universum", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def run_matrix():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        stock_rows = [
            {"Ticker": "A1", "Name": "Aktie 1", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "B1", "Name": "Aktie 2", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "C1", "Name": "Aktie 3", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "V1", "Name": "Aktie 4", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "N1", "Name": "Aktie 5", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "H1", "Name": "Aktie 6", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "R1", "Name": "Rohstatus Valide", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "R2", "Name": "Rohstatus Achtung", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "R3", "Name": "Rohstatus Valide aber final Achtung", "Assetdaten_Status": "AUSGELESEN"},
            {"Ticker": "A2", "Name": "Aktie 7", "Assetdaten_Status": "NICHT AUSGELESEN"},
            {"Ticker": "N2", "Name": "Aktie 8", "Assetdaten_Status": "NICHT AUSGELESEN"},
        ]
        stock_csv = root / "Trade_Story_Aktienuniversum(2099-01-01).csv"
        write_csv(stock_csv, stock_rows, ["Ticker", "Name", "Assetdaten_Status"])

        raw_csv = root / "Trade_Story_Setup_Rohuniversum(2099-01-01).csv"
        write_csv(
            raw_csv,
            [
                {"Ticker": "R1", "Name": "Rohstatus Valide", "Status2": "VALIDE", "Trade_Story_Status": "VORFILTER_VALIDE"},
                {"Ticker": "R2", "Name": "Rohstatus Achtung", "Status2": "ACHTUNG", "Trade_Story_Status": "VORFILTER_ACHTUNG"},
                {"Ticker": "R3", "Name": "Rohstatus Valide aber final Achtung", "Status2": "VALIDE", "Trade_Story_Status": "VORFILTER_VALIDE"},
            ],
            ["Ticker", "Name", "Status2", "Trade_Story_Status"],
        )

        setup_csv = root / "Setups(2099-01-01).csv"
        write_csv(
            setup_csv,
            [
                {"Ticker": "V1", "Name": "Aktie 4", "Status2": "VALIDE"},
                {"Ticker": "R3", "Name": "Rohstatus Valide aber final Achtung", "Status2": "ACHTUNG"},
            ],
            ["Ticker", "Name", "Status2"],
        )

        hebel_json = root / "hebeltrader_einzel_check.json"
        hebel_json.write_text(json.dumps({
            "candidates": [
                {"ticker": "A1", "name": "Aktie 1", "status": "KAUFKANDIDAT A"},
                {"ticker": "B1", "name": "Aktie 2", "status": "KAUFKANDIDAT B"},
                {"ticker": "C1", "name": "Aktie 3", "status": "KAUFKANDIDAT C"},
                {"ticker": "A2", "name": "Aktie 7", "status": "KAUFKANDIDAT A"},
                {"ticker": "X9", "name": "Nicht-Projekt-Aktie", "status": "KAUFKANDIDAT A"},
            ]
        }), encoding="utf-8")

        paths = {
            "Trade_Story_Aktienuniversum(...).csv": str(stock_csv),
            "Trade_Story_Setup_Rohuniversum(...).csv": str(raw_csv),
            "Setups(...).csv": str(setup_csv),
            "HEBELTRADER-Einzelcheck": str(hebel_json),
        }

        universe = mod.build_trade_story_universe(paths)
        assert universe["schema_version"] == 2
        assert universe["principles"]["valid"] == "FINAL_VALIDE"
        assert universe["principles"]["prefilter_valid"] == "VORFILTER_VALIDE"
        assert universe["principles"]["prefilter_attention"] == "VORFILTER_ACHTUNG"
        assert universe["principles"]["no_setup"] == "NICHT_FINAL_QUALIFIZIERT"
        members = {
            str(x.get("ticker")).upper()
            for x in universe["candidates"]
            if x.get("ticker")
        }

        expected = {"A1", "B1", "C1", "V1", "N1", "H1", "R1", "R2", "R3"}
        excluded = {"A2", "N2", "X9"}

        assert expected <= members, (expected, members)
        assert not (excluded & members), (excluded, members)

        # Explicitly verify that technical status does not control membership.
        status_by_ticker = {
            x["ticker"]: x["trade_story_status"]
            for x in universe["candidates"]
            if x.get("ticker")
        }
        assert status_by_ticker["A1"] == "VORBEREITET"
        assert status_by_ticker["B1"] == "VORBEREITET"
        assert status_by_ticker["C1"] == "VORBEREITET"
        assert status_by_ticker["V1"] == "FINAL_VALIDE"
        assert status_by_ticker["N1"] == "NICHT_FINAL_QUALIFIZIERT"
        assert status_by_ticker["H1"] == "NICHT_FINAL_QUALIFIZIERT"
        assert status_by_ticker["R1"] == "NICHT_FINAL_QUALIFIZIERT"
        assert status_by_ticker["R2"] == "NICHT_FINAL_QUALIFIZIERT"
        assert status_by_ticker["R3"] == "VORBEREITET"

        # Rohstatus remains separately visible and cannot promote a candidate
        # to FINAL_VALIDE without a matching final Setups.csv row.
        by_ticker = {x["ticker"]: x for x in universe["candidates"] if x.get("ticker")}
        assert by_ticker["R1"]["normal_setup_status"] == "VORFILTER_VALIDE"
        assert by_ticker["R2"]["normal_setup_status"] == "VORFILTER_ACHTUNG"
        assert by_ticker["R3"]["normal_setup_status"] == "VORFILTER_VALIDE"
        assert by_ticker["R3"]["final_setup_status"] == "ACHTUNG"
        assert "final_setup_status" not in by_ticker["R1"]
        assert by_ticker["V1"]["final_setup_status"] == "FINAL_VALIDE"
        assert by_ticker["V1"]["normal_setup_status"] == "NICHT_FINAL_QUALIFIZIERT"

        # Name + ticker must survive the primary universe handoff.
        for ticker, name in {
            "A1": "Aktie 1", "B1": "Aktie 2", "C1": "Aktie 3",
            "V1": "Aktie 4", "N1": "Aktie 5", "H1": "Aktie 6",
            "R1": "Rohstatus Valide", "R2": "Rohstatus Achtung",
            "R3": "Rohstatus Valide aber final Achtung",
        }.items():
            row = next(x for x in universe["candidates"] if x.get("ticker") == ticker)
            assert row.get("name") == name

        return True


if __name__ == "__main__":
    run_matrix()
    print("8-FÄLLE-MATRIX: PASS")
