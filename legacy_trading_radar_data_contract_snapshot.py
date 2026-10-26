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
import shutil
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


def load_radar_module(radar_path=RADAR):
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
    spec = importlib.util.spec_from_file_location("trading_radar_under_test", radar_path)
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

    def test_duplicate_ticker_entry_date_keeps_all_source_rows_but_no_export_markers(self):
        radar = load_radar_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                csv_path = Path(temp_dir) / "Offene Positionen+Check.csv"
                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, delimiter=";",
                        fieldnames=["Name", "Ticker", "Status", "Einstieg", "Einstiegsdatum", "Stop"]
                    )
                    writer.writeheader()
                    writer.writerows([
                        {"Name": "Alpha", "Ticker": "AAA", "Status": "Offen", "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9"},
                    ])
                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
                    "TAB-2-ANZAHL=2\nTAB-2-QUELLE=Geschlossene Positionen\n"
                    "Quellzeile: 3 | Ticker: AAA | Einstiegsdatum: 01.01.2026 | Status: Gestoppt\n"
                    "Quellzeile: 4 | Ticker: AAA | Einstiegsdatum: 01.01.2026 | Status: Verkauft"
                )
                report = "\n".join(
                    f"{i}. {title}\n" + ("Ausreichend dokumentierter Abschnittsinhalt und Prüfkontext." * 2)
                    for i, title in enumerate([
                        "Trading-Radar", "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
                        "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang"
                    ], start=1)
                ) + "\nTRADING_RADAR_FACHLICH_FINAL=FINAL\n"
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(
                        report, {"Offene Positionen+Check.csv": str(csv_path)}
                    )
                saved = Path(output).read_text(encoding="utf-8-sig")
                closed_start = saved.index("10.5 Geschlossene Positionen")
                closed_end = saved.rfind("TRADING_RADAR_FACHLICH_FINAL=")
                closed_section = saved[closed_start:closed_end]
                self.assertIn("TAB-2-QUELLE NICHT VERIFIZIERBAR", closed_section)
                self.assertIn("Quellzeile: 3 | Ticker: AAA | Einstiegsdatum: 01.01.2026", closed_section)
                self.assertIn("Quellzeile: 4 | Ticker: AAA | Einstiegsdatum: 01.01.2026", closed_section)
                self.assertNotIn("TAB-2-ANZAHL=", closed_section)
                self.assertNotIn("TAB-2-QUELLE=Geschlossene Positionen", closed_section)
                self.assertNotIn("TAB-2-EXPORT-SHA256=", closed_section)
                self.assertTrue(saved.rstrip().endswith("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"))
            finally:
                os.chdir(previous_cwd)

    def test_workflow_keeps_non_final_as_artifact_not_regular_publication(self):
        if not WORKFLOW.is_file():
            self.skipTest("Workflow fehlt in dieser Anlage")
        workflow = WORKFLOW.read_text(encoding="utf-8-sig")
        self.assertIn("Trading-Radar-Laufbericht als Historien-Artifact sichern", workflow)
        self.assertIn("trading-radar-${{ steps.trading-radar-report-check.outputs.TRADING_RADAR_FACHLICH_FINAL || 'NICHT_FINAL' }}-${{ github.run_id }}", workflow)
        self.assertIn("if ! python trading_radar_report_gate.py", workflow)
        self.assertIn("s/^TRADING_RADAR_FACHLICH_FINAL=FINAL$/TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL/", workflow)
        self.assertIn("retention-days: 90", workflow)
        self.assertIn("steps.trading-radar-report-check.outcome == 'success' && steps.trading-radar.outputs.fachlich_final == 'FINAL'", workflow)
        self.assertIn("steps.trading-radar.outputs.fachlich_final == 'FINAL'", workflow)

    def test_actual_report_writer_preserves_all_chapter_headings_with_msci_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            isolated_radar = temp_root / "Trading_Radar_Alleinauswertung.py"
            shutil.copy2(RADAR, isolated_radar)
            (temp_root / ".firma_name_cache.json").write_text(
                '{"MSCI": {"name": "MSCI Inc.", "missing": false}, '
                '"CME": {"name": "CME Group Inc.", "missing": false}}',
                encoding="utf-8",
            )
            radar = load_radar_module(isolated_radar)
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                csv_path = temp_root / "Offene Positionen+Check.csv"
                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, delimiter=";",
                        fieldnames=["Name", "Ticker", "Status", "Einstieg", "Einstiegsdatum", "Stop"],
                    )
                    writer.writeheader()
                    writer.writerow({
                        "Name": "Alpha", "Ticker": "AAA", "Status": "Offen",
                        "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9",
                    })
                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
                    "TAB-2-ANZAHL=0\nTAB-2-QUELLE=Geschlossene Positionen\n"
                )
                headings = [
                    "1. Trading-Radar", "2. Thesen-Update", "3. Ereignisradar",
                    "4. Assetklassenvergleich", "5. Technische Kandidaten",
                    "6. Portfolio & Risiko", "7. MSCI-Vergleich",
                    "8. Daten-/Prüfanhang", "8.1 Unterkapitel",
                    "9. Risikohinweise", "11. Methodische Hinweise",
                ]
                report = "\n".join(headings) + (
                    "\nMSCI wird im Fliesstext genannt.\n"
                    "MSCI World ist der Benchmark.\n"
                    "CME Group Inc. (CME) wird ebenfalls genannt.\n"
                    "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
                )
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(
                        report, {"Offene Positionen+Check.csv": str(csv_path)}
                    )
                saved = Path(output).read_text(encoding="utf-8-sig")
                saved_lines = saved.splitlines()
                for heading in headings:
                    with self.subTest(heading=heading):
                        self.assertIn(heading, saved_lines)
                self.assertIn("MSCI Inc. (MSCI) wird im Fliesstext genannt.", saved)
                self.assertIn("MSCI World ist der Benchmark.", saved)
                self.assertNotIn("MSCI Inc. (MSCI) World", saved)
                self.assertIn("CME Group Inc. (CME) wird ebenfalls genannt.", saved)
                self.assertNotIn("CME Group Inc. (CME) Group Inc.", saved)
                self.assertIn("TRADING_RADAR_OFFENE_POSITIONEN_BEGIN", saved)
                self.assertNotIn("Name nicht aufgelöst (TRADING_RADAR_OFFENE)", saved)
                self.assertNotIn("Name nicht aufgelöst (POSITIONEN_BEGIN)", saved)
                self.assertIn("7. MSCI-Vergleich", saved_lines)
                self.assertNotIn("7. MSCI Inc. (MSCI)-Vergleich", saved_lines)
            finally:
                os.chdir(previous_cwd)

    def test_final_verification_has_separate_data_contract_failure_diagnostic(self):
        self.assertTrue(WORKFLOW.is_file(), "main.yml fehlt; Workflow-Diagnose kann nicht verifiziert werden")
        workflow = WORKFLOW.read_text(encoding="utf-8-sig")
        self.assertIn(
            "TRADING_RADAR_CONTRACT_OUTCOME: ${{ steps.trading-radar-contract.outcome }}",
            workflow,
        )
        contract_check = 'if [ "${TRADING_RADAR_CONTRACT_OUTCOME:-}" != "success" ]; then'
        gemini_check = 'if [ "$TRADING_RADAR_STEP_OUTCOME" != "success" ]; then'
        self.assertIn(contract_check, workflow)
        self.assertIn(gemini_check, workflow)
        self.assertLess(workflow.index(contract_check), workflow.index(gemini_check))
        self.assertIn(
            "kein Gemini-Fehler diagnostiziert.",
            workflow,
            "Bei fehlgeschlagener Vorprüfung muss die Meldung den Fehler eindeutig vom Gemini-Ausfall abgrenzen.",
        )
        self.assertIn("REPORT_CHECK=FAIL | Trading-Radar-Datenvertrag-Vorprüfung fehlgeschlagen", workflow)

    def test_report_field_normalizer_explicitly_marks_missing_m06_m08_values(self):
        radar = load_radar_module()
        report = (
            "3. Ereignisradar\nEin Ereignis wird beobachtet, aber Quellenwerte fehlen.\n"
            "4. Assetklassenvergleich\nAssetklassen werden verglichen.\n"
            "7. MSCI-Vergleich\nPortfolio-Gesamtperformance: TBD; Ø Positionsperformance: Platzhalter.\n"
            "8. Daten-/Prüfanhang\nDatenstatus dokumentiert.\n"
        )
        normalized = radar._trading_radar_berichtsfelder_normalisieren(report)
        for field in ("Uhrzeit", "Zeitzone", "Kennzahl", "Berichtszeitraum", "Letzter Wert", "Konsens"):
            self.assertIn(f"{field}: NICHT VERFÜGBAR", normalized)
        self.assertIn("Portfolio-Gesamtperformance: NICHT VERFÜGBAR", normalized)
        self.assertIn("Ø Positionsperformance: NICHT VERFÜGBAR", normalized)
        self.assertNotIn(": TBD", normalized)
        self.assertNotIn(": Platzhalter", normalized)

    def test_m08_normalizer_fills_each_event_record_independently(self):
        radar = load_radar_module()
        report = (
            "3. Ereignisradar\n"
            "- CPI-Veröffentlichung | Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | "
            "Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9\n"
            "- Arbeitsmarktdaten | Uhrzeit: TBD | Zeitzone: ET | Kennzahl: NFP | "
            "Berichtszeitraum: Oktober 2026\n"
            "4. Assetklassenvergleich\nVergleich dokumentiert.\n"
        )
        normalized = radar._trading_radar_berichtsfelder_normalisieren(report)
        event_section = normalized.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
        event_bullets = re.findall(r"(?m)^- (?:CPI-Veröffentlichung|Arbeitsmarktdaten)", event_section)
        self.assertEqual(len(event_bullets), 2)
        self.assertIn("Uhrzeit: NICHT VERFÜGBAR", event_section)
        self.assertIn("Letzter Wert: NICHT VERFÜGBAR", event_section)
        self.assertIn("Zeitzone: ET", event_section)
        self.assertIn("Kennzahl: NFP", event_section)
        self.assertNotIn("Uhrzeit: TBD", event_section)

    def test_m08_normalizer_output_passes_event_gate_for_each_record(self):
        radar = load_radar_module()
        gate_spec = importlib.util.spec_from_file_location(
            "trading_radar_gate_m08_integration", ROOT / "trading_radar_report_gate.py"
        )
        gate_module = importlib.util.module_from_spec(gate_spec)
        gate_spec.loader.exec_module(gate_module)
        fixture_spec = importlib.util.spec_from_file_location(
            "trading_radar_gate_fixture", ROOT / "tests" / "test_trading_radar_report_gate.py"
        )
        fixture_module = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture_module)
        report = fixture_module.report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "- CPI-Veröffentlichung | Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.\n"
            "- Arbeitsmarktdaten | Uhrzeit: TBD | Zeitzone: ET | Kennzahl: NFP | Berichtszeitraum: Oktober 2026.",
        )
        normalized = radar._trading_radar_berichtsfelder_normalisieren(report)
        errors = gate_module.validate_report(normalized)
        self.assertFalse(any("M-08" in error for error in errors), errors)

    def test_radar_prompt_keeps_a_b_c_as_non_authorization(self):
        self.assertIn("interne Kategorisierungen der Setups", self.radar_source)
        self.assertIn("KEINE finale Kaufentscheidung", self.radar_source)

    def test_radar_filename_contract_exists_at_both_save_paths(self):
        self.assertGreaterEqual(self.radar_source.count("TRADING_RADAR_DATEINAME_UNGUELTIG"), 2)
        self.assertIn(r"Trading_Radar\(\d{4}-\d{2}-\d{2}\)\.txt", self.radar_source)
        self.assertIn("TRADING_RADAR_ABLAGEPFAD_UNGUELTIG", self.radar_source)

    def test_tab2_entry_date_text_parser_rejects_nonempty_invalid_dates(self):
        radar = load_radar_module()
        for invalid_date in ("32.13.2026", "2026-02-30", "Datum unbekannt", "TBD", ""):
            with self.subTest(invalid_date=invalid_date):
                self.assertIsNone(radar._parse_tab2_entry_date_text(invalid_date))
        for valid_date in ("02.08.2026", "2026-08-02", "02/08/2026"):
            with self.subTest(valid_date=valid_date):
                self.assertEqual(
                    radar._parse_tab2_entry_date_text(valid_date),
                    datetime.date(2026, 8, 2),
                )

    def test_generator_marks_nonempty_invalid_entry_date_nonverifiable(self):
        radar = load_radar_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                csv_path = Path(temp_dir) / "Offene Positionen+Check.csv"
                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, delimiter=";",
                        fieldnames=["Name", "Ticker", "Status", "Einstieg", "Einstiegsdatum", "Stop"],
                    )
                    writer.writeheader()
                    writer.writerow({
                        "Name": "Alpha", "Ticker": "AAA", "Status": "Offen",
                        "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9",
                    })
                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
                radar._GEMINI_GESCHlossene_10_5_FINAL = ""
                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
                    "TAB-2-ANZAHL=1\nTAB-2-QUELLE=Geschlossene Positionen\n"
                    "Quellzeile: 3 | Ticker: AAA | Einstiegsdatum: 32.13.2026 | Status: Verkauft"
                )
                report = "\n".join(
                    f"{i}. {title}\n" + ("Ausreichend dokumentierter Abschnittsinhalt und Prüfkontext." * 2)
                    for i, title in enumerate([
                        "Trading-Radar", "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
                        "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang"
                    ], start=1)
                ) + "\nTRADING_RADAR_FACHLICH_FINAL=FINAL\n"
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(
                        report, {"Offene Positionen+Check.csv": str(csv_path)}
                    )
                saved = Path(output).read_text(encoding="utf-8-sig")
                closed_section = saved[saved.index("10.5 Geschlossene Positionen"):saved.rfind("TRADING_RADAR_FACHLICH_FINAL=")]
                self.assertIn("TAB-2-QUELLE NICHT VERIFIZIERBAR", closed_section)
                self.assertIn("Einstiegsdatum: 32.13.2026", closed_section)
                self.assertNotIn("TAB-2-ANZAHL=", closed_section)
                self.assertNotIn("TAB-2-QUELLE=Geschlossene Positionen", closed_section)
                self.assertNotIn("TAB-2-EXPORT-SHA256=", closed_section)
                self.assertTrue(saved.rstrip().endswith("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"))
            finally:
                os.chdir(previous_cwd)

    def test_closed_positions_loader_has_no_date_filter_and_keeps_unparseable_dates(self):
        start = self.radar_source.find("def lade_offenen_positionen_check_tab2")
        end = self.radar_source.find("\ndef ", start + 5)
        section = self.radar_source[start:end if end >= 0 else None]
        self.assertGreaterEqual(start, 0)
        self.assertIn("selected = list(zip(source_row_numbers, rows))", section)
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


    def test_tab2_source_row_numbers_survive_blank_rows(self):
        radar = load_radar_module()
        source_rows = [
            ["AAA", "Closed"],  # sheet row 3
            ["", ""],           # sheet row 4 is blank and omitted
            ["", "Closed"],     # sheet row 5 is populated but missing ticker
            ["BBB", "Closed"],  # sheet row 6
        ]
        nonempty, row_numbers = radar._tab2_nonempty_rows_with_source_numbers(
            source_rows, start_sheet_row=3
        )
        self.assertEqual(nonempty, [source_rows[0], source_rows[2], source_rows[3]])
        self.assertEqual(row_numbers, [3, 5, 6])

    def test_report_section_boundaries_are_robust_and_source_has_no_bom(self):
        source_bytes = RADAR.read_bytes()
        self.assertFalse(source_bytes.startswith(b"\xef\xbb\xbf"))
        source = source_bytes.decode("utf-8")
        self.assertIn(r"11(?:\s*[.·](?!\d)|\s+)", source)
        self.assertIn("ohne belegte Grenze wird Abschnitt 7 nicht verändert", source)
        self.assertIn("Niemals bis EOF erweitern", source)
        self.assertIn(r"(?:\s*[.·](?!\d)|\s+(?!\d))", source)

    def test_actual_report_writer_bounds_performance_section_when_section8_is_missing(self):
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
                    writer.writerow({
                        "Name": "Alpha", "Ticker": "AAA", "Status": "Offen",
                        "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9"
                    })
                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
                    "TAB-2-ANZAHL=0\nTAB-2-QUELLE=Geschlossene Positionen\n"
                )
                # Abschnitt 8 fehlt. Abschnitt 9 muss trotzdem die harte Grenze
                # fuer Abschnitt 7 sein; die Kennzahlen duerfen nicht dahinter landen.
                report = (
                    "7. MSCI-Vergleich\n"
                    "Nur bestehende Angaben aus Abschnitt 7.\n"
                    "8.1 Unterkapitel mit ergänzenden Hinweisen\n"
                    "Dieser Text gehört weiterhin zum Bericht und darf keine Abschnittsgrenze auslösen.\n"
                    "9. Risikohinweise\n"
                    "Dieser Text gehoert ausschliesslich zu Abschnitt 9.\n"
                    "11. Methodische Hinweise und Datenqualität\n"
                    "Methodik bleibt erhalten.\n"
                    "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
                )
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(
                        report, {"Offene Positionen+Check.csv": str(csv_path)}
                    )
                saved = Path(output).read_text(encoding="utf-8-sig")
                section7_start = saved.index("7. MSCI-Vergleich")
                section9_start = saved.index("9. Risikohinweise")
                section7 = saved[section7_start:section9_start]
                section9_start = saved.index("9. Risikohinweise")
                section11_start = saved.index("11. Methodische Hinweise und Datenqualität")
                section9 = saved[section9_start:section11_start]
                self.assertIn("Portfolio-Gesamtperformance nicht verfügbar", section7)
                self.assertIn("Ø Positionsperformance nicht verfügbar", section7)
                self.assertNotIn("Portfolio-Gesamtperformance nicht verfügbar", section9)
                self.assertNotIn("Ø Positionsperformance nicht verfügbar", section9)
                self.assertIn("8.1 Unterkapitel mit ergänzenden Hinweisen", section7)
                self.assertIn("Dieser Text gehört weiterhin zum Bericht und darf keine Abschnittsgrenze auslösen.", section7)
                self.assertIn("Dieser Text gehoert ausschliesslich zu Abschnitt 9.", section9)
            finally:
                os.chdir(previous_cwd)

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
                    "Quellzeile: 3 | Ticker: OLD | Einstiegsdatum: 01.01.2019 | Ausstiegsdatum: 2019-01-01\n"
                    "Quellzeile: 4 | Ticker: BADDATE | Einstiegsdatum: 02.01.2019 | Ausstiegsdatum: unbekannt\n"
                    + "\n".join(
                        (
                            f"Quellzeile: {i + 4} | Ticker: OLD | Einstiegsdatum: {i + 2:02d}.01.2019 | Ausstiegsdatum: 2019-01-01"
                            if i == 1 else
                            f"Quellzeile: {i + 4} | Ticker: CLOSED{i:02d} | Einstiegsdatum: {(datetime.date(2026, 2, 1) + datetime.timedelta(days=i)).strftime('%d.%m.%Y')} | Ausstiegsdatum: 2026-10-24"
                        )
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
                    "11. Methodische Hinweise und Datenqualität\n"
                    "11.1 Datenstatus\nMethodik-Inhalt muss erhalten bleiben.\n"
                    "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
                )
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(report, {"Offene Positionen+Check.csv": str(csv_path)})
                output_path = Path(output)
                self.assertEqual(output_path.name, f"Trading_Radar({datetime.date.today().isoformat()}).txt")
                self.assertTrue(output_path.is_file())
                saved = output_path.read_text(encoding="utf-8-sig")
                gate_spec = importlib.util.spec_from_file_location(
                    "trading_radar_gate_integration", ROOT / "trading_radar_report_gate.py"
                )
                gate_module = importlib.util.module_from_spec(gate_spec)
                gate_spec.loader.exec_module(gate_module)
                gate_errors = gate_module.validate_report(saved)
                tab2_errors = [
                    error for error in gate_errors
                    if "Tab-2" in error or "Geschlossene-Positionen-Abschnitt" in error
                ]
                self.assertEqual(tab2_errors, [], gate_errors)
                self.assertIn("OFFENE_POSITIONEN_QUELLANZAHL=2", saved)
                self.assertIn("OFFENE_POSITIONEN_ANZAHL=2", saved)
                self.assertIn("Alpha (AAA)", saved)
                self.assertIn("Beta (BBB)", saved)
                self.assertNotIn("Closed (CCC)", saved)
                self.assertIn("Ticker: OLD | Einstiegsdatum: 01.01.2019 | Ausstiegsdatum: 2019-01-01", saved)
                self.assertIn("Ticker: BADDATE | Einstiegsdatum: 02.01.2019 | Ausstiegsdatum: unbekannt", saved)
                closed_start = saved.index("10.5 Geschlossene Positionen")
                closed_end = saved.index("11. Methodische Hinweise und Datenqualität")
                closed_section = saved[closed_start:closed_end]
                self.assertIn("TAB-2-ANZAHL=51", closed_section)
                self.assertEqual(len(re.findall(r"(?m)^Quellzeile:\s+\d+\s+\|\s+Ticker:\s+\S+", closed_section)), 51)
                # Regression: alter Gemini-10.5-Inhalt muss vollständig weg,
                # der deterministische Quellenblock steht vor Abschnitt 11 und Status.
                self.assertNotIn("Ticker: STALE", saved)
                self.assertNotIn("alter Gemini-Platzhalter", saved)
                self.assertLess(saved.index("10.5 Geschlossene Positionen"), saved.index("11. Methodische Hinweise und Datenqualität"))
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


    def test_independent_radar_passes_loaded_tab2_block_to_report_writer(self):
        source = Path(__file__).resolve().parents[1] / "Trading_Radar_Alleinauswertung.py"
        text = source.read_text(encoding="utf-8")
        start = text.index("def _gemini_trading_radar_eigenstaendig(")
        end = text.index("\ndef gemini_auswertung_starten(", start)
        function = text[start:end]
        self.assertIn("global _GEMINI_GESCHLOSSENE_10_5_FINAL", function)
        self.assertIn(
            "_GEMINI_GESCHLOSSENE_10_5_FINAL = geschlossene_positionen_fakten or \"\"",
            function,
        )
        self.assertLess(
            function.index("_GEMINI_GESCHLOSSENE_10_5_FINAL = geschlossene_positionen_fakten or \"\""),
            function.index('prompt = """Du erstellst eine eigenstaendige Trading-Radar-Auswertung'),
        )


    def test_valid_tab2_overrides_model_non_final_marker_after_deterministic_validation(self):
        radar = load_radar_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                csv_path = Path(temp_dir) / "Offene Positionen+Check.csv"
                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, delimiter=";",
                        fieldnames=["Name", "Ticker", "Status", "Einstieg", "Einstiegsdatum", "Stop"],
                    )
                    writer.writeheader()
                    writer.writerow({
                        "Name": "Alpha", "Ticker": "AAA", "Status": "Offen",
                        "Einstieg": "10", "Einstiegsdatum": "01.01.2026", "Stop": "9",
                    })
                radar._GEMINI_TRADING_RADAR_ONLY_MODE = True
                radar._GEMINI_TRADING_RADAR_STATUS = "EIGENSTAENDIG_ERZEUGT"
                radar._GEMINI_GESCHlossene_10_5_FINAL = ""
                radar._GEMINI_GESCHLOSSENE_10_5_FINAL = (
                    "TAB-2-ANZAHL=1\nTAB-2-QUELLE=Geschlossene Positionen\n"
                    "Quellzeile: 3 | Ticker: AAA | Einstiegsdatum: 01.02.2026 | Status: Verkauft"
                )
                report = "\n".join(
                    f"{i}. {title}\n" + ("Ausreichend dokumentierter Abschnittsinhalt und Prüfkontext." * 2)
                    for i, title in enumerate([
                        "Trading-Radar", "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
                        "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang"
                    ], start=1)
                ) + "\nTRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
                with redirect_stdout(StringIO()):
                    output = radar.speichere_ergebnis(
                        report, {"Offene Positionen+Check.csv": str(csv_path)}
                    )
                saved = Path(output).read_text(encoding="utf-8-sig")
                self.assertIn("TAB-2-ANZAHL=1", saved)
                self.assertIn("TAB-2-QUELLE=Geschlossene Positionen", saved)
                self.assertRegex(saved, r"(?m)^TAB-2-EXPORT-SHA256=[0-9a-f]{64}$")
                self.assertTrue(saved.rstrip().endswith("TRADING_RADAR_FACHLICH_FINAL=FINAL"))
            finally:
                os.chdir(previous_cwd)



if __name__ == "__main__":
    unittest.main(verbosity=2)
