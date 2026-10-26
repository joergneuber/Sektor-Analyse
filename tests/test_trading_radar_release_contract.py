"""Release contracts: eight-section report, thesis lifecycle, portfolio immutability, benchmark golden values."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import datetime as datetime_module

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TradingRadarReleaseContractTests(unittest.TestCase):
    def test_eight_section_golden_report_passes_gate_and_order_is_required(self):
        gate = load_module("release_report_gate", "trading_radar_report_gate.py")
        fixture_module = load_module("report_gate_fixture", "tests/test_trading_radar_report_gate.py")
        report = fixture_module.report_fixture()
        self.assertEqual(gate.validate_report(report), [])
        bad_order = report.replace("2. Thesen-Update", "2. Thesen-Update", 1)
        # Move section 8 before section 7; a complete but out-of-order report must fail.
        section7 = bad_order.index("7. MSCI-Vergleich")
        section8 = bad_order.index("8. Daten-/Prüfanhang")
        chunk7 = bad_order[section7:section8]
        end8 = bad_order.find("\n\n10.5", section8)
        chunk8 = bad_order[section8:end8]
        reordered = bad_order[:section7] + chunk8 + "\n\n" + chunk7 + bad_order[end8:]
        self.assertTrue(gate.validate_report(reordered), "out-of-order sections must fail the report gate")

    def test_thesis_register_lifecycle_is_idempotent_versions_changes_and_archives_removed_theses(self):
        thesen = load_module("release_thesen", "trading_radar_thesen.py")
        def thesis(text="Rates stay restrictive", status="NEU"):
            return {
                "thesis_id": "T-RATES-01", "thesis": text, "horizon": "1-3 months",
                "evidence": ["policy statement"], "counter_evidence": ["inflation falling"],
                "confidence": "medium, conflicting indicators", "confirm_triggers": ["yields rise"],
                "invalidate_triggers": ["cuts accelerate"], "event_dependencies": ["FOMC"],
                "asset_classes": ["bonds"], "candidates": ["TLT"], "status": status,
            }
        def report(theses):
            return "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(
                {"data_cutoff": "2026-10-26T15:00:00+00:00", "theses": theses}
            ) + "\n```"
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "theses.json")
            first = thesen.update_register(report([thesis()]), path)
            first_doc = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertTrue(first["updated"])
            self.assertEqual(first_doc["theses"][0]["version"], 1)
            second = thesen.update_register(report([thesis()]), path)
            second_doc = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(second_doc["theses"][0]["version"], 1, "unchanged rerun must be idempotent")
            third = thesen.update_register(report([thesis(text="Yields remain elevated")]), path)
            third_doc = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(third_doc["theses"][0]["version"], 2, "material change must increment version")
            thesen.update_register(report([]), path)
            archived = json.loads(Path(path).read_text(encoding="utf-8"))["theses"][0]
            self.assertFalse(archived["active"], "removed thesis must be archived, not deleted")

    def test_report_and_thesis_processing_do_not_mutate_portfolio_source(self):
        thesen = load_module("release_thesen_immutable", "trading_radar_thesen.py")
        with tempfile.TemporaryDirectory() as tmp:
            portfolio = Path(tmp) / "Offene_Positionen.csv"
            portfolio_bytes = b"Ticker;Status;Einstieg\nAAA;Offen;100\n"
            portfolio.write_bytes(portfolio_bytes)
            register = Path(tmp) / "theses.json"
            payload = {
                "data_cutoff": "2026-10-26T15:00:00+00:00",
                "theses": [{
                    "thesis_id": "T-1", "thesis": "Test thesis", "horizon": "short",
                    "evidence": ["source"], "counter_evidence": ["counter"],
                    "confidence": "low", "confirm_triggers": [], "invalidate_triggers": [],
                    "event_dependencies": [], "asset_classes": ["equity"], "candidates": ["AAA"],
                    "status": "NEU",
                }],
            }
            report = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(payload) + "\n```"
            thesen.update_register(report, str(register))
            self.assertEqual(portfolio.read_bytes(), portfolio_bytes, "thesis register update must not write source positions")

    def test_benchmark_golden_math_and_output_with_fixed_prices(self):
        # Isolate yfinance: no network calls; the golden result is fully deterministic.
        import sys
        import types
        sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: None))
        benchmark = load_module("release_benchmark", "performance_benchmark.py")
        import pandas as pd

        class FrozenDateTime(datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 12)

        positions = pd.DataFrame([
            {"Ticker": "AAA", "Name": "Alpha", "Status": "Verkauft", "Einstiegsdatum": "10.08.2026",
             "Ausstiegsdatum": "12.08.2026", "Einstieg": "100", "Ausstiegskurs": "110",
             "Aktueller_Kurs": "", "Richtung": "Long", "Ideen_Quelle": "Golden", "Performance_Seit_Einstieg%": ""},
            {"Ticker": "BBB", "Name": "Beta", "Status": "Offen", "Einstiegsdatum": "08.08.2026",
             "Ausstiegsdatum": "", "Einstieg": "100", "Ausstiegskurs": "", "Aktueller_Kurs": "105",
             "Richtung": "Long", "Ideen_Quelle": "Golden", "Performance_Seit_Einstieg%": "5"},
            {"Ticker": "EUNL.DE", "Name": "Benchmark", "Status": "Offen", "Einstiegsdatum": "08.08.2026",
             "Ausstiegsdatum": "", "Einstieg": "200", "Ausstiegskurs": "", "Aktueller_Kurs": "220",
             "Richtung": "Long", "Ideen_Quelle": "Benchmark", "Performance_Seit_Einstieg%": "10"},
        ])
        dates = pd.to_datetime(["2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12"])
        histories = {
            "EUNL.DE": pd.Series([200.0, 200.0, 210.0, 220.0], index=dates),
            "AAA": pd.Series([95.0, 100.0, 105.0, 110.0], index=dates),
            "BBB": pd.Series([100.0, 102.0, 103.0, 105.0], index=dates),
        }
        with (
            patch.object(benchmark, "_load_positions", return_value=positions),
            patch.object(benchmark, "_download_close", side_effect=lambda ticker, *_: histories[ticker]),
            patch.object(benchmark.dt, "datetime", FrozenDateTime),
            patch.object(benchmark, "OUTPUT_FILE", Path(tempfile.gettempdir()) / "benchmark_release_golden.txt"),
        ):
            result = benchmark.calculate_benchmark()
        self.assertAlmostEqual(result["open_avg"], 5.0, places=8)
        self.assertEqual(result["trade_count"], 2)
        self.assertAlmostEqual(result["system_avg"], 7.5, places=8)
        self.assertAlmostEqual(result["benchmark_avg"], 10.0, places=8)
        self.assertAlmostEqual(result["delta"], -2.5, places=8)
        self.assertIn("Out-/Underperformance: -2,50%", result["text"])
        self.assertNotIn("EUNL.DE | Long", result["text"], "benchmark ETF must not enter the system trade basket")


if __name__ == "__main__":
    unittest.main()
