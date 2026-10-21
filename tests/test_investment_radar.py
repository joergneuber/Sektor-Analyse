import json
import tempfile
import unittest
from pathlib import Path
import investment_radar

class InvestmentRadarReportTests(unittest.TestCase):
    def test_company_label_name_ticker(self):
        self.assertEqual(investment_radar.company_label({"name":"Example Corp","ticker":"EXM"}),"Example Corp (EXM)")

    def test_report_shows_all_candidates_without_top_n_cap(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"Trade_Story_Universum(2026-10-21).json"
            candidates=[{"name":f"Company {i}","ticker":f"T{i}","trade_story_status":"VALIDE SETUP","Setup_Typ":"Trend"} for i in range(14)]
            p.write_text(json.dumps({"candidates":candidates}),encoding="utf-8")
            report=investment_radar.build_report(Path(d))
            self.assertIn("Company 13 (T13)",report)
            self.assertIn("Company 0 (T0)",report)
            self.assertEqual(report.count("VALIDE SETUP"),14)

if __name__=="__main__": unittest.main()
