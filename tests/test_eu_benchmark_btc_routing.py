from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load_function(path: str, function_name: str, namespace: dict):
    source = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace[function_name]


def _quality_frame(value: float, status: str = "PASS") -> pd.DataFrame:
    frame = pd.DataFrame(
        {"Close": [value]},
        index=pd.DatetimeIndex(["2026-11-10"]),
    )
    frame.attrs["ohlcv_quality"] = {"quality_status": status, "reason_codes": []}
    return frame


def test_exsa_uses_direct_yahoo_fallback_only_after_primary_quality_rejection():
    calls = []
    primary = pd.DataFrame()
    primary.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE",
        "reason_codes": ["LATEST_EXPECTED_BAR_INVALID", "MISSING_EXPECTED_SESSION"],
    }
    fallback = _quality_frame(123.45)

    def raw_fetch(ticker):
        calls.append(("raw", ticker))
        return pd.DataFrame({"Close": [100.0]})

    def quality_check(ticker, frame):
        calls.append(("quality", ticker))
        return primary

    def direct_fetch(ticker, *, period):
        calls.append(("direct", ticker, period))
        return fallback

    namespace = {
        "pd": pd,
        "_get_yf_history_raw": raw_fetch,
        "_quality_checked_frame": quality_check,
        "get_yf_history_from_yahoo": direct_fetch,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    result = function("EXSA.DE")

    assert calls == [
        ("raw", "EXSA.DE"),
        ("quality", "EXSA.DE"),
        ("direct", "EXSA.DE", "1y"),
    ]
    assert result.iloc[-1]["Close"] == 123.45
    assert result.attrs["ohlcv_quality"]["quality_status"] == "PASS"



def test_exsa_uses_direct_yahoo_fallback_when_primary_fetch_raises():
    calls = []
    fallback = _quality_frame(124.56, status="PASS_WITH_EXCLUSIONS")

    def raw_fetch(ticker):
        calls.append(("raw", ticker))
        raise TimeoutError("primary Yahoo request timed out")

    def direct_fetch(ticker, *, period):
        calls.append(("direct", ticker, period))
        return fallback

    namespace = {
        "pd": pd,
        "_get_yf_history_raw": raw_fetch,
        "_quality_checked_frame": lambda ticker, frame: (_ for _ in ()).throw(
            AssertionError("Quality gate must not run without primary data")
        ),
        "get_yf_history_from_yahoo": direct_fetch,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    result = function("EXSA.DE")

    assert calls == [
        ("raw", "EXSA.DE"),
        ("direct", "EXSA.DE", "1y"),
    ]
    assert result.iloc[-1]["Close"] == 124.56
    assert result.attrs["ohlcv_quality"]["quality_status"] == "PASS_WITH_EXCLUSIONS"



def test_exsa_primary_exception_and_failed_direct_fallback_remain_fail_closed():
    fallback = pd.DataFrame()
    fallback.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE",
        "reason_codes": ["YAHOO_PROVIDER_ERROR:TimeoutError"],
    }

    def raw_fetch(ticker):
        raise TimeoutError("primary timeout")

    namespace = {
        "pd": pd,
        "_get_yf_history_raw": raw_fetch,
        "_quality_checked_frame": lambda ticker, frame: frame,
        "get_yf_history_from_yahoo": lambda ticker, *, period: fallback,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    result = function("EXSA.DE")
    assert result.empty
    assert result.attrs["ohlcv_quality"]["quality_status"] == "INCOMPLETE"

def test_non_benchmark_primary_fetch_exception_keeps_legacy_propagation():
    def raw_fetch(ticker):
        raise TimeoutError("primary request timed out")

    namespace = {
        "pd": pd,
        "_get_yf_history_raw": raw_fetch,
        "_quality_checked_frame": lambda ticker, frame: frame,
        "get_yf_history_from_yahoo": lambda ticker, *, period: (_ for _ in ()).throw(
            AssertionError("No new retry for non-benchmark tickers")
        ),
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    try:
        function("AAPL")
    except TimeoutError as exc:
        assert str(exc) == "primary request timed out"
    else:
        raise AssertionError("Legacy exception behavior must be preserved")


def test_exsa_accepts_pass_with_exclusions_from_quality_gate():
    primary = pd.DataFrame()
    primary.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE", "reason_codes": ["MISSING_EXPECTED_SESSION"]
    }
    fallback = _quality_frame(123.45, status="PASS_WITH_EXCLUSIONS")
    namespace = {
        "pd": pd,
        "_get_yf_history_raw": lambda ticker: pd.DataFrame({"Close": [100.0]}),
        "_quality_checked_frame": lambda ticker, frame: primary,
        "get_yf_history_from_yahoo": lambda ticker, *, period: fallback,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    result = function("EXSA.DE")
    assert result.iloc[-1]["Close"] == 123.45
    assert result.attrs["ohlcv_quality"]["quality_status"] == "PASS_WITH_EXCLUSIONS"

def test_exsa_does_not_retry_when_primary_quality_gate_passes():
    calls = []
    primary = _quality_frame(120.0)

    namespace = {
        "pd": pd,
        "_get_yf_history_raw": lambda ticker: pd.DataFrame({"Close": [120.0]}),
        "_quality_checked_frame": lambda ticker, frame: calls.append(("quality", ticker)) or primary,
        "get_yf_history_from_yahoo": lambda ticker, *, period: (_ for _ in ()).throw(
            AssertionError("Direct fallback must not run for a valid primary result")
        ),
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    result = function("EXSA.DE")

    assert calls == [("quality", "EXSA.DE")]
    assert result.iloc[-1]["Close"] == 120.0


def test_exsa_remains_fail_closed_when_direct_yahoo_also_fails_quality_gate():
    primary = pd.DataFrame()
    primary.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE", "reason_codes": ["MISSING_EXPECTED_SESSION"]
    }
    fallback = pd.DataFrame()
    fallback.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE", "reason_codes": ["LATEST_EXPECTED_BAR_INVALID"]
    }
    namespace = {
        "pd": pd,
        "_get_yf_history_raw": lambda ticker: pd.DataFrame({"Close": [100.0]}),
        "_quality_checked_frame": lambda ticker, frame: primary,
        "get_yf_history_from_yahoo": lambda ticker, *, period: fallback,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    assert function("EXSA.DE").empty


def test_exsa_rejects_nonempty_direct_fallback_without_pass_quality_status():
    primary = pd.DataFrame()
    primary.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE", "reason_codes": ["MISSING_EXPECTED_SESSION"]
    }
    fallback = _quality_frame(123.45, status="INCOMPLETE")
    namespace = {
        "pd": pd,
        "_get_yf_history_raw": lambda ticker: pd.DataFrame({"Close": [100.0]}),
        "_quality_checked_frame": lambda ticker, frame: primary,
        "get_yf_history_from_yahoo": lambda ticker, *, period: fallback,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    assert function("EXSA.DE").empty


def test_non_benchmark_ticker_does_not_receive_new_direct_retry():
    primary = pd.DataFrame()
    primary.attrs["ohlcv_quality"] = {
        "quality_status": "INCOMPLETE", "reason_codes": ["MISSING_EXPECTED_SESSION"]
    }
    calls = []
    namespace = {
        "pd": pd,
        "_get_yf_history_raw": lambda ticker: pd.DataFrame({"Close": [100.0]}),
        "_quality_checked_frame": lambda ticker, frame: primary,
        "get_yf_history_from_yahoo": lambda ticker, *, period: calls.append(ticker),
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function("market_cache.py", "get_yf_history", namespace)

    assert function("AAPL").empty
    assert calls == []


def test_crypto_assets_never_enter_stock_trend_following_client():
    calls = []

    def stock(*args):
        calls.append(("stock", args))
        return {"source": "stock"}

    def eu(*args):
        calls.append(("eu", args))
        return {"source": "eu"}

    namespace = {
        "KRYPTOTICKER": frozenset({"BTC-USD", "ETH-USD"}),
        "analyze_a_setup": stock,
        "analyze_a_setup_eu": eu,
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function(
        "einzel_check.py", "_trendfolge_einzelcheck", namespace
    )

    assert function("BTC-USD", "N/A", False, "SPY", "EXSA") is None
    assert function("ETH-USD", "N/A", False, "SPY", "EXSA") is None
    assert calls == []


def test_pruefe_routes_trend_following_through_crypto_guard():
    source = (ROOT / "einzel_check.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    pruefe = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "pruefe"
    )
    called_names = {
        node.func.id for node in ast.walk(pruefe)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_trendfolge_einzelcheck" in called_names
    assert "analyze_a_setup" not in called_names
    assert "analyze_a_setup_eu" not in called_names


def test_equity_and_eu_trend_following_dispatch_remains_unchanged():
    calls = []
    namespace = {
        "KRYPTOTICKER": frozenset({"BTC-USD", "ETH-USD"}),
        "analyze_a_setup": lambda *args: calls.append(("stock", args)) or "US_RESULT",
        "analyze_a_setup_eu": lambda *args: calls.append(("eu", args)) or "EU_RESULT",
        "print": lambda *args, **kwargs: None,
    }
    function = _load_function(
        "einzel_check.py", "_trendfolge_einzelcheck", namespace
    )

    assert function("MSFT", "Technologie", False, "SPY_CLOSE", "EU_CLOSE") == "US_RESULT"
    assert function("SAP.DE", "Industrie", True, "SPY_CLOSE", "EU_CLOSE") == "EU_RESULT"
    assert calls == [
        ("stock", ("MSFT", "Technologie", "SPY_CLOSE")),
        ("eu", ("SAP.DE", "Industrie", "EU_CLOSE")),
    ]
