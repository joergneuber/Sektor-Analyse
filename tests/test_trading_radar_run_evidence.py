"""Regression tests for source coverage and portfolio risk evidence."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "trading_radar_run_evidence.py"
spec = importlib.util.spec_from_file_location("radar_evidence", MODULE_PATH)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class RunEvidenceTests(unittest.TestCase):
    def _write_csv(self, path, headers, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def test_available_requires_all_three_source_categories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_csv(root / "Offene_Positionen.csv",
                            ["Ticker", "Einstieg", "Stop", "Richtung", "Stückzahl"],
                            [{"Ticker": "AAA", "Einstieg": "100", "Stop": "95",
                              "Richtung": "LONG", "Stückzahl": "2"}])
            with patch.object(evidence, "ROOT", root):
                snapshot = evidence.collect_snapshot("start")
            self.assertEqual(snapshot["coverage"]["portfolio"]["status"], "AVAILABLE")
            self.assertEqual(snapshot["coverage"]["candidates"]["status"], "MISSING")
            self.assertEqual(snapshot["coverage"]["events"]["status"], "MISSING")
            self.assertEqual(snapshot["overall_status"], "INCOMPLETE")
            self.assertFalse(snapshot["coverage_complete"])

    def test_supported_source_formats_and_all_categories_can_be_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_csv(root / "Offene_Positionen.csv",
                            ["Ticker", "Einstieg", "Stop", "Richtung", "Stückzahl"],
                            [{"Ticker": "AAA", "Einstieg": "100", "Stop": "95",
                              "Richtung": "LONG", "Stückzahl": "2"}])
            self._write_csv(root / "trendwende_scanner.csv",
                            ["Ticker", "Einstieg", "Stop", "CRV"],
                            [{"Ticker": "BBB", "Einstieg": "10", "Stop": "9", "CRV": "1.2"}])
            (root / "fomc_termine.json").write_text(json.dumps([{"date": "2026-11-01"}]), encoding="utf-8")
            with patch.object(evidence, "ROOT", root):
                snapshot = evidence.collect_snapshot("end")
            self.assertEqual(snapshot["overall_status"], "AVAILABLE")
            self.assertTrue(all(snapshot["coverage"][k]["complete"] for k in ("portfolio", "candidates", "events")))
            self.assertTrue(any(s["format"] == "json" for s in snapshot["sources"]))

    def test_trailing_long_stop_above_entry_is_valid_and_quantity_is_fixed_one(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Offene_Positionen.csv"
            self._write_csv(path, ["Ticker", "Einstieg", "Aktueller_Kurs", "Stop", "Richtung", "Stückzahl"],
                            [{"Ticker": "AAA", "Einstieg": "100", "Aktueller_Kurs": "110",
                              "Stop": "105", "Richtung": "LONG", "Stückzahl": "99"}])
            with patch.object(evidence, "ROOT", root):
                meta = evidence._metadata(path)
                risk = evidence._risk_metrics(path, meta)
            self.assertEqual(risk["monetary_risk"], 5.0)
            self.assertEqual(risk["monetary_risk_status"], "CALCULATED_ASSUMING_ONE_UNIT_PER_POSITION")
            self.assertEqual(risk["assumed_position_quantity_per_position"], 1)
            self.assertEqual(risk["locked_in_profit_per_unit"][0]["amount"], 5.0)
            self.assertEqual(risk["invalid_stop_rows"], [])

    def test_long_stop_above_current_price_is_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Offene_Positionen.csv"
            self._write_csv(path, ["Ticker", "Einstieg", "Aktueller_Kurs", "Stop", "Richtung"],
                            [{"Ticker": "AAA", "Einstieg": "100", "Aktueller_Kurs": "104",
                              "Stop": "105", "Richtung": "LONG"}])
            with patch.object(evidence, "ROOT", root):
                risk = evidence._risk_metrics(path, evidence._metadata(path))
            self.assertEqual(risk["monetary_risk"], None)
            self.assertEqual(risk["monetary_risk_status"], "INVALID_STOP_RELATIVE_TO_CURRENT_PRICE")
            self.assertEqual(len(risk["invalid_stop_rows"]), 1)

    def test_semicolon_csv_with_commas_in_names_is_parsed_correctly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Offene_Positionen.csv"
            path.write_text('Ticker;Name;Richtung;Einstieg;Aktueller_Kurs;Stop;Status\n'
                            'AAA;Acme, Inc.;Long;100;110;105;Offen\n', encoding="utf-8")
            with patch.object(evidence, "ROOT", root):
                meta = evidence._metadata(path)
                risk = evidence._risk_metrics(path, meta)
            self.assertEqual(meta["columns"], ["Ticker", "Name", "Richtung", "Einstieg", "Aktueller_Kurs", "Stop", "Status"])
            self.assertEqual(risk["monetary_risk"], 5.0)
            self.assertEqual(risk["positions_counted"], 1)

    def test_closed_position_history_is_excluded_from_open_risk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Geschlossene Positionen.csv"
            self._write_csv(path, ["Ticker", "Einstieg", "Aktueller_Kurs", "Stop", "Richtung", "Status"],
                            [{"Ticker": "AAA", "Einstieg": "100", "Aktueller_Kurs": "90",
                              "Stop": "95", "Richtung": "LONG", "Status": "Gestoppt"}])
            with patch.object(evidence, "ROOT", root):
                risk = evidence._risk_metrics(path, evidence._metadata(path))
            self.assertEqual(risk["scope"], "CLOSED_HISTORY_EXCLUDED_FROM_OPEN_RISK")
            self.assertEqual(risk["monetary_risk"], None)
            self.assertEqual(risk["monetary_risk_status"], "EXCLUDED_CLOSED_POSITION_HISTORY")

    def test_large_valid_json_is_not_misreported_as_truncated_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "large_cache.json"
            payload = {"items": ["x" * 100 for _ in range(12000)]}
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertGreater(path.stat().st_size, 1_000_000)
            meta = evidence._metadata(path)
            self.assertIsNone(meta["read_error"])
            self.assertEqual(meta["json_top_level_type"], "dict")
            self.assertIn("items", meta["json_top_level_keys"])


if __name__ == "__main__":
    unittest.main()
