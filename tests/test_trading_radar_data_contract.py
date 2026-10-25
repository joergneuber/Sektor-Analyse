"""Ausfuehrbare Vertrags- und Integrationstests fuer den Trading-Radar-Handoff.

Die Tests benoetigen weder Gemini-Zugang noch Google-Drive-Credentials.
Ein fehlender Workflow wird als explizite Skip-Meldung behandelt, nicht als
impliziter Erfolg. Der Report-Speicherpfad wird mit echten temporaeren Dateien
und einem erzeugten Radarbericht durchlaufen.
"""
from pathlib import Path
import csv
import datetime
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO

ROOT = Path(__file__).resolve().parents[1]
RADAR = ROOT / "Trading_Radar_Alleinauswertung.py"
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
BENCHMARK = ROOT / "performance_benchmark.py"


def load_radar_module():
    """Importiert die Datei ohne optionale Projekt-/API-Abhaengigkeiten."""
    trade_story = types.ModuleType("trade_story_universum")
    trade_story.write_trade_story_universe = lambda *a, **k: None
    sources = types.ModuleType("quellenwert_vertrag")
    sources.values_match_display = lambda *a, **k: False
    sources.find_exact_labeled_line = lambda *a, **k: None
    sources.extract_four_week_percent = lambda *a, **k: None
    sources.extract_metal_four_week_value = lambda *a, **k: None
    sys.modules.setdefault("trade_story_universum", trade_story)
    sys.modules.setdefault("quellenwert_vertrag", sources)
    spec = importlib.util.spec_from_file_location("trading_radar_under_test", RADAR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TradingRadarDataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.radar_source = RADAR.read_text(encoding="utf-8-sig")

    def test_workflow_presence_is_reported_not_assumed(self):
        if not WORKFLOW.is_file():
            self.skipTest("Runner-/Workflow-End-to-End nicht pruefbar: .github/workflows/main.yml fehlt in dieser Anlage")
        workflow = WORKFLOW.read_text(encoding="utf-8-sig")
        self.assertIn("Trading-Radar erstellen", workflow)

    def test_benchmark_contract_only_when_source_is_in_package(self):
        if not BENCHMARK.is_file():
            self.skipTest("Benchmark-Vertrag nicht pruefbar: performance_benchmark.py fehlt in dieser Anlage")
        benchmark = BENCHMARK.read_text(encoding="utf-8-sig")
        self.assertIn('OUTPUT_FILE = Path("Benchmark_Live.txt")', benchmark)

    def test_report_prompt_has_explicit_m05_m10_fields_and_no_fabrication_rules(self):
        self.assertIn("REPORT-GATE-VERTRAG", self.radar_source)
        self.assertIn("Keine neue oder aktualisierte", self.radar_source)
        self.assertIn("Portfolio-Gesamtperformance", self.radar_source)
        self.assertIn("OHLCV-Abdeckung", self.radar_source)

    def test_workflow_keeps_non_final_as_artifact_not_regular_publication(self):
        if not WORKFLOW.is_file():
            self.skipTest("Workflow fehlt in dieser Anlage")
        workflow = WORKFLOW.read_text(encoding="utf-8-sig")
        self.assertIn("Trading-Radar-Laufbericht als Historien-Artifact sichern", workflow)
        self.assertIn("trading-radar-${{ steps.trading-radar.outputs.fachlich_final || 'STATUS-UNBEKANNT' }}-${{ github.run_id }}", workflow)
        self.assertIn("retention-days: 90", workflow)
        self.assertIn("steps.trading-radar-report-check.outcome == 'success' && steps.trading-radar.outputs.fachlich_final == 'FINAL'", workflow)
        self.assertIn("steps.trading-radar.outputs.fachlich_final == 'FINAL'", workflow)

    def test_radar_prompt_keeps_a_b_c_as_non_authorization(self):
        self.assertIn("interne Kategorisierungen der Setups", self.radar_source)
        self.assertIn("KEINE finale Kaufentscheidung", self.radar_source)

    def test_radar_filename_contract_exists_at_both_save_paths(self):
        self.assertGreaterEqual(self.radar_source.count("TRADING_RADAR_DATEINAME_UNGUELTIG"), 2)
        self.assertIn(r"Trading_Radar\(\d{4}-\d{2}-\d{2}\)\.txt", self.radar_source)
        self.assertIn("TRADING_RADAR_ABLAGEPFAD_UNGUELTIG", self.radar_source)

    def test_closed_positions_loader_has_no_date_filter_and_keeps_unparseable_dates(self):
        start = self.radar_source.find("def lade_offenen_positionen_check_tab2")
        end = self.radar_source.find("\ndef ", start + 5)
        section = self.radar_source[start:end if end >= 0 else None]
        self.assertGreaterEqual(start, 0)
        self.assertIn("selected = list(rows)", section)
        self.assertNotIn("timedelta(days=2)", section)
        self.assertIn("TAB-2-ANZAHL", section)
        self.assertIn("Unparsebare Datumswerte", section)

    def test_open_positions_source_block_has_count_and_rejects_empty_export(self):
        start = self.radar_source.find("def _offene_positionen_quellblock")
        end = self.radar_source.find("\ndef ", start + 5)
        section = self.radar_source[start:end if end >= 0 else None]
        self.assertGreaterEqual(start, 0)
        self.assertIn("OFFENE_POSITIONEN_QUELLANZAHL={offene_quellzeilen}", section)
        self.assertIn("OFFENE_POSITIONEN_ANZAHL={export_count}", section)
        self.assertIn("Quellen-/Exportzählung abweichend", section)
        self.assertIn("nicht zuordenbar", section)
        self.assertIn("Keine offenen Positionen exportiert", section)

    def test_actual_report_writer_creates_canonical_report_and_complete_facts(self):
        radar = load_radar_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                csv_path = Path(temp_dir) / "Offene Positionen+Check.csv"
                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, delimiter=";", fieldnames=[
                        "Name", "Ticker", "Status", "Einstieg", "Einstiegsdatum", "Stop"
                    ])
                    writer.writeheader()
                    writer.writerows([
                        {"Name": "Alpha", "Ticker": "AAA", "Status": "Offen", "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9"},
                        {"Name": "Beta", "Ticker": "BBB", "Status": "Offen", "Einstieg": "20", "Einstiegsdatum": "02.01.2026", "Stop": "18"},
                        {"Name": "Closed", "Ticker": "CCC", "Status": "Geschlossen", "Einstieg": "30", "Einstiegsdatum": "03.01.2026", "Stop": "25"},
                    ])
                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
                    "TAB-2-ANZAHL=51\nTAB-2-QUELLE=Geschlossene Positionen\n"
                    "Ticker: OLD | Ausstiegsdatum: 2019-01-01\n"
                    "Ticker: BADDATE | Ausstiegsdatum: unbekannt\n"
                    + "\n".join(
                        f"Ticker: CLOSED{i:02d} | Ausstiegsdatum: 2026-10-24"
                        for i in range(1, 50)
                    )
                )
                report = "\n".join(f"{i}. {title}" for i, title in enumerate([
                    "Trading-Radar", "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
                    "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang"
                ], start=1)) + (
                    "\n10.5 Geschlossene Positionen\n"
                    "Ticker: STALE | alte Gemini-Zeile darf nicht erhalten bleiben\n"
                    "TAB-2-QUELLE NICHT VERIFIZIERBAR: alter Gemini-Platzhalter\n"
                    "11. METHODIK / DATENQUALITÄT\n"
                    "11.1 Datenstatus\nMethodik-Inhalt muss erhalten bleiben.\n"
                    "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
                )
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(report, {"Offene Positionen+Check.csv": str(csv_path)})
                output_path = Path(output)
                self.assertEqual(output_path.name, f"Trading_Radar({datetime.date.today().isoformat()}).txt")
                self.assertTrue(output_path.is_file())
                saved = output_path.read_text(encoding="utf-8-sig")
                self.assertIn("OFFENE_POSITIONEN_QUELLANZAHL=2", saved)
                self.assertIn("OFFENE_POSITIONEN_ANZAHL=2", saved)
                self.assertIn("Alpha (AAA)", saved)
                self.assertIn("Beta (BBB)", saved)
                self.assertNotIn("Closed (CCC)", saved)
                self.assertIn("Ticker: OLD | Ausstiegsdatum: 2019-01-01", saved)
                self.assertIn("Ticker: BADDATE | Ausstiegsdatum: unbekannt", saved)
                closed_start = saved.index("10.5 Geschlossene Positionen")
                closed_end = saved.index("11. METHODIK / DATENQUALITÄT")
                closed_section = saved[closed_start:closed_end]
                self.assertIn("TAB-2-ANZAHL=51", closed_section)
                self.assertEqual(len(re.findall(r"(?m)^Ticker:\s+\S+", closed_section)), 51)
                # Regression: alter Gemini-10.5-Inhalt muss vollständig weg,
                # der deterministische Quellenblock steht vor Abschnitt 11 und Status.
                self.assertNotIn("Ticker: STALE", saved)
                self.assertNotIn("alter Gemini-Platzhalter", saved)
                self.assertLess(saved.index("10.5 Geschlossene Positionen"), saved.index("11. METHODIK / DATENQUALITÄT"))
                self.assertLess(saved.index("10.5 Geschlossene Positionen"), saved.index("TRADING_RADAR_FACHLICH_FINAL="))
                self.assertIn("11.1 Datenstatus", saved)
                self.assertIn("Methodik-Inhalt muss erhalten bleiben.", saved)
                self.assertEqual(saved.count("10.5 Geschlossene Positionen"), 1)
                self.assertEqual(saved.count("TRADING_RADAR_FACHLICH_FINAL="), 1)
                self.assertIn("Portfolio-Gesamtperformance nicht verfügbar", saved)
                self.assertIn("Ø Positionsperformance nicht verfügbar", saved)
                self.assertEqual(output_path.parent, Path(temp_dir))
            finally:
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
