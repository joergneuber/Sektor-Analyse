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

    def test_invalid_long_stop_is_explicit_and_not_zero_risk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Offene_Positionen.csv"
            self._write_csv(path, ["Ticker", "Einstieg", "Stop", "Richtung", "Stückzahl"],
                            [{"Ticker": "AAA", "Einstieg": "100", "Stop": "105",
                              "Richtung": "LONG", "Stückzahl": "2"}])
            with patch.object(evidence, "ROOT", root):
                meta = evidence._metadata(path)
                risk = evidence._risk_metrics(path, meta)
            self.assertEqual(risk["monetary_risk"], None)
            self.assertEqual(risk["monetary_risk_status"], "INVALID_STOP_PRESENT")
            self.assertEqual(len(risk["invalid_stop_rows"]), 1)


if __name__ == "__main__":
    unittest.main()
