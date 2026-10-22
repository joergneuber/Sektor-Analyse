"""Regressionstests fuer WTI/Brent-Perioden im Marktbriefing."""
import ast
import datetime
import unittest
import pandas as pd
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "analyse.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NODE = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "get_rohstoff_performance_yf")

class WTIPerformanceTests(unittest.TestCase):
    def setUp(self):
        # 400 tägliche Datenpunkte mit linear steigendem Schlusskurs.
        dates = pd.date_range("2025-01-01", periods=400, freq="B")
        self.hist = pd.DataFrame({"Close": range(100, 500)}, index=dates)
        self.env = {"pd": pd, "datetime": datetime,
                   "_hole_kursdaten_gecached": lambda ticker: self.hist.copy(),
                   "_nur_abgeschlossene_tagesbalken": lambda hist, ticker: (hist, hist.index[-1].date())}
        exec(compile(ast.Module(body=[NODE], type_ignores=[]), str(SOURCE), "exec"), self.env)

    def test_output_contains_all_periods_and_source_date(self):
        line = self.env["get_rohstoff_performance_yf"]("CL=F", "WTI")
        for token in ("5T", "4W", "1M", "3M", "6M", "1J", "Quelle=yfinance/CL=F", "Datenstand"):
            self.assertIn(token, line)

    def test_four_weeks_and_one_month_are_separately_computed(self):
        line = self.env["get_rohstoff_performance_yf"]("CL=F", "WTI")
        import re
        vals = {m.group(2): m.group(1) for m in re.finditer(r"([-+]?\d+[,.]\d+)%\s*(4W|1M)", line)}
        self.assertIn("4W", vals)
        self.assertIn("1M", vals)
        # Different lookback boundaries on this fixture should not be aliased.
        self.assertNotEqual(vals["4W"], vals["1M"])

    def test_missing_history_is_explicit(self):
        self.env["_hole_kursdaten_gecached"] = lambda ticker: pd.DataFrame()
        line = self.env["get_rohstoff_performance_yf"]("CL=F", "WTI")
        self.assertIn("nicht verfügbar", line)
        self.assertIn("yfinance/CL=F", line)

    def test_none_history_is_explicit(self):
        self.env["_hole_kursdaten_gecached"] = lambda ticker: None
        line = self.env["get_rohstoff_performance_yf"]("BZ=F", "Brent")
        self.assertIn("nicht verfügbar", line)
        self.assertIn("yfinance/BZ=F", line)

    def test_missing_close_column_is_explicit(self):
        self.env["_hole_kursdaten_gecached"] = lambda ticker: pd.DataFrame({"Open": [1.0]})
        line = self.env["get_rohstoff_performance_yf"]("CL=F", "WTI")
        self.assertIn("nicht verfügbar", line)
        self.assertIn("yfinance/CL=F", line)

    def test_five_trading_day_return_uses_sixth_close(self):
        # Letzter Schluss 500; Schluss vor fünf Handelssitzungen 494.
        line = self.env["get_rohstoff_performance_yf"]("CL=F", "WTI")
        import re
        match = re.search(r"([-+]?[0-9]+,[0-9]{2})%\s*5T", line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "+1,01")

if __name__ == "__main__":
    unittest.main()
