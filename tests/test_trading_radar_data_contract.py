"""Statische Schnittstellen-Regression fuer den Trading-Radar-Handoff.

Prueft nur Dateivertraege und Workflow-Reihenfolge; Scanner-, Portfolio- und
Benchmarkberechnungen werden nicht ausgefuehrt oder veraendert.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
RADAR = ROOT / "Trading_Radar_Alleinauswertung.py"
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
BENCHMARK = ROOT / "performance_benchmark.py"


class TradingRadarDataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.radar_source = RADAR.read_text(encoding="utf-8-sig")
        cls.workflow_source = WORKFLOW.read_text(encoding="utf-8-sig")
        cls.benchmark_source = BENCHMARK.read_text(encoding="utf-8-sig")

    def test_radar_keeps_existing_scanner_and_portfolio_input_keys(self):
        for key in (
            '"Setups(...).csv"',
            '"Trendwende_Setups(...).csv"',
            '"Short_Setups(...).csv"',
            '"Offene Positionen+Check.csv"',
            '"Trade_Story_Universum(...).json"',
            '"Benchmark_Live.txt"',
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.radar_source)

    def test_radar_required_sources_include_portfolio_check_and_core_scanner(self):
        match = re.search(r"(?ms)^PFLICHT_DATEIEN\s*=\s*\{(.*?)^\}", self.radar_source)
        self.assertIsNotNone(match, "PFLICHT_DATEIEN-Kontrakt nicht gefunden")
        required = match.group(1)
        for key in (
            '"briefing.txt"',
            '"Setups(...).csv"',
            '"Performance(...).csv"',
            '"Performance_EU(...).csv"',
            '"Trade_Story_Aktienuniversum(...).csv"',
            '"Offene Positionen+Check.csv"',
        ):
            with self.subTest(key=key):
                self.assertIn(key, required)

    def test_live_benchmark_contract_stays_on_raw_open_positions(self):
        self.assertIn('POSITIONS_FILE = Path("Offene_Positionen.csv")', self.benchmark_source)
        self.assertIn('OUTPUT_FILE = Path("Benchmark_Live.txt")', self.benchmark_source)
        self.assertIn('"Benchmark_Live.txt"', self.radar_source)
        # Check-Datei und Live-Benchmark bleiben getrennte Quellen/Pfade.
        self.assertNotIn('POSITIONS_FILE = Path("Offene Positionen+Check.csv")', self.benchmark_source)

    def test_workflow_generates_upstream_sources_before_radar(self):
        steps = (
            "Offene Positionen + Check erstellen und in Drive aktualisieren",
            "Live-Benchmark gegen MSCI World aktualisieren",
            "Analyse ausführen",
            "Trendwende-Scan ausführen",
            "Edelmetalle-Scan ausführen",
            "Trading-Radar erstellen",
        )
        positions = []
        for step in steps:
            found = self.workflow_source.find(f"- name: {step}")
            self.assertGreaterEqual(found, 0, f"Workflow-Schritt fehlt: {step}")
            positions.append(found)
        self.assertEqual(positions, sorted(positions), "Radar muss nach seinen vorgelagerten Datenquellen laufen")

    def test_standalone_radar_uses_shared_report_gate_and_thesis_register(self):
        self.assertIn("from trading_radar_report_gate import validate_report", self.radar_source)
        self.assertIn("from trading_radar_thesen import extract_thesis_payload, validate_theses", self.radar_source)
        self.assertIn("TRADING_RADAR_THESEN_JSON", self.radar_source)
        # Register write is in the standalone save branch as well as the legacy path.
        standalone = self.radar_source[self.radar_source.find("def speichere_ergebnis(text, eingabedateien=None):"): ]
        self.assertIn("update_register(text", standalone)

    def test_nonfinal_radar_is_marked_not_silently_promoted(self):
        self.assertIn('"TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"', self.radar_source)
        self.assertIn('"TRADING_RADAR_STATUS=NICHT_FINAL | Befunde="', self.radar_source)

    def test_radar_candidate_loader_is_explicitly_scanner_only(self):
        start = self.radar_source.find("def _lade_aktuelle_scanner_kandidaten")
        self.assertGreaterEqual(start, 0)
        section = self.radar_source[start:start + 9000]
        for scanner in (
            '"Setups(...).csv"',
            '"Trendwende_Setups(...).csv"',
            '"Short_Setups(...).csv"',
        ):
            self.assertIn(scanner, section)
        self.assertIn("crv < 1.0", section)
        self.assertIn("entry or not stop or not tp1", section)
        self.assertIn("source_date != report_date", section)


if __name__ == "__main__":
    unittest.main()
