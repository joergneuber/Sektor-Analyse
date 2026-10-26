import ast
import datetime
import json
import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "Trading_Radar_Alleinauswertung.py"

EXPECTED_EXCLUSIONS = (
    "10O.DE", "1TRA.DE", "ABB", "ANSS", "ARND.DE", "ASTA.DE", "AUM.DE",
    "CMA", "DFS", "FLIR", "FRAG.DE", "HES", "JNPR", "KCOG.DE", "MAXN",
    "MRO", "NAFG.DE", "PNE.DE", "PSAG.DE", "RTL.DE", "SHA.DE", "SHELL.SO",
    "SNV", "STM1.DE", "WRK", "X",
)


def load_normalizer():
    source = GENERATOR.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(GENERATOR))
    nodes = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name in {
            "_trading_radar_berichtsfelder_normalisieren",
            "_trading_radar_aktive_firmennamen_luecken",
            "_trading_radar_nicht_handhabbare_kandidaten_normalisieren",
            "_trading_radar_name_ticker_normalisieren",
            "_trading_radar_numeric_wert_gueltig",
            "_parse_tab2_entry_date_text",
        }
    ]
    namespace = {"re": re, "datetime": datetime, "json": json, "os": os, "__file__": str(GENERATOR)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(GENERATOR), "exec"), namespace)
    return namespace


class TradingRadarReportFormattingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers = load_normalizer()
        cls.normalize = staticmethod(helpers["_trading_radar_berichtsfelder_normalisieren"])
        cls.filter_active_missing_names = staticmethod(
            helpers["_trading_radar_aktive_firmennamen_luecken"]
        )
        cls.normalize_unactionable_candidates = staticmethod(
            helpers["_trading_radar_nicht_handhabbare_kandidaten_normalisieren"]
        )
        cls.normalize_name_ticker = staticmethod(
            helpers["_trading_radar_name_ticker_normalisieren"]
        )
        cls.parse_tab2_entry_date = staticmethod(helpers["_parse_tab2_entry_date_text"])
        cls.numeric_value_valid = staticmethod(helpers["_trading_radar_numeric_wert_gueltig"])

    def test_adds_real_blank_line_at_both_report_section_transitions(self):
        report = (
            "3. Ereignisradar\nInhalt 3\n"
            "4. Assetklassenvergleich\nInhalt 4\n"
            "8. Daten-/Prüfanhang\nInhalt 8\n"
            "9. Vollständige offene Positionen\nInhalt 9\n"
        )
        result = self.normalize(report)
        self.assertRegex(result, r"(?s)Inhalt 3.*\n\n4\. Assetklassenvergleich")
        self.assertRegex(result, r"(?s)Inhalt 8.*\n\n9\. Vollständige offene Positionen")
        self.assertNotIn(r"\n4. Assetklassenvergleich", result)
        self.assertNotIn(r"\n9. Vollständige offene Positionen", result)

    def test_report_output_lists_all_26_excluded_tickers(self):
        result = self.normalize("8. Daten-/Prüfanhang\nDatenprüfung abgeschlossen\n")
        heading = "Bewusst ausgeschlossene Ticker (nicht Teil des aktiven Datenuniversums):"
        self.assertIn(heading, result)
        for ticker in EXPECTED_EXCLUSIONS:
            with self.subTest(ticker=ticker):
                self.assertIn(ticker, result)
        self.assertEqual(len(EXPECTED_EXCLUSIONS), 26)
        self.assertEqual(result.count(heading), 1)

    def test_unavailable_candidate_levels_are_not_published_as_actionable(self):
        report = (
            "5. Technische Kandidaten\n"
            "* Asset/Ticker: XEL\n"
            "  Trigger: Hebeltrader-Setup / Einzel-Check KAUFKANDIDAT A\n"
            "  Einstieg: NICHT VERFÜGBAR\n"
            "  Stop: NICHT VERFÜGBAR\n"
            "  TP1: NICHT VERFÜGBAR\n"
            "  TP2: NICHT VERFÜGBAR\n"
            "  CRV: NICHT VERFÜGBAR\n"
            "  Gegenargument: schwache relative Stärke\n"
            "  Regel-/Datenstatus: KAUFKANDIDAT A\n\n"
            "6. Portfolio & Risiko\nPortfolio-Informationen\n"
        )
        result = self.normalize_unactionable_candidates(report)
        self.assertIn("Keine belastbaren technischen Kandidaten; keine Handelsfreigabe.", result)
        self.assertNotIn("KAUFKANDIDAT A", result)
        self.assertNotIn("Asset/Ticker: XEL", result)

    def test_mixed_candidate_list_drops_only_candidates_with_unavailable_levels(self):
        report = (
            "5. Technische Kandidaten\n"
            "* Asset/Ticker: AAA\nTrigger: Breakout\nEinstieg: 100\n"
            "Stop: 95\nTP1: 110\nTP2: 120\nCRV: 2\n"
            "Gegenargument: Risiko\nRegelstatus: JETZT TECHNISCH PRÜFEN\n\n"
            "* Asset/Ticker: BBB\nTrigger: Einzel-Check\nEinstieg: NICHT VERFÜGBAR\n"
            "Stop: NICHT VERFÜGBAR\nTP1: NICHT VERFÜGBAR\nTP2: NICHT VERFÜGBAR\n"
            "CRV: NICHT VERFÜGBAR\nGegenargument: Daten fehlen\n"
            "Regelstatus: KAUFKANDIDAT A\n\n"
            "6. Portfolio & Risiko\nPortfolio-Informationen\n"
        )
        result = self.normalize_unactionable_candidates(report)
        self.assertIn("Asset/Ticker: AAA", result)
        self.assertNotIn("Asset/Ticker: BBB", result)
        self.assertNotIn("KAUFKANDIDAT A", result)

    def test_numeric_candidate_levels_are_preserved(self):
        report = (
            "5. Technische Kandidaten\n"
            "* Asset/Ticker: AAA\nTrigger: Breakout\nEinstieg: 100\n"
            "Stop: 95\nTP1: 110\nTP2: 120\nCRV: 2\n"
            "Gegenargument: Risiko\nRegelstatus: JETZT TECHNISCH PRÜFEN\n\n"
            "6. Portfolio & Risiko\nPortfolio-Informationen\n"
        )
        self.assertIn("Asset/Ticker: AAA", self.normalize_unactionable_candidates(report))

    def test_candidate_numeric_validator_accepts_common_units_but_not_prose(self):
        for value in ("100", "1.234,50 EUR", "USD 100.25", "95 €", "2.5x", "110%"):
            with self.subTest(value=value):
                self.assertTrue(self.numeric_value_valid(value))
        for value in ("NICHT VERFÜGBAR", "bald 100", "ca. 100 USD", ""):
            with self.subTest(value=value):
                self.assertFalse(self.numeric_value_valid(value))

    def test_name_ticker_normalizer_expands_known_tickers_and_preserves_tab2_contract(self):
        report = (
            "1. Trading-Radar\n"
            "Siemens Energy AG (ENR.DE) ist im Portfolio; ENR.DE beobachten.\n"
            "10.5 Geschlossene Positionen\n"
            "Quellzeile: 3 | Ticker: MDLZ | Name: Mondelez International, Inc. | "
            "Einstiegsdatum: 17.07.2026 | Status: Verkauft\n"
            "TAB-2-EXPORT-SHA256=abc\n"
        )
        result = self.normalize_name_ticker(report)
        self.assertIn("Siemens Energy AG (ENR.DE) ist im Portfolio; Siemens Energy AG (ENR.DE) beobachten.", result)
        original_tab2 = (
            "10.5 Geschlossene Positionen\n"
            "Quellzeile: 3 | Ticker: MDLZ | Name: Mondelez International, Inc. | "
            "Einstiegsdatum: 17.07.2026 | Status: Verkauft\n"
            "TAB-2-EXPORT-SHA256=abc\n"
        )
        self.assertIn(original_tab2, result)
        self.assertNotIn("Asset: Mondelez International, Inc. (MDLZ)", result)

    def test_name_ticker_normalizer_expands_known_market_instruments(self):
        report = "1. Trading-Radar\nGold GC=F und Silber SI=F; Bitcoin BTC-USD; WTI und Brent.\n"
        result = self.normalize_name_ticker(report)
        self.assertIn("Gold Future (GC=F)", result)
        self.assertIn("Silber Future (SI=F)", result)
        self.assertIn("Bitcoin USD (BTC-USD)", result)
        self.assertIn("WTI Crude Oil Futures (CL=F)", result)
        self.assertIn("Brent Crude Oil Futures (BZ=F)", result)

    def test_name_ticker_normalizer_marks_unresolved_tickers(self):
        report = (
            "1. Trading-Radar\n"
            "XYZ_UNKNOWN und QXZQ sind im Bericht relevant; FOMC und MSCI sind Kontextbegriffe.\n"
        )
        result = self.normalize_name_ticker(report)
        self.assertIn("Name nicht aufgelöst (XYZ_UNKNOWN)", result)
        self.assertIn("Name nicht aufgelöst (QXZQ)", result)
        self.assertIn("FOMC und MSCI", result)
        self.assertNotIn("Name nicht aufgelöst (FOMC)", result)
        self.assertNotIn("Name nicht aufgelöst (MSCI)", result)

    def test_name_ticker_normalizer_does_not_mutate_structured_values(self):
        report = (
            "1. Trading-Radar\nGold und ENR.DE beobachten.\n"
            "Aktueller Kurs: 100 Gold\n"
            "Ticker: ENR.DE | Name: Siemens Energy AG | Aktueller Kurs: 100\n"
            "Bewusst ausgeschlossene Ticker (nicht aktiv): SHELL.SO, STM1.DE\n"
            "10.5 Geschlossene Positionen\n"
            "Quellzeile: 3 | Ticker: MDLZ | Name: Mondelez International, Inc. | Status: Verkauft\n"
            "TAB-2-EXPORT-SHA256=abc\n"
        )
        result = self.normalize_name_ticker(report)
        self.assertIn("Gold Future (GC=F) und Siemens Energy AG (ENR.DE) beobachten.", result)
        self.assertIn("Aktueller Kurs: 100 Gold", result)
        self.assertIn("Ticker: ENR.DE | Name: Siemens Energy AG | Aktueller Kurs: 100", result)
        self.assertIn("Bewusst ausgeschlossene Ticker (nicht aktiv): SHELL.SO, STM1.DE", result)
        original_tab2 = (
            "10.5 Geschlossene Positionen\n"
            "Quellzeile: 3 | Ticker: MDLZ | Name: Mondelez International, Inc. | Status: Verkauft\n"
            "TAB-2-EXPORT-SHA256=abc\n"
        )
        self.assertIn(original_tab2, result)
        self.assertIn("TAB-2-EXPORT-SHA256=abc", result)

    def test_generator_rejects_nonempty_invalid_tab2_entry_date(self):
        self.assertIsNone(self.parse_tab2_entry_date("31.02.2026"))
        self.assertIsNone(self.parse_tab2_entry_date("2026-13-01"))
        self.assertEqual(
            self.parse_tab2_entry_date("26.10.2026").isoformat(),
            "2026-10-26",
        )

    def test_excluded_tickers_are_not_reported_as_active_name_resolution_failures(self):
        cache = {
            ticker: {"missing": True}
            for ticker in EXPECTED_EXCLUSIONS
        }
        cache.update({
            "ACTIVE1": {"missing": True},
            "ACTIVE2": {"missing": True},
            "KNOWN": {"missing": False},
        })
        result = self.filter_active_missing_names(cache, EXPECTED_EXCLUSIONS)
        self.assertEqual(result, ["ACTIVE1", "ACTIVE2"])
        for ticker in EXPECTED_EXCLUSIONS:
            with self.subTest(ticker=ticker):
                self.assertNotIn(ticker, result)


    def test_name_ticker_normalizer_handles_plain_tickers_without_underscores(self):
        report = (
            "1. Trading-Radar\n"
            "Albemarle Corporation (ALB) und Alibaba Group (BABA) bleiben im Fokus; "
            "BABA beobachten.\n"
        )
        result = self.normalize_name_ticker(report)
        self.assertIn("Albemarle Corporation (ALB)", result)
        self.assertIn("Alibaba Group (BABA) bleiben im Fokus", result)
        self.assertIn("Alibaba Group (BABA) beobachten", result)

    @unittest.skipUnless(os.environ.get("TRADING_RADAR_HISTORICAL_REPORT"), "historischer Bericht nicht konfiguriert")
    def test_full_historical_report_preserves_structured_invariants_independently(self):
        report_path = Path(os.environ["TRADING_RADAR_HISTORICAL_REPORT"])
        report = report_path.read_text(encoding="utf-8")
        result = self.normalize_name_ticker(report)

        # Independent source-to-output checks: the whole Tab-2 payload, from its
        # heading through the checksum line, must be byte-for-byte identical.
        heading = "10.5 Geschlossene Positionen"
        checksum_prefix = "TAB-2-EXPORT-SHA256="
        source_start = report.index(heading)
        output_start = result.index(heading)
        source_hash_start = report.index(checksum_prefix, source_start)
        output_hash_start = result.index(checksum_prefix, output_start)
        source_end = report.find("\n", source_hash_start)
        output_end = result.find("\n", output_hash_start)
        source_end = len(report) if source_end < 0 else source_end + 1
        output_end = len(result) if output_end < 0 else output_end + 1
        self.assertEqual(report[source_start:source_end], result[output_start:output_end])

        # Required report invariants are checked independently of normalizer output.
        invariants = (
            "OFFENE_POSITIONEN_QUELLANZAHL=14",
            "OFFENE_POSITIONEN_ANZAHL=14",
            "TAB-2-ANZAHL=51",
            "TAB-2-EXPORT-SHA256=847ba1d593ac488399c2ba005073ad9d95bae7388fce5238102317c90ec7d104",
            "Alibaba Group Holding Limited (BABA) | Einstieg: 141,0",
            "Albemarle Corporation (ALB) | Einstieg: 200,0",
            "Bewusst ausgeschlossene Ticker (nicht Teil des aktiven Datenuniversums):",
        )
        for invariant in invariants:
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, result)

        # Every source Tab-2 record and the hash must survive exactly once.
        source_rows = [line for line in report[source_start:source_end].splitlines()
                       if line.startswith("Quellzeile:")]
        output_rows = [line for line in result[output_start:output_end].splitlines()
                       if line.startswith("Quellzeile:")]
        self.assertEqual(len(source_rows), 51)
        self.assertEqual(source_rows, output_rows)
        self.assertEqual(result.count(checksum_prefix), report.count(checksum_prefix))



if __name__ == "__main__":
    unittest.main()
