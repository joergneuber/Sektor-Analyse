from __future__ import annotations

import ast
from pathlib import Path
import unittest

try:
    from alpaca.data.enums import Adjustment
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    ALPACA_SDK_AVAILABLE = True
except ImportError:
    Adjustment = StockBarsRequest = TimeFrame = TimeFrameUnit = None
    ALPACA_SDK_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_FILES = (
    "analyse.py",
    "market_data.py",
    "short_scanner.py",
    "positionen_tracker.py",
)


class AlpacaSplitAdjustmentContractTests(unittest.TestCase):
    @unittest.skipUnless(ALPACA_SDK_AVAILABLE, "alpaca-py is not installed in this local environment")
    def test_sdk_exposes_split_adjustment_and_request_accepts_it(self):
        self.assertEqual(Adjustment.SPLIT.value, "split")
        # Other test modules stub alpaca.data.timeframe.TimeFrame after collection.
        # TimeFrame.Day resolves that module global and can therefore hit the stub.
        # Constructing the preset explicitly uses the real class imported above
        # while preserving the same one-day timeframe contract.
        daily_timeframe = TimeFrame(amount=1, unit=TimeFrameUnit.Day)
        request = StockBarsRequest(
            symbol_or_symbols=["AAPL"],
            timeframe=daily_timeframe,
            adjustment=Adjustment.SPLIT,
        )
        self.assertEqual(request.adjustment, Adjustment.SPLIT)
        self.assertEqual(request.timeframe.amount, 1)
        self.assertEqual(request.timeframe.unit, TimeFrameUnit.Day)

    def test_every_productive_stock_bars_request_is_explicitly_split_adjusted(self):
        checked = 0
        for relative_path in PRODUCTION_FILES:
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative_path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_stock_bars = (
                    isinstance(func, ast.Name) and func.id == "StockBarsRequest"
                ) or (
                    isinstance(func, ast.Attribute) and func.attr == "StockBarsRequest"
                )
                if not is_stock_bars:
                    continue
                checked += 1
                adjustment = next(
                    (kw.value for kw in node.keywords if kw.arg == "adjustment"), None
                )
                self.assertIsNotNone(
                    adjustment, f"{relative_path}:{node.lineno}: adjustment missing"
                )
                self.assertIsInstance(adjustment, ast.Attribute)
                self.assertEqual(adjustment.attr, "SPLIT", f"{relative_path}:{node.lineno}")
                self.assertIsInstance(adjustment.value, ast.Name)
                self.assertEqual(adjustment.value.id, "Adjustment")
        self.assertEqual(checked, 7, "Unexpected change in productive StockBarsRequest inventory")

    def test_repository_stock_bars_request_inventory_is_complete_and_classified(self):
        expected = {
            "analyse.py", "market_data.py", "short_scanner.py",
            "positionen_tracker.py", "provider_coverage_us.py",
            "diagnose_us_split_adjustment.py",
        }
        found = set()
        count = 0
        for path in ROOT.rglob("*.py"):
            relative = path.relative_to(ROOT)
            if relative.parts[0] == "tests" or any(
                part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts
            ):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Name) and func.id == "StockBarsRequest") or (
                    isinstance(func, ast.Attribute) and func.attr == "StockBarsRequest"
                ):
                    found.add(path.relative_to(ROOT).as_posix())
                    count += 1
        self.assertEqual(found, expected)
        self.assertEqual(count, 10)

    def test_diagnostic_provider_paths_keep_their_explicit_conventions(self):
        provider = (ROOT / "provider_coverage_us.py").read_text(encoding="utf-8")
        diagnostic = (ROOT / "diagnose_us_split_adjustment.py").read_text(encoding="utf-8")
        self.assertEqual(provider.count("adjustment=Adjustment.RAW"), 2)
        self.assertIn("adjustment=Adjustment.SPLIT", diagnostic)

    def test_spy_cache_key_is_versioned_for_split_adjusted_data(self):
        source = (ROOT / "analyse.py").read_text(encoding="utf-8")
        self.assertIn("alpaca:SPY:close:ohlcv-quality-v1.4-split", source)
        self.assertNotIn("alpaca:SPY:close:ohlcv-quality-v1.3", source)

    def test_every_workflow_installs_the_same_pinned_alpaca_sdk(self):
        workflows = ROOT / ".github" / "workflows"
        for path in workflows.glob("*.yml"):
            source = path.read_text(encoding="utf-8")
            if "alpaca-py" not in source:
                continue
            self.assertIn(
                "alpaca-py==0.45.0", source,
                f"{path.relative_to(ROOT)} must pin the Alpaca SDK",
            )
            self.assertNotIn("pip install alpaca-py ", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
