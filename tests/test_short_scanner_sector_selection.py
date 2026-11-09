from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "short_scanner.py"


def _load_selection_functions():
    """Load only the pure sector-selection functions, without importing API clients."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"_bereite_rotation_scores_vor", "bestimme_bottom_sektoren"}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in nodes} != names:
        raise AssertionError("Short-Scanner sector-selection functions are incomplete.")

    namespace = {
        "pd": pd,
        "np": np,
        "BOTTOM_SEKTOREN_US": 2,
        "BOTTOM_SEKTOREN_EU": 1,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class ShortScannerSectorSelectionTests(unittest.TestCase):
    def setUp(self):
        self.functions = _load_selection_functions()

    def test_score_preparation_coerces_numeric_values_and_excludes_invalid_values(self):
        frame = pd.DataFrame({
            "Sektor": ["A", "B", "C", "D", "E"],
            "Rotation-Score": [2.0, "-1.5", None, "ungueltig", float("inf")],
        })

        prepared, valid = self.functions["_bereite_rotation_scores_vor"](
            frame, "TEST", 3
        )

        self.assertEqual(set(valid["Sektor"]), {"A", "B"})
        self.assertEqual(valid["Rotation-Score"].dtype.kind, "f")
        self.assertTrue(pd.isna(prepared.loc[2, "Rotation-Score"]))
        self.assertTrue(pd.isna(prepared.loc[3, "Rotation-Score"]))
        self.assertTrue(pd.isna(prepared.loc[4, "Rotation-Score"]))

    def test_nullable_numeric_dtype_with_pd_na_is_supported(self):
        frame = pd.DataFrame({
            "Sektor": ["A", "B"],
            "Rotation-Score": pd.Series([1.25, pd.NA], dtype="Float64"),
        })

        _, valid = self.functions["_bereite_rotation_scores_vor"](frame, "TEST", 1)

        self.assertEqual(valid["Sektor"].tolist(), ["A"])
        self.assertEqual(float(valid["Rotation-Score"].iloc[0]), 1.25)

    def test_all_invalid_scores_fail_closed_with_diagnostic(self):
        frame = pd.DataFrame({
            "Sektor": ["A", "B"],
            "Rotation-Score": [None, "ungueltig"],
        })

        with self.assertRaisesRegex(RuntimeError, "keine gueltigen numerischen Rotation-Scores"):
            self.functions["_bereite_rotation_scores_vor"](frame, "US", 2)

    def test_bottom_selection_uses_weakest_valid_scores_only(self):
        namespace = self.functions
        namespace.update({
            "sektoren_map": {
                "US1": "US One", "US2": "US Two", "US3": "US Three"
            },
            "eu_sektoren_etf": {"EU1": "EU One", "EU2": "EU Two"},
            "get_perf": lambda ticker, name: {
                "Sektor": name, "5T": 1.0, "12T": 2.0,
                "Rotation-Score": {
                    "US One": 3.0, "US Two": -2.0, "US Three": 1.0
                }[name],
            },
            "get_perf_yf": lambda ticker, name: {
                "Sektor": name, "5T": 1.0, "12T": 2.0,
                "Rotation-Score": {"EU One": -1.0, "EU Two": None}[name],
            },
        })

        with patch("builtins.print"):
            bottom_us, bottom_eu, momentum_us, momentum_eu = namespace[
                "bestimme_bottom_sektoren"
            ]()

        self.assertEqual(bottom_us, ["US Two", "US Three"])
        self.assertEqual(bottom_eu, ["EU One"])
        self.assertTrue(pd.isna(momentum_eu["EU Two"]["Rotation-Score"]))

    def test_missing_required_columns_fail_closed(self):
        frame = pd.DataFrame({"Sektor": ["A"]})

        with self.assertRaisesRegex(RuntimeError, "Pflichtspalten"):
            self.functions["_bereite_rotation_scores_vor"](frame, "EU", 1)


if __name__ == "__main__":
    unittest.main()
