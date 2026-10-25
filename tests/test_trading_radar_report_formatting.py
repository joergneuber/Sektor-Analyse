import ast
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
        }
    ]
    namespace = {"re": re}
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
