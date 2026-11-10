from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYSE = ROOT / "analyse.py"
DELISTED_TICKERS = {"ANSS", "JNPR", "HES", "MRO"}


def _load_sector_universe() -> dict[str, list[str]]:
    tree = ast.parse(ANALYSE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "sektoren_aktien" for target in node.targets):
                value = ast.literal_eval(node.value)
                if not isinstance(value, dict):
                    raise AssertionError("sektoren_aktien muss ein Dictionary sein")
                return value
    raise AssertionError("sektoren_aktien wurde in analyse.py nicht gefunden")


class ShortScannerUniverseTickerTests(unittest.TestCase):
    def test_acquired_and_delisted_us_tickers_are_not_in_active_universe(self):
        universe = _load_sector_universe()
        active_tickers = {ticker for tickers in universe.values() for ticker in tickers}
        self.assertTrue(DELISTED_TICKERS.isdisjoint(active_tickers))

    def test_sector_universe_remains_nonempty_after_delisted_ticker_cleanup(self):
        universe = _load_sector_universe()
        self.assertTrue(universe)
        self.assertTrue(all(isinstance(tickers, list) and tickers for tickers in universe.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
