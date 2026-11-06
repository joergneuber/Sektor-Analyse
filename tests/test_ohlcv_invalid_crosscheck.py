from __future__ import annotations

import csv
import json
from pathlib import Path

import requests

import ohlcv_invalid_crosscheck as diag


def row(**overrides):
    value = {
        "ticker": "TEST.DE", "date": "2020-01-02", "open": 10.0,
        "high": 9.0, "low": 8.0, "close": 9.5, "volume": 100.0,
        "source": "yfinance", "invalid_reasons": "high_below_open|high_below_close",
        "recent_90d": 0,
    }
    value.update(overrides)
    return value


def test_valid_ohlc_accepts_valid_bar():
    assert diag._valid_ohlc({"open": 10, "high": 12, "low": 9, "close": 11})


def test_valid_ohlc_rejects_high_below_close():
    assert not diag._valid_ohlc({"open": 10, "high": 9, "low": 8, "close": 9.5})


def test_stooq_mapping_is_conservative_for_futures_and_indices():
    assert diag.stooq_symbol_for_ticker("BZ=F")[0] is None
    assert diag.stooq_symbol_for_ticker("^STOXX")[0] is None
    assert diag.stooq_symbol_for_ticker("SAP.DE")[0] == "sap.de"
    assert diag.stooq_symbol_for_ticker("EURUSD=X")[0] == "eurusd"


def test_explicit_stooq_override_wins():
    assert diag.stooq_symbol_for_ticker("ABBNY", {"ABBNY": "abbny.us"}) == (
        "abbny.us", "explicit_environment_mapping"
    )


def test_cme_close_outside_range_is_settlement_special_case():
    source = row(ticker="GC=F", invalid_reasons="high_below_close")
    classification, _ = diag._classify(source, None, None, "no_data")
    assert classification == "SETTLEMENT_SONDERFALL"


def test_non_cme_future_is_not_automatically_settlement_special_case():
    source = row(ticker="BZ=F", invalid_reasons="high_below_close")
    classification, _ = diag._classify(source, None, None, "unsupported_mapping")
    assert classification == "ABWEICHUNG_UNGEKLÄRT"


def test_independent_valid_stooq_difference_confirms_bad_stored_candle():
    source = row()
    stooq = {"open": 10.0, "high": 11.0, "low": 8.0, "close": 9.5}
    classification, _ = diag._classify(source, None, stooq, "rows_returned", "exchange_suffix_mapping")
    assert classification == "BESTÄTIGT_FEHLERHAFT"


def test_yahoo_repair_alone_is_not_independent_confirmation():
    source = row()
    repaired = {"open": 10.0, "high": 11.0, "low": 8.0, "close": 9.5}
    classification, _ = diag._classify(source, repaired, None, "no_data")
    assert classification == "ABWEICHUNG_UNGEKLÄRT"


def test_tiny_rounding_difference_can_be_ok():
    source = row(high=9.9999995, close=9.9999998,
                 invalid_reasons="high_below_close")
    stooq = {"open": 10.0, "high": 10.0, "low": 8.0, "close": 10.0}
    classification, _ = diag._classify(source, None, stooq, "rows_returned", "exchange_suffix_mapping")
    assert classification == "OK"


def test_load_invalid_rows_and_write_outputs(tmp_path, monkeypatch):
    input_file = tmp_path / "input.csv"
    with input_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "ticker", "date", "open", "high", "low", "close", "volume",
            "source", "invalid_reasons", "recent_90d",
        ])
        writer.writeheader()
        writer.writerow(row())
    loaded = diag.load_invalid_rows(input_file)
    assert len(loaded) == 1
    assert loaded[0]["ticker"] == "TEST.DE"
    assert loaded[0]["open"] == 10.0

    monkeypatch.setattr(diag, "DETAIL_CSV", tmp_path / "out.csv")
    monkeypatch.setattr(diag, "SUMMARY_JSON", tmp_path / "out.json")
    monkeypatch.setattr(diag, "REPORT_MD", tmp_path / "out.md")
    result = {
        "ticker_count_checked": 1, "ticker_count_requested": 1,
        "invalid_candle_count_checked": 1,
        "invalid_candle_count_skipped_by_max_tickers": 0,
        "price_tolerance_relative": 0.0001,
        "provider_ticker_status_counts": {"yahoo_repair": {"rows_returned": 1}, "stooq": {"no_data": 1}},
        "classification_counts": {"ABWEICHUNG_UNGEKLÄRT": 1},
        "limitations": ["unit-test"], "rows": [{**row(), "classification": "ABWEICHUNG_UNGEKLÄRT"}],
    }
    diag.write_outputs(result)
    assert (tmp_path / "out.csv").is_file()
    assert json.loads((tmp_path / "out.json").read_text())["classification_counts"] == {"ABWEICHUNG_UNGEKLÄRT": 1}
    assert "OHLCV-Quellenvergleich" in (tmp_path / "out.md").read_text()


def test_run_crosscheck_uses_provider_results_without_touching_database(monkeypatch):
    source = row()
    calls = []

    def yahoo(ticker, start, end):
        calls.append(("yahoo", ticker, start, end))
        return {"2020-01-02": {"open": 10, "high": 11, "low": 8, "close": 9.5}}

    def stooq(symbol, start, end):
        calls.append(("stooq", symbol, start, end))
        return {"2020-01-02": {"open": 10, "high": 11, "low": 8, "close": 9.5}}

    monkeypatch.setattr(diag, "REQUEST_PAUSE", 0)
    monkeypatch.setattr(diag, "_load_overrides", lambda: {})
    result = diag.run_crosscheck([source], yahoo_fetch=yahoo, stooq_fetch=stooq, sleep=lambda _: None)
    assert result["invalid_candle_count_checked"] == 1
    assert result["classification_counts"] == {"BESTÄTIGT_FEHLERHAFT": 1}
    assert len(calls) == 2



def test_stooq_circuit_breaker_stops_after_consecutive_transport_errors(monkeypatch):
    sources = [
        row(ticker="TESTA.DE"),
        row(ticker="TESTB.DE"),
        row(ticker="TESTC.DE"),
        row(ticker="TESTD.DE"),
    ]
    stooq_calls = []

    def yahoo(ticker, start, end):
        return {}

    def stooq(symbol, start, end):
        stooq_calls.append(symbol)
        raise requests.exceptions.ConnectTimeout("simulated timeout")

    monkeypatch.setattr(diag, "REQUEST_PAUSE", 0)
    monkeypatch.setattr(diag, "STOOQ_CIRCUIT_BREAKER_FAILURES", 2)
    monkeypatch.setattr(diag, "_load_overrides", lambda: {})
    result = diag.run_crosscheck(
        sources, yahoo_fetch=yahoo, stooq_fetch=stooq, sleep=lambda _: None
    )

    assert stooq_calls == ["testa.de", "testb.de"]
    assert result["stooq_circuit_breaker_open"] is True
    assert result["stooq_consecutive_transport_errors"] == 2
    assert result["provider_ticker_status_counts"]["stooq"] == {
        "request_error": 2,
        "provider_unavailable_after_consecutive_errors": 2,
    }
    assert all(
        item["classification"] == "ABWEICHUNG_UNGEKLÄRT"
        for item in result["rows"]
    )
    assert "ausgesetzt" in result["rows"][2]["classification_basis"]
    assert [
        item["stooq_status"] for item in result["rows"]
    ] == [
        "request_error:ConnectTimeout:simulated timeout",
        "request_error:ConnectTimeout:simulated timeout",
        "provider_unavailable_after_consecutive_errors",
        "provider_unavailable_after_consecutive_errors",
    ]
