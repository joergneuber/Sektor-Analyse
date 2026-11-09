from __future__ import annotations

import ast
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "short_scanner.py"


def _load_calculation_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {
        "berechne_atr14",
        "get_swing_highs_above",
        "berechne_strukturellen_stop_short",
        "berechne_short_crv",
        "short_crv_erfuellt",
        "check_kumo_breakdown",
        "get_fib_levels_short",
        "get_swing_lows_below",
        "get_round_number_targets",
        "sammle_abwaerts_ziele",
        "waehle_short_ziele",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    found = {node.name for node in nodes}
    if found != names:
        raise AssertionError(f"Berechnungsfunktionen fehlen: {sorted(names - found)}")
    namespace = {
        "pd": pd,
        "np": np,
        "math": __import__("math"),
        "argrelextrema": argrelextrema,
        "FRISCHE_TAGE": 3,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class ShortScannerCalculationTests(unittest.TestCase):
    def setUp(self):
        self.functions = _load_calculation_functions()

    def test_stop_uses_nearest_confirmed_swing_high_plus_half_atr(self):
        frame = pd.DataFrame({
            "High": np.full(150, 102.0),
            "Low": np.full(150, 98.0),
            "Close": np.full(150, 100.0),
        })
        frame.loc[40, "High"] = 112.0
        frame.loc[70, "High"] = 108.0

        highs = self.functions["get_swing_highs_above"](frame, 100.0, order=5)
        stop = self.functions["berechne_strukturellen_stop_short"](
            frame, 100.0, atr_puffer=0.5, order=5
        )

        self.assertEqual(highs[:2], [108.0, 112.0])
        self.assertIsNotNone(stop)
        self.assertGreater(stop, 108.0)
        self.assertLess(stop, 112.0)

    def test_stop_fails_closed_without_confirmed_resistance(self):
        frame = pd.DataFrame({
            "High": np.linspace(101.0, 102.0, 80),
            "Low": np.linspace(98.0, 99.0, 80),
            "Close": np.linspace(99.0, 100.0, 80),
        })
        self.assertIsNone(
            self.functions["berechne_strukturellen_stop_short"](frame, 100.0)
        )

    def test_recent_unconfirmed_high_is_not_used_as_stop(self):
        frame = pd.DataFrame({
            "High": np.full(80, 102.0),
            "Low": np.full(80, 98.0),
            "Close": np.full(80, 100.0),
        })
        # Der Hoechstkurs liegt nur zwei Kerzen zurueck; bei order=5 fehlen
        # die fuenf rechten Bestaetigungskerzen fuer einen Pivot.
        frame.loc[77, "High"] = 115.0
        self.assertIsNone(
            self.functions["berechne_strukturellen_stop_short"](frame, 100.0)
        )

    def test_recent_unconfirmed_low_is_not_used_as_target(self):
        lows = np.linspace(90.0, 100.0, 80)
        lows[77] = 70.0
        frame = pd.DataFrame({"Low": lows})
        self.assertEqual(
            self.functions["get_swing_lows_below"](frame, 100.0),
            [],
        )

    def test_atr_is_positive_and_requires_sufficient_history(self):
        frame = pd.DataFrame({
            "High": np.full(40, 102.0),
            "Low": np.full(40, 98.0),
            "Close": np.full(40, 100.0),
        })
        atr = self.functions["berechne_atr14"](frame)
        self.assertGreater(atr, 0)
        self.assertIsNone(self.functions["berechne_atr14"](frame.iloc[:10]))

    def test_kumo_breakdown_requires_previous_close_above_entire_cloud(self):
        frame = pd.DataFrame({
            "Close": [10.5, 10.5, 10.5, 10.5, 10.5, 9.5],
            "SenkouA": [10.0] * 6,
            "SenkouB": [11.0] * 6,
            "Vol_Ratio": [1.2] * 6,
        })
        result_inside_cloud = self.functions["check_kumo_breakdown"](frame)
        self.assertFalse(result_inside_cloud[0])

        frame.loc[4, "Close"] = 11.5
        result_from_above_cloud = self.functions["check_kumo_breakdown"](frame)
        self.assertTrue(result_from_above_cloud[0])

    def test_short_crv_uses_unrounded_values_and_validates_geometry(self):
        # CRV1 liegt roh unter 1, wuerde auf zwei Stellen aber 1.00 ergeben.
        values = self.functions["berechne_short_crv"](
            entry=100.0, stop=110.0, tp1=90.04, tp2=89.0
        )
        self.assertIsNotNone(values)
        self.assertAlmostEqual(values[0], 0.996)
        self.assertLessEqual(values[0], 1.0)
        self.assertGreater(values[1], 1.0)
        self.assertFalse(self.functions["short_crv_erfuellt"](*values))
        self.assertFalse(self.functions["short_crv_erfuellt"](1.0, 1.5))
        self.assertTrue(self.functions["short_crv_erfuellt"](1.00001, 1.5))

        self.assertIsNone(
            self.functions["berechne_short_crv"](100.0, 110.0, 101.0, 90.0)
        )
        self.assertIsNone(
            self.functions["berechne_short_crv"](100.0, 99.0, 90.0, 80.0)
        )

    def test_tp_selection_requires_two_real_targets(self):
        namespace = self.functions
        namespace["sammle_abwaerts_ziele"] = lambda data, entry: [95.0]
        self.assertIsNone(namespace["waehle_short_ziele"](None, 100.0))

        namespace["sammle_abwaerts_ziele"] = lambda data, entry: [95.0, 90.0]
        self.assertEqual(
            namespace["waehle_short_ziele"](None, 100.0),
            (95.0, 90.0),
        )

    def test_target_collection_never_returns_nonpositive_prices(self):
        frame = pd.DataFrame({
            "High": np.full(120, 55.0),
            "Low": np.full(120, 45.0),
            "Close": np.full(120, 50.0),
            "SenkouA": np.full(120, 46.0),
            "SenkouB": np.full(120, 47.0),
            "EMA20": np.full(120, 48.0),
            "EMA50": np.full(120, 47.5),
            "EMA100": np.full(120, 47.0),
            "EMA200": np.full(120, 46.5),
            "WMA200": np.full(120, 46.0),
        })
        targets = self.functions["sammle_abwaerts_ziele"](frame, 50.0)
        self.assertTrue(targets)
        self.assertTrue(all(0 < target < 50.0 for target in targets))
        self.assertEqual(targets, sorted(targets, reverse=True))


if __name__ == "__main__":
    unittest.main()
