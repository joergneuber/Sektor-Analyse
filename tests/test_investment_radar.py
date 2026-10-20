import unittest
import investment_radar


class InvestmentRadarLinkTests(unittest.TestCase):
    def test_rule_overlap_uses_only_present_sector_data(self):
        macro = "CPI: 3.2 | STATUS=REAL\nISM Manufacturing PMI: 51.0 | STATUS=REAL"
        perf = [{"Ticker": "XLB", "Sektor": "Rohstoffe", "30T": "2.0"}]
        setups = [{"Ticker": "NUE", "Sektor": "Rohstoffe", "Status2": "VALIDE"}]
        story = [{"ticker": "NUE", "Sektor": "Rohstoffe"}]
        found = investment_radar.macro_hypotheses(macro, perf, [], setups, story)
        self.assertTrue(found)
        self.assertTrue(all(item["sector"] == "Rohstoffe" for item in found))
        self.assertTrue(any(item["setup_tickers"] == ["NUE"] for item in found))
        self.assertTrue(any(item["performance"][0]["ticker"] == "XLB" for item in found))

    def test_no_macro_evidence_means_no_hypothesis(self):
        found = investment_radar.macro_hypotheses(
            "Keine passenden Makrozeilen", [{"Ticker": "XLE", "Sektor": "Energie"}],
            [], [], [])
        self.assertEqual(found, [])

    def test_no_sector_overlap_means_no_hypothesis(self):
        found = investment_radar.macro_hypotheses(
            "CPI: 3.2 | STATUS=REAL", [], [], [], [])
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
