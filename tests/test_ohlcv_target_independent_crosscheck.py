from __future__ import annotations

import csv

import ohlcv_target_independent_crosscheck as diag


def source_row(**updates):
    row = {
        "ticker": "ABBNY", "date": "2026-11-05", "open": 84.59,
        "high": 84.59, "low": 81.48, "close": 81.36,
        "invalid_reasons": "high_below_close",
    }
    row.update(updates)
    return row


def test_target_filter_keeps_only_five_declared_dates(tmp_path):
    path = tmp_path / "invalid.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(source_row().keys()))
        writer.writeheader()
        writer.writerow(source_row())
        writer.writerow(source_row(date="2026-11-04"))
    rows = diag.load_target_rows(path)
    assert [(r["ticker"], r["date"]) for r in rows] == [("ABBNY", "2026-11-05")]


def test_valid_ohlc_checks_all_bounds():
    assert diag._valid_ohlc({"open": 10, "high": 12, "low": 9, "close": 11})
    assert not diag._valid_ohlc({"open": 10, "high": 9, "low": 8, "close": 9.5})


def test_independent_equity_difference_is_confirmed():
    repaired = {"open": 84.41, "high": 84.51, "low": 81.23, "close": 81.36}
    classification, _ = diag._classify(source_row(), repaired, "rows_returned")
    assert classification == "BESTÄTIGT_FEHLERHAFT"


def test_independent_equity_agreement_is_ok():
    same = {"open": 84.59, "high": 84.59, "low": 81.0, "close": 81.36}
    classification, _ = diag._classify(source_row(low=81.0), same, "rows_returned")
    assert classification == "OK"


def test_fx_difference_stays_unresolved_due_to_session_cutoff():
    source = {"open": 1.1575, "high": 1.1590, "low": 1.1574, "close": 1.1582}
    classification, basis = diag._classify(
        {"open": 1.1575, "high": 1.1580, "low": 1.15755, "close": 1.15739},
        source, "rows_returned", is_fx=True,
    )
    assert classification == "ABWEICHUNG_UNGEKLÄRT"
    assert "Session" in basis


def test_missing_source_never_confirms_error():
    classification, _ = diag._classify(source_row(), None, "provider_not_configured")
    assert classification == "ABWEICHUNG_UNGEKLÄRT"


def test_symbol_search_requires_exact_us_listing(monkeypatch):
    monkeypatch.setattr(diag, "API_KEY", "test-key")
    monkeypatch.setattr(diag, "REQUEST_PAUSE", 0)

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"bestMatches": [
                {"1. symbol": "ABBNY.A", "2. name": "Other", "4. region": "United States"},
                {"1. symbol": "ABBNY", "2. name": "ABB Ltd", "4. region": "United States"},
            ]}

    calls = []
    def fake_get(url, params, timeout, headers):
        calls.append(params)
        if params["function"] == "SYMBOL_SEARCH":
            return Response()
        class DailyResponse(Response):
            def json(self):
                return {"Time Series (Daily)": {"2026-11-05": {
                    "1. open": "84.41", "2. high": "84.51", "3. low": "81.23", "4. close": "81.36"
                }}}
        return DailyResponse()

    bars, status, name = diag.fetch_equity_daily("ABBNY", http_get=fake_get, sleep=lambda _: None)
    assert status == "rows_returned"
    assert name == "ABB Ltd"
    assert "2026-11-05" in bars
    assert [call["function"] for call in calls] == ["SYMBOL_SEARCH", "TIME_SERIES_DAILY"]


def test_no_api_key_does_not_call_provider(monkeypatch):
    monkeypatch.setattr(diag, "API_KEY", "")
    def fail(*args, **kwargs):
        raise AssertionError("provider must not be called without API key")
    bars, status, name = diag.fetch_equity_daily("ABBNY", http_get=fail)
    assert bars == {}
    assert status == "provider_not_configured"
    assert name == ""


def test_fx_daily_series_uses_fx_specific_json_key():
    parsed = diag._parse_daily_series({"Time Series FX (Daily)": {"2026-08-17": {
        "1. open": "1.16", "2. high": "1.17", "3. low": "1.15", "4. close": "1.165"
    }}})
    assert parsed["2026-08-17"]["close"] == 1.165


def test_run_crosscheck_integrates_all_five_targets_without_live_network(monkeypatch):
    monkeypatch.setattr(diag, "API_KEY", "test-key")
    monkeypatch.setattr(diag, "REQUEST_PAUSE", 0)
    rows = [
        source_row(),
        source_row(ticker="LZAGF", date="2026-11-05"),
        source_row(ticker="SIEGY", date="2026-11-05"),
        source_row(ticker="EURUSD=X", date="2026-08-17", open=1.1575,
                   high=1.1576, low=1.15755, close=1.15739),
        source_row(ticker="EURUSD=X", date="2026-11-02", open=1.1162,
                   high=1.1163, low=1.1161, close=1.11568),
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload

    def fake_get(url, params, timeout, headers):
        function = params["function"]
        if function == "SYMBOL_SEARCH":
            ticker = params["keywords"]
            return Response({"bestMatches": [{
                "1. symbol": ticker, "2. name": f"Verified {ticker}",
                "4. region": "United States",
            }]})
        if function == "FX_DAILY":
            return Response({"Time Series FX (Daily)": {
                "2026-08-17": {"1. open": "1.1575", "2. high": "1.1590", "3. low": "1.1574", "4. close": "1.1582"},
                "2026-11-02": {"1. open": "1.1162", "2. high": "1.1170", "3. low": "1.1155", "4. close": "1.1165"},
            }})
        return Response({"Time Series (Daily)": {"2026-11-05": {
            "1. open": "84.41", "2. high": "84.51", "3. low": "81.23", "4. close": "81.36"
        }}})

    result = diag.run_crosscheck(rows, http_get=fake_get, sleep=lambda _: None)
    assert result["targets_found_in_invalid_csv"] == 5
    assert len(result["rows"]) == 5
    equity = [item for item in result["rows"] if item["ticker"] != "EURUSD=X"]
    fx = [item for item in result["rows"] if item["ticker"] == "EURUSD=X"]
    assert all(item["classification"] == "BESTÄTIGT_FEHLERHAFT" for item in equity)
    assert all(item["classification"] == "ABWEICHUNG_UNGEKLÄRT" for item in fx)


def test_run_without_key_skips_network_and_waits(monkeypatch):
    monkeypatch.setattr(diag, "API_KEY", "")
    monkeypatch.setattr(diag, "REQUEST_PAUSE", 12)
    sleeps = []
    def fail(*args, **kwargs):
        raise AssertionError("network must not be called without key")
    rows = [
        source_row(),
        source_row(ticker="LZAGF", date="2026-11-05"),
        source_row(ticker="SIEGY", date="2026-11-05"),
        source_row(ticker="EURUSD=X", date="2026-08-17"),
        source_row(ticker="EURUSD=X", date="2026-11-02"),
    ]
    result = diag.run_crosscheck(rows, http_get=fail, sleep=sleeps.append)
    assert result["targets_found_in_invalid_csv"] == 5
    assert result["classification_counts"] == {"ABWEICHUNG_UNGEKLÄRT": 5}
    assert sleeps == []
