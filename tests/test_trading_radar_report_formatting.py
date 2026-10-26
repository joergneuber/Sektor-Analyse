import ast
import datetime
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
            "_trading_radar_numeric_wert_gueltig",
            "_parse_tab2_entry_date_text",
        }
    ]
    namespace = {"re": re, "datetime": datetime}
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


if __name__ == "__main__":
    unittest.main()
