from __future__ import annotations

import ast
import datetime as dt
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _bars(symbols: list[str]) -> pd.DataFrame:
    symbols = list(symbols)
    index = pd.MultiIndex.from_product(
        [symbols, pd.to_datetime(["2026-11-06", "2026-11-09"])],
        names=["symbol", "timestamp"],
    )
    return pd.DataFrame(
        {
            "open": [10.0, 10.5] * len(symbols),
            "high": [11.0, 11.5] * len(symbols),
            "low": [9.0, 10.0] * len(symbols),
            "close": [10.5, 11.0] * len(symbols),
            "volume": [1000, 1100] * len(symbols),
        },
        index=index,
    )


class FakeRequest:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class MarketDataSipFallbackTests(unittest.TestCase):
    def _load_market_data(self):
        fake_alpaca = {
            "alpaca": types.ModuleType("alpaca"),
            "alpaca.data": types.ModuleType("alpaca.data"),
            "alpaca.data.enums": types.ModuleType("alpaca.data.enums"),
            "alpaca.data.requests": types.ModuleType("alpaca.data.requests"),
            "alpaca.data.timeframe": types.ModuleType("alpaca.data.timeframe"),
        }
        fake_alpaca["alpaca.data.enums"].Adjustment = types.SimpleNamespace(SPLIT="split")
        fake_alpaca["alpaca.data.requests"].StockBarsRequest = FakeRequest
        fake_alpaca["alpaca.data.timeframe"].TimeFrame = types.SimpleNamespace(Day="1Day")
        previous_market_data = sys.modules.pop("market_data", None)
        with patch.dict(sys.modules, fake_alpaca):
            module = importlib.import_module("market_data")

        def restore_market_data_module():
            sys.modules.pop("market_data", None)
            if previous_market_data is not None:
                sys.modules["market_data"] = previous_market_data

        self.addCleanup(restore_market_data_module)
        return module

    def test_default_shared_fetch_keeps_legacy_request_feed_and_end_defaults(self):
        market_data = self._load_market_data()
        requests = []

        class Client:
            def get_stock_bars(self, request):
                requests.append(request.kwargs)
                return types.SimpleNamespace(df=_bars(request.kwargs["symbol_or_symbols"]))

        fake_quality = types.ModuleType("ohlcv_quality_gate")
        fake_quality.quality_checked_frame = lambda frame, *args, **kwargs: frame
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = market_data.fetch_us_batch_robust(Client(), ["AAPL"])

        self.assertIn("AAPL", result)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("feed", requests[0])
        self.assertNotIn("end", requests[0])
        self.assertEqual(requests[0]["adjustment"], "split")

    def test_explicit_sip_feed_and_completed_end_survive_invalid_symbol_split(self):
        market_data = self._load_market_data()
        requests = []

        class Client:
            def get_stock_bars(self, request):
                requests.append(request.kwargs)
                symbols = request.kwargs["symbol_or_symbols"]
                if "BAD" in symbols:
                    raise RuntimeError("invalid symbol: BAD")
                return types.SimpleNamespace(df=_bars(symbols))

        fake_quality = types.ModuleType("ohlcv_quality_gate")
        fake_quality.quality_checked_frame = lambda frame, *args, **kwargs: frame
        end = dt.datetime(2026, 11, 10, 0, 0)
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = market_data.fetch_us_batch_robust(
                Client(), ["AAPL", "BAD", "BRK-B"],
                chunk_size=3, days=365, end=end, feed="sip",
            )

        self.assertEqual(set(result), {"AAPL", "BRK-B"})
        self.assertTrue(requests)
        self.assertTrue(all(item["feed"] == "sip" for item in requests))
        self.assertTrue(all(item["end"] == end for item in requests))
        self.assertTrue(all(item["adjustment"] == "split" for item in requests))
        self.assertTrue(any("BRK.B" in item["symbol_or_symbols"] for item in requests))

    def _load_short_fetch(
        self, *, primary_symbols, fallback_result, primary_error=None,
        primary_response=None,
    ):
        source = (ROOT / "short_scanner.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef) and item.name == "fetch_us_batch"
        )
        requests = []
        fallback_calls = []
        logs = []

        class Client:
            def get_stock_bars(self, request):
                requests.append(request.kwargs)
                if primary_error is not None:
                    raise primary_error
                response = primary_response if primary_response is not None else _bars(primary_symbols)
                return types.SimpleNamespace(df=response)

        def fallback(client, tickers, **kwargs):
            fallback_calls.append((list(tickers), kwargs))
            return fallback_result

        namespace = {
            "datetime": dt,
            "Counter": __import__("collections").Counter,
            "_chunks": lambda values, size: [values[i:i + size] for i in range(0, len(values), size)],
            "CHUNK_SIZE": 100,
            "alpaca_client": Client(),
            "StockBarsRequest": FakeRequest,
            "TimeFrame": types.SimpleNamespace(Day="1Day"),
            "Adjustment": types.SimpleNamespace(SPLIT="split"),
            "DataFeed": types.SimpleNamespace(SIP="sip"),
            "fetch_us_batch_robust": fallback,
            "print": lambda *args, **kwargs: logs.append(" ".join(str(arg) for arg in args)),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "short_scanner.py"), "exec"), namespace)
        fake_quality = types.ModuleType("ohlcv_quality_gate")
        fake_quality.quality_checked_frame = lambda frame, *args, **kwargs: frame
        return namespace["fetch_us_batch"], requests, fallback_calls, fake_quality, logs

    def test_only_missing_primary_tickers_are_retried_via_historical_sip(self):
        fallback_frame = _bars(["MSFT"])
        fetch, requests, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL"], fallback_result={"MSFT": fallback_frame}
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(set(result), {"AAPL", "MSFT"})
        self.assertEqual(len(fallback_calls), 1)
        tickers, kwargs = fallback_calls[0]
        self.assertEqual(tickers, ["MSFT"])
        self.assertEqual(kwargs["feed"], "sip")
        self.assertEqual(kwargs["days"], 365)
        self.assertEqual(kwargs["chunk_size"], 100)
        self.assertEqual(kwargs["end"].hour, 0)
        self.assertEqual(kwargs["end"].minute, 0)
        self.assertIsNone(kwargs["end"].tzinfo)
        self.assertEqual(requests[0]["adjustment"], "split")
        self.assertTrue(any("nicht in Response=1" in line for line in logs))
        self.assertTrue(any("Quality-Gate gesperrt=0" in line for line in logs))

    def test_quality_gate_rejection_is_classified_separately_and_retried(self):
        fetch, _, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL", "MSFT"], fallback_result={"MSFT": _bars(["MSFT"])}
        )
        original_quality = fake_quality.quality_checked_frame

        def reject_msft(frame, ticker, **kwargs):
            if ticker == "MSFT":
                blocked = pd.DataFrame()
                blocked.attrs["ohlcv_quality"] = {"reason_codes": ["TEST_BLOCKED"]}
                return blocked
            return original_quality(frame, ticker, **kwargs)

        fake_quality.quality_checked_frame = reject_msft
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(set(result), {"AAPL", "MSFT"})
        self.assertEqual(fallback_calls[0][0], ["MSFT"])
        self.assertTrue(any("ohne Bars=0" in line for line in logs))
        self.assertTrue(any("Quality-Gate gesperrt=1" in line for line in logs))
        self.assertTrue(any("TEST_BLOCKED" in line for line in logs))

    def test_symbol_processing_exception_is_classified_and_retried(self):
        fetch, _, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL", "MSFT"], fallback_result={}
        )

        def fail_msft(frame, ticker, **kwargs):
            if ticker == "MSFT":
                raise ValueError("malformed frame")
            return frame

        fake_quality.quality_checked_frame = fail_msft
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(set(result), {"AAPL"})
        self.assertEqual(fallback_calls[0][0], ["MSFT"])
        self.assertTrue(any("Symbolfehler=1" in line for line in logs))
        self.assertTrue(any("Bilanz=2/2" in line for line in logs))

    def test_quality_result_inspection_error_is_classified_and_retried(self):
        class BrokenQualityResult:
            @property
            def empty(self):
                raise ValueError("malformed quality result")

        fetch, _, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL"], fallback_result={}
        )
        fake_quality.quality_checked_frame = lambda *args, **kwargs: BrokenQualityResult()
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL"])

        self.assertEqual(result, {})
        self.assertEqual(fallback_calls[0][0], ["AAPL"])
        self.assertTrue(any("Symbolfehler=1" in line for line in logs))
        self.assertTrue(any("Bilanz=1/1" in line for line in logs))
        self.assertTrue(any("malformed quality result" in line for line in logs))

    def test_non_key_error_while_extracting_symbol_is_classified_and_retried(self):
        class BrokenLocResponse:
            empty = False
            index = ["AAPL", "MSFT"]

            @property
            def loc(self):
                return self

            def __getitem__(self, ticker):
                raise ValueError("malformed provider response")

        fetch, _, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=[],
            fallback_result={},
            primary_response=BrokenLocResponse(),
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(result, {})
        self.assertEqual(fallback_calls[0][0], ["AAPL", "MSFT"])
        self.assertTrue(any("Symbolfehler=2" in line for line in logs))
        self.assertTrue(any("Bilanz=2/2" in line for line in logs))
        self.assertTrue(any("malformed provider response" in line for line in logs))

    def test_primary_results_are_kept_and_no_sip_request_when_nothing_is_missing(self):
        primary_frame = _bars(["AAPL", "MSFT"])
        fetch, _, fallback_calls, fake_quality, _ = self._load_short_fetch(
            primary_symbols=["AAPL", "MSFT"], fallback_result={}
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(set(result), {"AAPL", "MSFT"})
        self.assertEqual(fallback_calls, [])


    def test_duplicate_sector_memberships_are_requested_once_and_reported_separately(self):
        fetch, requests, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL", "MSFT"], fallback_result={}
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "AAPL", "MSFT", "MSFT", "MSFT"])

        self.assertEqual(set(result), {"AAPL", "MSFT"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["symbol_or_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(fallback_calls, [])
        self.assertTrue(any(
            "Sektor-Mitgliedschaften=5, eindeutige US-Ticker=2, Mehrfachmitgliedschaften=3"
            in line for line in logs
        ))
        self.assertTrue(any("Bilanz=2/2" in line for line in logs))

    def test_blank_memberships_are_excluded_from_unique_and_duplicate_counts(self):
        fetch, requests, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL", "MSFT"], fallback_result={}
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "", "AAPL", "  ", "MSFT"])

        self.assertEqual(set(result), {"AAPL", "MSFT"})
        self.assertEqual(requests[0]["symbol_or_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(fallback_calls, [])
        self.assertTrue(any(
            "Sektor-Mitgliedschaften=3, eindeutige US-Ticker=2, "
            "Mehrfachmitgliedschaften=1, ungültig/leer=2" in line
            for line in logs
        ))


    def test_none_nan_non_string_and_blank_tickers_are_not_sent_to_alpaca(self):
        fetch, requests, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=["AAPL", "MSFT"], fallback_result={}
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", None, pd.NA, float("nan"), 42, "", "  ", "MSFT"])

        self.assertEqual(set(result), {"AAPL", "MSFT"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["symbol_or_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(fallback_calls, [])
        self.assertTrue(any(
            "Sektor-Mitgliedschaften=2, eindeutige US-Ticker=2, "
            "Mehrfachmitgliedschaften=0, ungültig/leer=6" in line
            for line in logs
        ))

    def test_funnel_metrics_distinguish_sector_memberships_from_unique_market_tickers(self):
        source = (ROOT / "short_scanner.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(
            item for item in tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "_funnel_universum_kennzahlen"
        )
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "short_scanner.py"), "exec"), namespace)
        helper = namespace["_funnel_universum_kennzahlen"]

        us_tasks = [("AAPL", "Technologie"), ("AAPL", "Software"), ("MSFT", "Technologie")]
        eu_tasks = [("SAP.DE", "Software"), ("SAP.DE", "Technologie"), ("SIE.DE", "Industrie")]
        us_data = {"MSFT": _bars(["MSFT"])}
        eu_data = {"SAP.DE": _bars(["SAP.DE"])}
        metrics = helper(us_tasks, eu_tasks, us_data, eu_data)

        self.assertEqual(metrics["gesamt_mitgliedschaften"], 6)
        self.assertEqual(metrics["gesamt_eindeutige_markt_ticker"], 4)
        self.assertEqual(metrics["gesamt_mehrfachmitgliedschaften"], 2)
        self.assertEqual(metrics["keine_daten_mitgliedschaften"], 3)
        self.assertEqual(metrics["keine_daten_eindeutige_markt_ticker"], 2)
        self.assertEqual(metrics["us_mehrfachmitgliedschaften"], 1)
        self.assertEqual(metrics["eu_mehrfachmitgliedschaften"], 1)

    def test_empty_primary_response_is_classified_as_no_bars_and_retried(self):
        fetch, _, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=[], fallback_result={}
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(result, {})
        self.assertEqual(fallback_calls[0][0], ["AAPL", "MSFT"])
        self.assertTrue(any("ohne Bars=2" in line for line in logs))
        self.assertTrue(any("Bilanz=2/2" in line for line in logs))

    def test_chunk_request_error_is_classified_and_entire_chunk_reaches_sip(self):
        fetch, _, fallback_calls, fake_quality, logs = self._load_short_fetch(
            primary_symbols=[],
            fallback_result={},
            primary_error=RuntimeError("temporary provider failure"),
        )
        with patch.dict(sys.modules, {"ohlcv_quality_gate": fake_quality}):
            result = fetch(["AAPL", "MSFT"])

        self.assertEqual(result, {})
        self.assertEqual(fallback_calls[0][0], ["AAPL", "MSFT"])
        self.assertTrue(any("Request-Fehler=2" in line for line in logs))
        self.assertTrue(any("Bilanz=2/2" in line for line in logs))
        self.assertTrue(any("weiterhin fehlend=2" in line for line in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
