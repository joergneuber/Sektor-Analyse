from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import types

import pandas as pd

MODULE_PATH = Path(__file__).resolve().parents[1] / "provider_coverage_us.py"
SPEC = importlib.util.spec_from_file_location("provider_coverage_us", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


class ProviderCoverageHelpersTests(unittest.TestCase):
    def test_frame_dates_normalize_timezone_to_utc_date(self):
        frame = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.DatetimeIndex([
            "2026-11-06 23:00:00+00:00", "2026-11-07 01:00:00+00:00"
        ]))
        self.assertEqual(module._frame_dates(frame), ["2026-11-06", "2026-11-07"])

    def test_normalise_alpaca_multiindex_frames_keeps_project_symbol(self):
        index = pd.MultiIndex.from_product(
            [["BRK.B", "AAPL"], pd.to_datetime(["2026-11-05", "2026-11-06"])],
            names=["symbol", "timestamp"],
        )
        raw = pd.DataFrame({
            "open": [10, 11, 20, 21], "high": [12, 13, 22, 23],
            "low": [9, 10, 19, 20], "close": [11, 12, 21, 22],
            "volume": [100, 110, 200, 210],
        }, index=index)
        result = module._normalise_alpaca_frames(raw, ["BRK-B", "AAPL"], ["BRK.B", "AAPL"])
        self.assertEqual(set(result), {"BRK-B", "AAPL"})
        self.assertIn("Close", result["BRK-B"].columns)
        self.assertEqual(len(result["AAPL"]), 2)

    def test_provider_summary_counts_missing_expected_sessions(self):
        frame = pd.DataFrame({
            "Open": [10.0], "High": [12.0], "Low": [9.0],
            "Close": [11.0], "Volume": [100],
        }, index=pd.DatetimeIndex(["2026-11-06"]))
        fake_history = types.SimpleNamespace(
            _ohlcv_validation_reasons=lambda row, **kwargs: []
        )
        with patch.dict("sys.modules", {"historical_ohlcv": fake_history}):
            summary = module._provider_summary(
                frame, "rows_returned", ["2026-11-05", "2026-11-06"], "TEST"
            )
        self.assertEqual(summary["expected_sessions_covered"], 1)
        self.assertEqual(summary["expected_sessions_missing"], 1)
        self.assertTrue(summary["has_latest_expected_session"])

    def test_empty_provider_summary_does_not_claim_latest_session(self):
        summary = module._provider_summary(None, "no_rows", ["2026-11-06"], "TEST")
        self.assertEqual(summary["bar_count"], 0)
        self.assertFalse(summary["has_latest_expected_session"])


    def test_alpaca_invalid_symbol_isolated_and_production_default_feed_preserved(self):
        requests_seen = []

        class FakeRequest:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                requests_seen.append(kwargs)

        class FakeClient:
            def get_stock_bars(self, request):
                symbols = request.kwargs["symbol_or_symbols"]
                if "BAD" in symbols:
                    raise RuntimeError("invalid symbol: BAD")
                index = pd.MultiIndex.from_product(
                    [symbols, pd.to_datetime(["2026-11-06"])],
                    names=["symbol", "timestamp"],
                )
                frame = pd.DataFrame({
                    "open": [10.0], "high": [12.0], "low": [9.0],
                    "close": [11.0], "volume": [100],
                }, index=index)
                return types.SimpleNamespace(df=frame)

        fake_alpaca = {
            "alpaca": types.ModuleType("alpaca"),
            "alpaca.data": types.ModuleType("alpaca.data"),
            "alpaca.data.enums": types.SimpleNamespace(Adjustment=types.SimpleNamespace(RAW="raw")),
            "alpaca.data.requests": types.SimpleNamespace(StockBarsRequest=FakeRequest),
            "alpaca.data.timeframe": types.SimpleNamespace(TimeFrame=types.SimpleNamespace(Day="1Day")),
        }
        with patch.dict("sys.modules", fake_alpaca), patch.object(module.time, "sleep", return_value=None):
            frames, statuses = module.fetch_alpaca(
                FakeClient(), ["GOOD", "BAD"],
                pd.Timestamp("2026-11-01").to_pydatetime(),
                pd.Timestamp("2026-11-08").to_pydatetime(),
            )
        self.assertIn("GOOD", frames)
        self.assertEqual(statuses["GOOD"], "rows_returned")
        self.assertEqual(statuses["BAD"], "invalid_symbol")
        self.assertTrue(all("feed" not in request for request in requests_seen))
        self.assertTrue(all(request["adjustment"] == "raw" for request in requests_seen))

    def test_markdown_report_identifies_read_only_database(self):
        report = {
            "created_at_utc": "2026-11-07T00:00:00+00:00", "github_sha": "abc",
            "database_sha256_before": "a" * 64, "database_unchanged": True,
            "provider_comparison_summary": {
                "universe_count": 1, "alpaca_equity_universe_count": 1,
                "index_benchmark_count_excluded_from_alpaca_stock_endpoint": 0,
                "lookback_start": "2025-11-07",
                "lookback_end_exclusive": "2026-11-08", "alpaca_feed": "API default",
                "alpaca_adjustment": "RAW", "yahoo_auto_adjust": False,
                "alpaca_rows_ticker_count": 1, "yahoo_rows_ticker_count": 1,
                "alpaca_no_rows_ticker_count": 0, "yahoo_no_rows_ticker_count": 0,
                "alpaca_invalid_symbol_ticker_count": 0,
                "alpaca_request_error_ticker_count": 0, "yahoo_request_error_ticker_count": 0,
                "alpaca_latest_session_ticker_count": 1, "yahoo_latest_session_ticker_count": 1,
                "alpaca_missing_expected_sessions": 0, "yahoo_missing_expected_sessions": 0,
                "alpaca_invalid_bars": 0, "yahoo_invalid_bars": 0,
                "both_latest": 1, "alpaca_only_latest": 0, "yahoo_only_latest": 0,
                "neither_latest": 0, "us_historical_gap_ticker_count": 0,
                "us_historical_gap_session_count": 0,
            },
            "targeted_historical_gap_probe": {},
        }
        rendered = module.render_markdown(report)
        self.assertIn("Originaldatenbank unverändert: **True**", rendered)
        self.assertIn("Keine offenen historischen US-Lücken", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
