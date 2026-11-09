from __future__ import annotations

import ast
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "short_scanner.py"


def load_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted = {"get_swing_high_candidates", "get_swing_highs_above"}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    found = {node.name for node in nodes}
    if found != wanted:
        raise AssertionError(f"Missing functions: {sorted(wanted - found)}")
    namespace = {
        "pd": pd,
        "np": np,
        "argrelextrema": argrelextrema,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class ShortScannerSwingHighDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.helpers = load_helpers()

    def test_all_confirmed_pivots_and_selected_lowest_eligible_pivot(self):
        index = pd.date_range("2026-01-01", periods=140, freq="B")
        highs = np.full(140, 90.0)
        # Outside the 120-session window; must not be included.
        highs[10] = 150.0
        # Confirmed pivots inside the window, including a duplicate low price.
        highs[30] = 115.0
        highs[50] = 108.0
        highs[80] = 108.0
        highs[100] = 96.0  # below entry: diagnostic only, not stop-eligible
        highs[114] = 120.0
        # Inside the window but lacking five left / right bars respectively.
        highs[22] = 160.0
        highs[137] = 170.0
        frame = pd.DataFrame({"High": highs}, index=index)

        candidates = self.helpers["get_swing_high_candidates"](frame, 100.0)
        self.assertEqual(len(candidates), 5)
        self.assertEqual(
            [item["price"] for item in candidates],
            [96.0, 108.0, 108.0, 115.0, 120.0],
        )
        self.assertEqual(
            [item["pivot_date"] for item in candidates],
            [index[100].date().isoformat(), index[50].date().isoformat(),
             index[80].date().isoformat(), index[30].date().isoformat(),
             index[114].date().isoformat()],
        )
        self.assertFalse(candidates[0]["eligible_for_stop"])
        selected = [item for item in candidates if item["selected"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["price"], 108.0)
        self.assertEqual(selected[0]["pivot_date"], index[50].date().isoformat())

        # The existing production stop-selection interface remains price-only
        # and must select the same lowest eligible level.
        existing_prices = self.helpers["get_swing_highs_above"](frame, 100.0)
        self.assertEqual(existing_prices, [108.0, 115.0, 120.0])
        self.assertEqual(existing_prices[0], selected[0]["price"])

    def test_crv_diagnostic_wires_pivot_date_and_full_candidate_list(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        target = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_pruefe_short_setup"
        )
        calls = {
            node.func.id
            for node in ast.walk(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        string_values = {
            node.value for node in ast.walk(target)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("get_swing_high_candidates", calls)
        self.assertTrue(any("SwingHigh-Pivot-Datum=" in value for value in string_values))
        self.assertTrue(any("SwingHigh-Kandidaten-120S=" in value for value in string_values))

    def test_insufficient_history_returns_no_confirmed_pivots(self):
        frame = pd.DataFrame(
            {"High": np.arange(10, dtype=float)},
            index=pd.date_range("2026-01-01", periods=10, freq="B"),
        )
        self.assertEqual(
            self.helpers["get_swing_high_candidates"](frame, 5.0), []
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
