import csv
import sys
import types

import numpy as np
import pandas as pd
import pytest

sys.modules.setdefault("yfinance", types.SimpleNamespace(Ticker=None))
import offene_positionen_check as checker


def _history(rows=100):
    idx = pd.bdate_range("2026-01-01", periods=rows)
    close = pd.Series(np.arange(100, 100 + rows, dtype=float), index=idx)
    return pd.DataFrame({
        "Open": close - 0.5, "High": close + 1.0, "Low": close - 1.0,
        "Close": close, "Adj Close": close, "Volume": 1000,
    }, index=idx)


def _quality(status="PASS", **overrides):
    payload = {
        "quality_status": status,
        "reason_codes": [],
        "latest_bar_valid": True,
        "last_3_bars_valid": True,
    }
    payload.update(overrides)
    return payload


def _install_db(monkeypatch, frame):
    monkeypatch.setattr("historical_ohlcv.load_history", lambda ticker: frame.copy())
    monkeypatch.setattr(
        "ohlcv_quality_gate._last_completed_session_date",
        lambda ticker: frame.index.max().date(),
    )


def test_db_first_accepts_current_quality_pass(monkeypatch):
    frame = _history(100)
    frame.attrs["ohlcv_quality"] = _quality()
    _install_db(monkeypatch, frame)

    class ForbiddenYahoo:
        def __init__(self, ticker):
            raise AssertionError("Yahoo darf bei aktueller DB-Historie nicht laufen.")

    monkeypatch.setattr(checker.yf, "Ticker", ForbiddenYahoo, raising=False)
    result = checker.fetch_history("TEST.DE")
    assert len(result) == 100
    assert result.attrs["history_source"] == "historical_ohlcv.sqlite"


def test_stale_or_incomplete_db_uses_yahoo_fallback(monkeypatch):
    db = _history(99)
    db.attrs["ohlcv_quality"] = _quality()
    yahoo = _history(100)
    monkeypatch.setattr("historical_ohlcv.load_history", lambda ticker: db.copy())
    monkeypatch.setattr(
        "ohlcv_quality_gate._last_completed_session_date",
        lambda ticker: yahoo.index.max().date(),
    )

    class YahooTicker:
        def __init__(self, ticker):
            pass
        def history(self, **kwargs):
            return yahoo.copy()

    class Quality:
        def to_dict(self):
            return _quality()

    monkeypatch.setattr(checker.yf, "Ticker", YahooTicker, raising=False)
    monkeypatch.setattr(
        "ohlcv_quality_gate.apply_ohlcv_quality_gate",
        lambda data, *args, **kwargs: (data.copy(), Quality()),
    )
    monkeypatch.setattr(checker, "_persist_usable_provider_history", lambda *args: len(yahoo))
    result = checker.fetch_history("TEST.DE")
    assert len(result) == 100
    assert result.attrs["history_source"] == "Yahoo-Fallback"


def test_if_both_sources_fail_quality_empty_result_keeps_diagnostics(monkeypatch):
    db = _history(100)
    db.attrs["ohlcv_quality"] = _quality(
        "INCOMPLETE", latest_bar_valid=False,
        reason_codes=["LATEST_EXPECTED_BAR_INVALID"],
    )
    _install_db(monkeypatch, db)

    class YahooTicker:
        def __init__(self, ticker):
            pass
        def history(self, **kwargs):
            return _history(100)

    class Quality:
        def to_dict(self):
            return _quality(
                "INCOMPLETE", latest_bar_valid=False,
                reason_codes=["CALENDAR_UNVERIFIED_OR_UNAVAILABLE"],
            )

    monkeypatch.setattr(checker.yf, "Ticker", YahooTicker, raising=False)
    monkeypatch.setattr(
        "ohlcv_quality_gate.apply_ohlcv_quality_gate",
        lambda data, *args, **kwargs: (data.copy(), Quality()),
    )
    result = checker.fetch_history("TEST.DE")
    assert result.empty
    assert result.attrs["ohlcv_quality"]["reason_codes"] == [
        "CALENDAR_UNVERIFIED_OR_UNAVAILABLE"
    ]
    assert "LATEST_EXPECTED_BAR_INVALID" in result.attrs["history_error"]


def test_invalid_internal_session_is_not_compressed_into_indicator_series():
    frame = _history(100)
    frame.attrs["ohlcv_quality"] = _quality(
        "PASS_WITH_EXCLUSIONS", reason_codes=["BAD_BAR"],
    )
    frame.iloc[-70, frame.columns.get_loc("High")] = 1.0
    result = checker._valid_ohlc_history(frame)
    assert len(result) == 69
    assert result.index[0] == frame.index[-69]
    assert result.index.is_monotonic_increasing
    assert not result[["Open", "High", "Low", "Close"]].isna().any().any()


def test_latest_invalid_bar_is_rejected_even_if_60_bars_exist(monkeypatch):
    db = _history(100)
    db.attrs["ohlcv_quality"] = _quality(
        "INCOMPLETE", latest_bar_valid=False,
        reason_codes=["LATEST_EXPECTED_BAR_INVALID"],
    )
    _install_db(monkeypatch, db)

    class YahooTicker:
        def __init__(self, ticker):
            pass
        def history(self, **kwargs):
            return _history(100)

    class Quality:
        def to_dict(self):
            return _quality(
                "INCOMPLETE", latest_bar_valid=False,
                reason_codes=["LATEST_EXPECTED_BAR_INVALID"],
            )

    monkeypatch.setattr(checker.yf, "Ticker", YahooTicker, raising=False)
    monkeypatch.setattr(
        "ohlcv_quality_gate.apply_ohlcv_quality_gate",
        lambda data, *args, **kwargs: (data.copy(), Quality()),
    )
    result = checker.fetch_history("TEST.DE")
    assert result.empty
    assert "LATEST_EXPECTED_BAR_INVALID" in checker._quality_diagnostic(result)


def test_incomplete_history_blocks_technical_calculation():
    frame = _history(100)
    frame.attrs["ohlcv_quality"] = _quality(
        "INCOMPLETE", latest_bar_valid=False,
        reason_codes=["LATEST_EXPECTED_BAR_INVALID"],
    )
    result = checker.analyze_technical(frame)
    assert result.trend == "Nicht bestimmbar"
    assert result.data_quality.startswith("Datenqualität unvollständig")
    assert "Chartanalyse gesperrt" in result.data_quality


def test_known_ticker_fixtures_route_through_mocked_history_sources(monkeypatch):
    tickers = [
        "VNA.DE", "5LA1.F", "EUNL.DE", "XDEQ.DE", "NRJ.PA", "PPFD.SG",
        "4LL.F", "DRH.F", "LSUG.MI", "SIX2.DE", "SPYI.DE", "ENR.DE",
        "BABA", "ALB", "FCX", "XAU/USD",
    ]
    frame = _history(100)
    frame.attrs["ohlcv_quality"] = _quality()
    _install_db(monkeypatch, frame)
    monkeypatch.setattr(checker, "_fetch_twelvedata_gold_spot_history",
                        lambda: frame.copy())
    monkeypatch.setattr(checker, "_persist_usable_provider_history", lambda *args: len(frame))
    class ForbiddenYahoo:
        def __init__(self, ticker):
            raise AssertionError(f"Yahoo unerwartet für {ticker}")
    monkeypatch.setattr(checker.yf, "Ticker", ForbiddenYahoo, raising=False)

    for ticker in tickers:
        result = checker.fetch_history(ticker)
        assert len(result) == 100, ticker
        assert result.attrs["history_source"] in {
            "historical_ohlcv.sqlite", "TwelveData"
        }, ticker


def test_old_gap_outside_recent_252_sessions_is_preserved_not_compressed():
    frame = _history(400)
    frame.attrs["ohlcv_quality"] = _quality("PASS_WITH_EXCLUSIONS", reason_codes=["OLD_GAP"])
    gap_date = frame.index[50]
    frame.iloc[50, frame.columns.get_loc("High")] = 1.0
    result = checker._valid_ohlc_history(frame)
    assert gap_date in result.index
    assert pd.isna(result.loc[gap_date, "High"])
    assert len(result) == 400


def test_empty_frame_retains_quality_and_source_metadata():
    empty = pd.DataFrame()
    empty.attrs["history_source"] = "Yahoo-Fallback"
    empty.attrs["ohlcv_quality"] = _quality(
        "INCOMPLETE", reason_codes=["NO_DATA"]
    )
    result = checker._valid_ohlc_history(empty)
    assert result.empty
    assert result.attrs["history_source"] == "Yahoo-Fallback"
    assert result.attrs["ohlcv_quality"]["reason_codes"] == ["NO_DATA"]


# Stable regression fixtures for calendar coverage. This list is NOT the
# live open-position universe; that is loaded dynamically from the current-run
# Offene_Positionen.csv written by positionen_tracker.py.
TICKER_CALENDAR_REGRESSION_FIXTURES = {
    "VNA.DE": "XETR",
    "5LA1.F": "XFRA",
    "EUNL.DE": "XETR",
    "XDEQ.DE": "XETR",
    "NRJ.PA": "XPAR",
    "PPFD.SG": "XFRA",  # Yahoo .SG = Stuttgart (STU); XFRA is explicit DE proxy.
    "4LL.F": "XFRA",
    "DRH.F": "XFRA",
    "LSUG.MI": "XMIL",
    "SIX2.DE": "XETR",
    "SPYI.DE": "XETR",
    "ENR.DE": "XETR",
    "BABA": "XNYS",
    "ALB": "XNYS",
    "FCX": "XNYS",
    "XAU/USD": "24/5",
}


@pytest.mark.parametrize("ticker,expected_calendar", TICKER_CALENDAR_REGRESSION_FIXTURES.items())
def test_open_position_ticker_uses_real_calendar_and_quality_gate(
    ticker, expected_calendar, tmp_path
):
    import historical_ohlcv
    from ohlcv_quality_gate import (
        _calendar_sessions,
        _last_completed_session_date,
        apply_ohlcv_quality_gate,
    )

    # Ensure that the production resolver points at the verified expected
    # calendar and that its actual session implementation is loadable.
    calendar_name = historical_ohlcv._calendar_name_for_ticker(ticker)
    assert calendar_name == expected_calendar

    as_of = pd.Timestamp("2026-11-12T23:30:00Z")
    expected_last = _last_completed_session_date(ticker, as_of=as_of)
    assert expected_last is not None, ticker

    start = (as_of - pd.Timedelta(days=180)).date().isoformat()
    end = expected_last
    sessions = _calendar_sessions(ticker, start, end)
    assert sessions is not None and len(sessions) >= 60, ticker
    assert sessions[-1] == expected_last, ticker

    # Use actual exchange session labels, not generic weekdays. The OHLCV
    # values are deterministic synthetic data so this is a calendar/quality
    # contract test, not a live-feed or SQLite-coverage claim.
    index = pd.DatetimeIndex(pd.to_datetime(sessions[-90:]))
    close = pd.Series(np.linspace(100.0, 130.0, len(index)), index=index)
    frame = pd.DataFrame(
        {
            "Open": close - 0.25,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Adj Close": close,
            "Volume": 1000.0,
        },
        index=index,
    )
    quality_registry = tmp_path / "empty_quality_registry.csv"
    quality_registry.write_text(
        "ticker,date,open,high,low,close,volume,source,invalid_reasons,"
        "recent_90d,classification,usage_decision,classification_note,"
        "independent_provider,independent_status,independent_open,"
        "independent_high,independent_low,independent_close,timeframe,"
        "approved_by,evidence_ref,rule_version,allowed_reason_codes\\n",
        encoding="utf-8",
    )
    gap_registry = tmp_path / "empty_gap_registry.csv"
    gap_registry.write_text(
        "ticker,calendar,missing_date,requested_start,requested_end_exclusive,"
        "yahoo_status,returned_row_count,returned_first_date,returned_last_date,"
        "missing_date_returned,yahoo_error,quality_class,usage_decision,"
        "classification_note,timeframe\\n",
        encoding="utf-8",
    )
    gated, quality = apply_ohlcv_quality_gate(
        frame,
        ticker,
        window_sessions=60,
        require_structure=True,
        preserve_expected_sessions=True,
        full_history=True,
        as_of=as_of,
        registry_path=quality_registry,
        gap_registry_path=gap_registry,
    )
    payload = quality.to_dict()
    assert payload["quality_status"] in {"PASS", "PASS_WITH_EXCLUSIONS"}, (
        ticker, payload
    )
    assert payload.get("latest_bar_valid") is True, (ticker, payload)
    assert payload.get("last_3_bars_valid") is True, (ticker, payload)
    assert len(gated.dropna(subset=["Open", "High", "Low", "Close"])) >= 60, ticker


def test_ppfd_sg_is_explicit_stuttgart_listing_proxy_not_generic_sg_suffix():
    import historical_ohlcv

    assert historical_ohlcv._calendar_name_for_ticker("PPFD.SG") == "XFRA"
    assert historical_ohlcv._calendar_name_for_ticker("OTHER.SG") is None


@pytest.mark.parametrize(
    "position_rows,expected_tickers",
    [
        (
            [
                {"Ticker": "AAA.DE", "Name": "Alpha", "Status": "Offen", "Einstiegsdatum": "01.01.2026", "Einstieg": "10", "Stop": "9"},
                {"Ticker": "BBB.N", "Name": "Beta", "Status": " offen ", "Einstiegsdatum": "02.01.2026", "Einstieg": "20", "Stop": "18"},
                {"Ticker": "CLOSED.DE", "Status": "Verkauft", "Einstiegsdatum": "03.01.2026"},
            ],
            ["AAA.DE", "BBB.N"],
        ),
        (
            [
                {"Ticker": "NEW1.DE", "Name": "New One", "Status": "Offen", "Einstiegsdatum": "01.01.2026", "Einstieg": "15", "Stop": "14"},
            ],
            ["NEW1.DE"],
        ),
        (
            [
                {"Ticker": "ANLEITUNG", "Status": "Offen", "Einstiegsdatum": ""},
                {"Ticker": "CLOSED.DE", "Status": "Gestoppt", "Einstiegsdatum": "03.01.2026"},
            ],
            [],
        ),
    ],
)
def test_run_local_uses_dynamic_current_input_universe(
    tmp_path, monkeypatch, position_rows, expected_tickers
):
    """The run-time CSV, not a checked-in ticker fixture, defines the universe."""
    import csv

    input_path = tmp_path / "Offene_Positionen.csv"
    output_path = tmp_path / "Offene Positionen+Check.csv"
    history_path = tmp_path / "Geschlossene Positionen.csv"
    fieldnames = ["Ticker", "Status", "Einstiegsdatum", "Name", "Einstieg", "Stop"]
    with input_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(position_rows)

    seen = []
    monkeypatch.setattr(checker, "extract_closed_history", lambda df: pd.DataFrame())
    monkeypatch.setattr(checker, "load_initial_history", lambda: pd.DataFrame(columns=checker.HISTORY_HEADERS))
    monkeypatch.setattr(checker, "fetch_history", lambda ticker: seen.append(ticker) or pd.DataFrame())
    monkeypatch.setattr(checker, "analyze_technical", lambda hist, row: checker.TechnicalResult())
    monkeypatch.setattr(
        checker, "make_row",
        lambda row, tech: {"Ticker": str(row.get("Ticker", "")).strip()},
    )

    out, _ = checker.run_local(
        input_file=str(input_path),
        output_csv=str(output_path),
        history_csv=str(history_path),
    )

    assert seen == expected_tickers
    assert out["Ticker"].tolist() == expected_tickers


@pytest.mark.parametrize("missing_ticker", ["", None, np.nan, pd.NA])
def test_open_position_with_missing_ticker_fails_closed(missing_ticker):
    frame = pd.DataFrame([
        {"Ticker": missing_ticker, "Status": "Offen", "Einstiegsdatum": "01.01.2026"}
    ])
    with pytest.raises(ValueError, match="ohne Ticker"):
        checker.select_open_positions(frame)


def test_duplicate_open_ticker_positions_are_not_collapsed():
    frame = pd.DataFrame([
        {"Ticker": "AAA.DE", "Status": "Offen"},
        {"Ticker": "AAA.DE", "Status": "Offen"},
    ])
    selected = checker.select_open_positions(frame)
    assert len(selected) == 2
    assert selected["Ticker"].tolist() == ["AAA.DE", "AAA.DE"]


def test_gold_twelvedata_fallback_is_persisted_after_quality_pass(monkeypatch):
    frame = _history(100)
    frame.attrs["ohlcv_quality"] = _quality()
    monkeypatch.setattr(checker, "_fetch_twelvedata_gold_spot_history", lambda: frame.copy())
    monkeypatch.setattr(
        "ohlcv_quality_gate._last_completed_session_date",
        lambda ticker: frame.index.max().date(),
    )
    persisted = []
    monkeypatch.setattr(
        checker, "_persist_usable_provider_history",
        lambda ticker, data, source: persisted.append((ticker, source)) or len(data),
    )

    result = checker.fetch_history("XAU/USD")

    assert len(result) == 100
    assert persisted == [("XAU/USD", "TwelveData")]


def test_provider_fallback_is_not_returned_as_usable_when_persistence_fails(monkeypatch):
    db = _history(99)
    db.attrs["ohlcv_quality"] = _quality()
    yahoo = _history(100)
    monkeypatch.setattr("historical_ohlcv.load_history", lambda ticker: db.copy())
    monkeypatch.setattr(
        "ohlcv_quality_gate._last_completed_session_date",
        lambda ticker: yahoo.index.max().date(),
    )

    class YahooTicker:
        def __init__(self, ticker):
            pass

        def history(self, **kwargs):
            return yahoo.copy()

    class Quality:
        def to_dict(self):
            return _quality()

    monkeypatch.setattr(checker.yf, "Ticker", YahooTicker, raising=False)
    monkeypatch.setattr(
        "ohlcv_quality_gate.apply_ohlcv_quality_gate",
        lambda data, *args, **kwargs: (data.copy(), Quality()),
    )
    monkeypatch.setattr(
        checker, "_persist_usable_provider_history",
        lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    result = checker.fetch_history("TEST.DE")

    assert result.empty
    assert "PERSISTENCE_ERROR:OSError" in result.attrs["history_error"]


def test_yahoo_fallback_persists_full_quality_frame_not_truncated_analysis_tail(monkeypatch):
    db = _history(99)
    db.attrs["ohlcv_quality"] = _quality()
    yahoo = _history(100)
    yahoo.iloc[-70, yahoo.columns.get_loc("High")] = 1.0
    monkeypatch.setattr("historical_ohlcv.load_history", lambda ticker: db.copy())
    monkeypatch.setattr(
        "ohlcv_quality_gate._last_completed_session_date",
        lambda ticker: yahoo.index.max().date(),
    )

    class YahooTicker:
        def __init__(self, ticker):
            pass
        def history(self, **kwargs):
            return yahoo.copy()

    class Quality:
        def to_dict(self):
            return _quality("PASS_WITH_EXCLUSIONS", reason_codes=["ISOLATED_BAD_BAR"])

    persisted = []
    monkeypatch.setattr(checker.yf, "Ticker", YahooTicker, raising=False)
    monkeypatch.setattr(
        "ohlcv_quality_gate.apply_ohlcv_quality_gate",
        lambda data, *args, **kwargs: (data.copy(), Quality()),
    )
    monkeypatch.setattr(checker, "_persist_usable_provider_history",
                        lambda ticker, data, source: persisted.append((len(data), source)) or len(data))

    result = checker.fetch_history("TEST.DE")

    assert len(result) == 69
    assert persisted == [(100, "Yahoo-Fallback")]


def test_twelvedata_persists_full_quality_frame_not_truncated_analysis_tail(monkeypatch):
    frame = _history(100)
    frame.iloc[-70, frame.columns.get_loc("High")] = 1.0
    frame.attrs["ohlcv_quality"] = _quality("PASS_WITH_EXCLUSIONS", reason_codes=["ISOLATED_BAD_BAR"])
    monkeypatch.setattr(checker, "_fetch_twelvedata_gold_spot_history", lambda: frame.copy())
    monkeypatch.setattr(checker, "_quality_is_current_and_usable", lambda analysis_frame, ticker: (True, ""))
    persisted = []
    monkeypatch.setattr(checker, "_persist_usable_provider_history",
                        lambda ticker, data, source: persisted.append((len(data), source)) or len(data))

    result = checker.fetch_history("XAU/USD")

    assert len(result) == 69
    assert persisted == [(100, "TwelveData")]


def test_read_positions_rejects_german_grouped_excel_date_serial_as_stop(tmp_path):
    path = tmp_path / "Offene_Positionen.csv"
    path.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        "AAPL;Apple;Offen;01.11.2026;250,00;46.175,50\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError, match="Excel-Datumsseriennummer"):
        checker.read_positions(str(path))


def test_lsug_mi_stop_is_preserved_in_live_output_csv(tmp_path, monkeypatch):
    source = tmp_path / "Offene_Positionen.csv"
    output = tmp_path / "Offene Positionen+Check.csv"
    history = tmp_path / "Geschlossene Positionen.csv"
    source.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Aktueller_Kurs;Stop;TP1;TP2;Richtung\n"
        "LSUG.MI;WisdomTree Sugar;Offen;18.03.2026;2,06;2,35;2,06;3,00;6,50;Long\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(
        checker, "load_initial_history",
        lambda: pd.DataFrame(columns=checker.HISTORY_HEADERS),
    )
    monkeypatch.setattr(checker, "fetch_history", lambda ticker: pd.DataFrame())
    monkeypatch.setattr(
        checker, "analyze_technical",
        lambda hist, row: checker.TechnicalResult(close=2.35),
    )

    out, closed = checker.run_local(
        input_file=str(source), output_csv=str(output), history_csv=str(history)
    )

    assert "Stop" in checker.HEADERS
    assert "Stop" in checker.NUMERIC_COLUMNS
    assert out.loc[0, "Ticker"] == "LSUG.MI"
    assert out.loc[0, "Einstiegsdatum"] == "18.03.2026"
    assert out.loc[0, "Einstieg"] == pytest.approx(2.06)
    assert out.loc[0, "Stop"] == pytest.approx(2.06)
    assert out.loc[0, "Aktueller_Kurs"] == pytest.approx(2.35)

    source_values = checker.read_positions(str(source)).iloc[0]
    assert source_values["TP1"] == "3,00"
    assert source_values["TP2"] == "6,50"

    saved = pd.read_csv(output, sep=";", decimal=",", encoding="utf-8-sig")
    assert saved.loc[0, "Stop"] == pytest.approx(2.06)
    assert saved.loc[0, "Einstieg"] == pytest.approx(2.06)
    assert saved.loc[0, "Aktueller_Kurs"] == pytest.approx(2.35)
    assert closed.empty


@pytest.mark.parametrize("stop_value", ["", "46.175,00", "ungueltig"])
def test_open_position_rejects_empty_or_invalid_stop_values(tmp_path, stop_value):
    path = tmp_path / "Offene_Positionen.csv"
    path.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        f"LSUG.MI;WisdomTree Sugar;Offen;18.03.2026;2,06;{stop_value}\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError):
        checker.read_positions(str(path))


def test_write_sheet_uploads_stop_value_and_formats_it_as_numeric():
    class _Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class _Values:
        def __init__(self):
            self.update_body = None
        def clear(self, **kwargs):
            return _Request()
        def update(self, **kwargs):
            self.update_body = kwargs["body"]
            return _Request()

    class _Sheets:
        def __init__(self):
            self.value_api = _Values()
            self.batch_body = None
        def get(self, **kwargs):
            return _Request({"sheets": [{"properties": {
                "title": "Offene Positionen+Check", "sheetId": 77
            }}]})
        def values(self):
            return self.value_api
        def batchUpdate(self, **kwargs):
            self.batch_body = kwargs["body"]
            return _Request()

    class _Service:
        def __init__(self):
            self.api = _Sheets()
        def spreadsheets(self):
            return self.api

    service = _Service()
    values = [
        ["Offene Positionen+Check | Test"] + [""] * (len(checker.HEADERS) - 1),
        list(checker.HEADERS),
        ["LSUG.MI", "WisdomTree Sugar", "", "", "", "", "Long", "Offen",
         "18.03.2026", 2.06, 2.06, 2.35] + [""] * (len(checker.HEADERS) - 12),
    ]
    checker._write_sheet(
        service, "spreadsheet-id", "Offene Positionen+Check", values,
        {"Stop": 85},
    )

    uploaded = service.api.value_api.update_body["values"]
    stop_index = checker.HEADERS.index("Stop")
    assert uploaded[1][stop_index] == "Stop"
    assert uploaded[2][stop_index] == pytest.approx(2.06)

    requests = service.api.batch_body["requests"]
    stop_width = [
        request["updateDimensionProperties"]
        for request in requests
        if "updateDimensionProperties" in request
        and request["updateDimensionProperties"]["range"]["dimension"] == "COLUMNS"
        and request["updateDimensionProperties"]["range"]["startIndex"] == stop_index
    ]
    assert stop_width
    assert stop_width[0]["properties"]["pixelSize"] == 85

    stop_format = [
        request for request in requests
        if request.get("repeatCell", {}).get("range", {}).get("startColumnIndex") == stop_index
        and request.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {}).get("numberFormat")
    ]
    assert stop_format
    assert stop_format[0]["repeatCell"]["cell"]["userEnteredFormat"]["numberFormat"]["pattern"] == "0.00"


def test_write_sheet_formats_all_position_dates_left_aligned():
    class _Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class _Values:
        def clear(self, **kwargs):
            return _Request()
        def update(self, **kwargs):
            return _Request()

    class _Sheets:
        def __init__(self):
            self.batch_body = None
        def get(self, **kwargs):
            return _Request({"sheets": [{"properties": {
                "title": "Geschlossene Positionen", "sheetId": 78
            }}]})
        def values(self):
            return _Values()
        def batchUpdate(self, **kwargs):
            self.batch_body = kwargs["body"]
            return _Request()

    class _Service:
        def __init__(self):
            self.api = _Sheets()
        def spreadsheets(self):
            return self.api

    service = _Service()
    headers = checker.HISTORY_DISPLAY_HEADERS
    values = [
        ["Geschlossene Positionen | Test"] + [""] * (len(headers) - 1),
        headers,
        ["XAU/USD", "Gold Spot", "", "", "", "Long", "Manuell",
         "04.11.2026", 4037.0, 3790.03, 3800.0, 4185.0, 4270.0,
         "Gestoppt", "18.11.2026", 3790.03, -6.12] + [""] * (len(headers) - 17),
    ]
    checker._write_sheet(
        service, "spreadsheet-id", "Geschlossene Positionen", values,
        {"Einstiegsdatum": 110, "Ausstiegsdatum": 110}, 2,
        target_title="Geschlossene Positionen",
    )

    requests = service.api.batch_body["requests"]
    for column in ("Einstiegsdatum", "Ausstiegsdatum"):
        index = headers.index(column)
        matching = [
            item["repeatCell"] for item in requests
            if item.get("repeatCell", {}).get("range", {}).get("startColumnIndex") == index
            and "horizontalAlignment" in item.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {})
        ]
        assert matching
        assert matching[0]["cell"]["userEnteredFormat"]["horizontalAlignment"] == "LEFT"
        assert matching[0]["cell"]["userEnteredFormat"]["numberFormat"]["pattern"] == "dd.MM.yyyy"


def test_make_row_uses_tracker_current_price_for_visible_price_and_performance_without_relaxing_quality_gate():
    tech = checker.TechnicalResult(
        close=None,
        data_quality="INCOMPLETE (LAST_3_EXPECTED_BARS_INVALID)",
        note="Technische Analyse wegen Quality-Gate gesperrt.",
    )
    row = {
        "Ticker": "NRJ.PA",
        "Name": "Test ETF",
        "Ideen_Quelle": "Manuell",
        "Markt": "EU",
        "Waehrung": "EUR",
        "Status": "Offen",
        "Einstiegsdatum": "03.02.2026",
        "Einstieg": 38.04,
        "Stop": 37.0,
        "Aktueller_Kurs": 37.90,
        "Richtung": "Long",
    }

    result = checker.make_row(row, tech)

    assert result["Aktueller_Kurs"] == 37.90
    assert result["Performance_Seit_Einstieg%"] == -0.37
    assert result["Datenqualitaet"] == tech.data_quality
    assert result["Support_1"] == ""
    assert result["Widerstand_1"] == ""


def test_closed_history_sorts_oldest_first_and_numbers_contiguously():
    frame = pd.DataFrame([
        {"Ticker": "NEW", "Ausstiegsdatum": "16.11.2026"},
        {"Ticker": "OLD", "Ausstiegsdatum": "01.02.2026"},
        {"Ticker": "MID", "Ausstiegsdatum": "12.08.2026"},
        {"Ticker": "UNKNOWN", "Ausstiegsdatum": ""},
    ], columns=checker.HISTORY_HEADERS)

    ordered = checker.sort_closed_history_by_exit_date(frame)
    numbered = checker.numbered_closed_history_rows(frame)

    assert ordered["Ticker"].tolist() == ["UNKNOWN", "OLD", "MID", "NEW"]
    assert [row[checker.HISTORY_HEADERS.index("Ticker")] for row in numbered] == [
        "UNKNOWN", "OLD", "MID", "NEW"
    ]
    assert [row[-1] for row in numbered] == [1, 2, 3, 4]
    assert checker.HISTORY_DISPLAY_HEADERS == checker.HISTORY_HEADERS + ["Nr."]


def test_local_closed_history_sort_uses_exit_ticker_entry_order():
    frame = pd.DataFrame([
        {"Ticker": "ZETA", "Ausstiegsdatum": "02.02.2026", "Einstiegsdatum": "01.01.2026"},
        {"Ticker": "BETA", "Ausstiegsdatum": "01.02.2026", "Einstiegsdatum": "01.01.2026"},
        {"Ticker": "ALPHA", "Ausstiegsdatum": "01.02.2026", "Einstiegsdatum": "02.01.2026"},
        {"Ticker": "ALPHA", "Ausstiegsdatum": "01.02.2026", "Einstiegsdatum": "01.01.2026"},
    ])
    ordered = checker.sort_closed_history_by_exit_date(frame)
    assert list(zip(ordered["Ausstiegsdatum"], ordered["Ticker"], ordered["Einstiegsdatum"])) == [
        ("01.02.2026", "ALPHA", "01.01.2026"),
        ("01.02.2026", "ALPHA", "02.01.2026"),
        ("01.02.2026", "BETA", "01.01.2026"),
        ("02.02.2026", "ZETA", "01.01.2026"),
    ]


def test_merge_closed_history_ignores_display_number_column():
    history_item = {column: "" for column in checker.HISTORY_HEADERS}
    history_item.update({
        "Ticker": "OLD", "Name": "Old Company", "Einstieg": "10.00",
        "Einstiegsdatum": "01.01.2026", "Ausstiegsdatum": "02.01.2026",
        "Ausstiegskurs": "11.00", "Richtung": "Long",
        "Performance_Seit_Einstieg%": "10.00",
    })
    history_row = [history_item[column] for column in checker.HISTORY_HEADERS]
    existing = [
        ["Geschlossene Positionen | Stand"],
        checker.HISTORY_DISPLAY_HEADERS,
        history_row + [1],
    ]

    merged = checker.merge_closed_history(
        existing, pd.DataFrame(columns=checker.HISTORY_HEADERS)
    )

    assert len(merged) == 1
    assert merged.iloc[0]["Ticker"] == "OLD"
    assert list(merged.columns) == checker.HISTORY_HEADERS


def test_27_column_display_header_is_stripped_for_archive_and_recovery_contract():
    history_item = {column: "" for column in checker.HISTORY_HEADERS}
    history_item.update({
        "Ticker": "OLD", "Name": "Legacy", "Status": "Verkauft",
        "Einstieg": "10.00", "Einstiegsdatum": "01.01.2026",
        "Ausstiegsdatum": "02.01.2026", "Ausstiegskurs": "11.00",
        "Richtung": "Long",
    })
    display_rows = [
        ["Geschlossene Positionen | Stand"],
        checker.HISTORY_DISPLAY_HEADERS,
        [history_item[column] for column in checker.HISTORY_HEADERS] + [17],
    ]

    # Archivierungs-Merge und Recovery-Einlesung müssen Nr. verwerfen und
    # exakt den kanonischen A:Z-Vertrag zurückgeben.
    merged = checker.merge_closed_history(
        display_rows, pd.DataFrame(columns=checker.HISTORY_HEADERS)
    )
    assert list(merged.columns) == checker.HISTORY_HEADERS
    assert len(merged) == 1
    assert merged.iloc[0]["Ticker"] == "OLD"
    assert "Nr." not in merged.columns
    assert merged.iloc[0]["Ausstiegsdatum"] == "02.01.2026"


def test_archive_append_accepts_27_column_existing_header_and_writes_canonical_26_columns(monkeypatch):
    existing = {column: "" for column in checker.HISTORY_HEADERS}
    existing.update({
        "Ticker": "OLD", "Name": "Existing", "Status": "Verkauft",
        "Einstieg": "10.00", "Einstiegsdatum": "01.01.2026",
        "Ausstiegsdatum": "02.01.2026", "Ausstiegskurs": "11.00",
        "Richtung": "Long",
    })
    new = {column: "" for column in checker.HISTORY_HEADERS}
    new.update({
        "Ticker": "NEW", "Name": "New", "Status": "Gestoppt",
        "Einstieg": "20.00", "Einstiegsdatum": "03.01.2026",
        "Ausstiegsdatum": "04.01.2026", "Ausstiegskurs": "19.00",
        "Performance_Seit_Einstieg%": "-5.00", "Richtung": "Long",
    })
    existing_values = [
        ["Geschlossene Positionen | Stand"],
        checker.HISTORY_DISPLAY_HEADERS,
        [existing[column] for column in checker.HISTORY_HEADERS] + [12],
    ]
    calls = {"append": []}

    class Request:
        def __init__(self, result=None): self.result = result or {}
        def execute(self): return self.result

    class Values:
        def get(self, **kwargs): return Request({"values": existing_values})
        def append(self, **kwargs):
            calls["append"].append(kwargs)
            return Request({"updates": {"updatedRows": 1}})

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 88, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 27},
            }}]})
        def values(self): return Values()

    class Sheets:
        def spreadsheets(self): return Spreadsheets()

    monkeypatch.setattr(checker, "_format_history_range", lambda *args, **kwargs: None)
    appended = checker._append_missing_history_rows(
        Sheets(), "spreadsheet-id",
        pd.DataFrame([new], columns=checker.HISTORY_HEADERS),
        allow_initialize=True,
    )

    assert appended == 1
    assert len(calls["append"]) == 1
    payload = calls["append"][0]
    assert payload["range"] == "'Geschlossene Positionen'!A:Z"
    assert len(payload["body"]["values"][0]) == len(checker.HISTORY_HEADERS) == 26
    assert payload["body"]["values"][0][checker.HISTORY_HEADERS.index("Ticker")] == "NEW"
    assert "Nr." not in checker.HISTORY_HEADERS


def test_27_column_header_is_normalized_by_initial_archive_recovery(tmp_path, monkeypatch):
    # Der Repo-Initialbestand darf die sichtbare Nr.-Spalte enthalten, muss
    # beim Recovery-Import aber wieder auf den kanonischen A:Z-Vertrag fallen.
    import csv
    row = {column: "" for column in checker.HISTORY_HEADERS}
    row.update({
        "Ticker": "RECOVERY", "Name": "Recovery Test", "Status": "Verkauft",
        "Einstiegsdatum": "01.01.2026", "Einstieg": "10.00",
        "Ausstiegsdatum": "02.01.2026", "Ausstiegskurs": "11.00",
    })
    path = tmp_path / "history.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=checker.HISTORY_DISPLAY_HEADERS, delimiter=";"
        )
        writer.writeheader()
        writer.writerow({**row, "Nr.": "99"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checker.os, "getenv", lambda *_args, **_kwargs: "")
    recovered = checker.load_initial_history(str(path))
    assert list(recovered.columns) == checker.HISTORY_HEADERS
    assert recovered.loc[recovered.index[0], "Ticker"] == "RECOVERY"
    assert "Nr." not in recovered.columns


def test_upsert_preflight_accepts_existing_27_column_history_header(monkeypatch):
    class StopAfterPreflight(RuntimeError):
        pass

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class SpreadsheetResource:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
            }}]})
        def batchUpdate(self, **kwargs):
            raise StopAfterPreflight("Preflight bestanden; erster Schreibzugriff erreicht")

    class Sheets:
        def spreadsheets(self):
            return SpreadsheetResource()

    sheets = Sheets()
    monkeypatch.setattr(checker, "_validate_output_before_upload", lambda *args: None)
    monkeypatch.setattr(checker, "build", lambda service, version, credentials: sheets, raising=False)
    monkeypatch.setattr(
        checker, "_find_or_initialize_master",
        lambda drive, closed_df: ("spreadsheet-id", False, None),
    )
    history_rows = [["Titel"], checker.HISTORY_DISPLAY_HEADERS]
    for number in range(51):
        row = {column: "" for column in checker.HISTORY_HEADERS}
        row.update({
            "Name": f"Unternehmen {number}",
            "Ticker": f"AAA{number:02d}",
            "Einstieg": "100",
            "Einstiegsdatum": f"{(number % 28) + 1:02d}.01.{2025 + number // 28}",
        })
        history_rows.append([row[column] for column in checker.HISTORY_HEADERS] + [number + 1])
    monkeypatch.setattr(checker, "read_existing_history", lambda *args, **kwargs: history_rows)

    with pytest.raises(StopAfterPreflight, match="Preflight bestanden"):
        checker.upsert_google_sheet(
            pd.DataFrame(columns=checker.HEADERS),
            pd.DataFrame(columns=checker.HISTORY_HEADERS),
            creds=object(),
        )


def test_twelvedata_ppfd_accepts_only_verified_xstu_eur_metadata(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "meta": {
                    "symbol": "PPFD", "mic_code": "XSTU", "currency": "EUR",
                    "exchange_timezone": "Europe/Berlin",
                },
                "values": [
                    {"datetime": "2026-11-18", "open": "39.0", "high": "40.0",
                     "low": "38.5", "close": "39.5", "volume": "100"},
                    {"datetime": "2026-11-19", "open": "39.5", "high": "41.0",
                     "low": "39.0", "close": "40.5", "volume": "120"},
                ],
                "status": "ok",
            }

    captured = {}
    def fake_get(url, params, **kwargs):
        captured.update(params)
        return Response()

    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-key-not-real")
    monkeypatch.setattr(checker.requests, "get", fake_get)
    monkeypatch.setattr(checker.time, "sleep", lambda *_: None)
    result = checker._fetch_twelvedata_ppfd_history()
    assert captured["symbol"] == "PPFD"
    assert captured["mic_code"] == "XSTU"
    assert captured["interval"] == "1day"
    assert list(result.index.strftime("%Y-%m-%d")) == ["2026-11-18", "2026-11-19"]
    assert result.attrs["history_source"] == "TwelveData:XSTU"
    assert result.attrs["provider_metadata"]["currency"] == "EUR"


def test_twelvedata_ppfd_rejects_wrong_exchange_metadata(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "meta": {"symbol": "PPFD", "mic_code": "XFRA", "currency": "EUR"},
                "values": [{"datetime": "2026-11-19", "open": "39", "high": "40",
                            "low": "38", "close": "39.5"}],
                "status": "ok",
            }

    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-key-not-real")
    monkeypatch.setattr(checker.requests, "get", lambda *a, **k: Response())
    with pytest.raises(RuntimeError, match="erwartet PPFD/XSTU/EUR"):
        checker._fetch_twelvedata_ppfd_history()


def test_twelvedata_ppfd_requires_existing_secret(monkeypatch):
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TWELVEDATA_API_KEY fehlt"):
        checker._fetch_twelvedata_ppfd_history()


def test_fetch_history_uses_twelvedata_ppfd_after_stale_sqlite_and_persists_only_after_quality_pass(monkeypatch):
    stale = _history(100)
    stale.attrs["ohlcv_quality"] = _quality(
        "INCOMPLETE", latest_bar_valid=False, reason_codes=["STALE_HISTORY"]
    )
    _install_db(monkeypatch, stale)
    provider = _history(100)
    provider.attrs["history_source"] = "TwelveData:XSTU"
    monkeypatch.setattr(checker, "_fetch_twelvedata_ppfd_history", lambda: provider.copy())

    class Quality:
        def to_dict(self):
            return _quality()

    monkeypatch.setattr(
        "ohlcv_quality_gate.apply_ohlcv_quality_gate",
        lambda data, *args, **kwargs: (data.copy(), Quality()),
    )
    quality_calls = {"count": 0}
    def quality_check(frame, ticker):
        quality_calls["count"] += 1
        if quality_calls["count"] == 1:
            return False, "STALE_HISTORY"
        return True, ""
    monkeypatch.setattr(checker, "_quality_is_current_and_usable", quality_check)
    persisted = []
    monkeypatch.setattr(
        checker, "_persist_usable_provider_history",
        lambda ticker, frame, source: persisted.append((ticker, source)) or len(frame),
    )
    class ForbiddenYahoo:
        def __init__(self, ticker):
            raise AssertionError("Bei bestandenem Twelve-Data-Gate darf Yahoo nicht laufen.")
    monkeypatch.setattr(checker.yf, "Ticker", ForbiddenYahoo, raising=False)

    result = checker.fetch_history("PPFD.SG")
    assert len(result) == 100
    assert result.attrs["history_source"] == "TwelveData:XSTU"
    assert persisted == [("PPFD.SG", "TwelveData:XSTU")]


@pytest.mark.parametrize("appended_history", [0, 1], ids=["ohne-neue-archivzeilen", "mit-neuen-archivzeilen"])
@pytest.mark.parametrize("timestamp_fails", [False, True], ids=["zeitstempel-ok", "zeitstempel-fehler"])
@pytest.mark.parametrize("is_new_master", [False, True], ids=["bestehender-master", "master-erstinitialisierung"])
def test_upsert_handles_timestamp_result_without_rolling_back_verified_tab1(
    monkeypatch, tmp_path, appended_history, timestamp_fails, is_new_master
):
    stale_title = "Geschlossene Positionen | historische Faktenbasis | Stand 01.01.2026 00:00"
    state = {
        "a1": stale_title, "updates": [], "enforce_called": False,
        "rollback_calls": [], "finalize_calls": [], "quarantine_calls": [],
    }

    archive_rows = []
    for number in range(checker.MINIMUM_PROTECTED_HISTORY_ROWS + 1):
        row = {column: "" for column in checker.HISTORY_HEADERS}
        row.update({
            "Name": f"Unternehmen {number}",
            "Ticker": f"AAA{number:02d}",
            "Einstieg": str(100 + number),
            "Einstiegsdatum": f"{(number % 28) + 1:02d}.01.{2025 + number // 28}",
            "Ausstiegsdatum": "02.02.2026",
            "Ausstiegskurs": str(101 + number),
            "Status": "Verkauft",
            "Richtung": "Long",
            "Performance_Seit_Einstieg%": round((101 + number - (100 + number)) / (100 + number) * 100, 2),
        })
        archive_rows.append(row)

    preflight_rows = [
        [stale_title],
        checker.HISTORY_DISPLAY_HEADERS,
    ]
    preflight_rows.extend(
        [row[column] for column in checker.HISTORY_HEADERS] + [index + 1]
        for index, row in enumerate(archive_rows[:checker.MINIMUM_PROTECTED_HISTORY_ROWS])
    )
    history_rows = list(preflight_rows)
    if appended_history:
        history_rows.append(
            [archive_rows[checker.MINIMUM_PROTECTED_HISTORY_ROWS][column]
             for column in checker.HISTORY_HEADERS]
            + [checker.MINIMUM_PROTECTED_HISTORY_ROWS + 1]
        )
    closed_df = (
        pd.DataFrame(
            [archive_rows[checker.MINIMUM_PROTECTED_HISTORY_ROWS]],
            columns=checker.HISTORY_HEADERS,
        )
        if appended_history
        else pd.DataFrame(columns=checker.HISTORY_HEADERS)
    )
    history_snapshot = [list(row) for row in history_rows]

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class Values:
        def get(self, **kwargs):
            requested_range = kwargs["range"]
            if requested_range == "'Geschlossene Positionen'!A1":
                return Request({"values": [[state["a1"]]]})
            if requested_range in {"Geschlossene Positionen!A1:Z", "Geschlossene Positionen!A1:AA"}:
                return Request({"values": history_rows})
            if requested_range == "'Offene Positionen+Check'!A:ZZZ":
                return Request({"values": [["Offene Positionen+Check | Stand"], checker.HEADERS]})
            raise AssertionError(f"Unerwarteter Google-Sheets-Read: {requested_range}")

        def update(self, **kwargs):
            state["updates"].append(kwargs)
            if kwargs["range"] == "'Geschlossene Positionen'!A1":
                state["a1"] = kwargs["body"]["values"][0][0]
            return Request()

        def clear(self, **kwargs):
            raise AssertionError("A1-Clear ist in diesem erfolgreichen Pfad nicht erwartet.")

    class SpreadsheetResource:
        def get(self, **kwargs):
            return Request({"sheets": [
                {"properties": {"title": "Offene Positionen+Check", "sheetId": 1}},
                {"properties": {"title": "Geschlossene Positionen", "sheetId": 2}},
            ]})
        def batchUpdate(self, **kwargs):
            return Request()
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return SpreadsheetResource()

    sheets = Sheets()
    monkeypatch.setattr(checker, "_validate_output_before_upload", lambda *args: None)
    monkeypatch.setattr(checker, "build", lambda *args, **kwargs: sheets, raising=False)
    monkeypatch.setattr(
        checker, "_find_or_initialize_master",
        lambda drive, current_closed: (
            "spreadsheet-id", is_new_master, "INIT-name" if is_new_master else None
        ),
    )
    monkeypatch.setattr(checker, "read_existing_history", lambda *args, **kwargs: preflight_rows)
    monkeypatch.setattr(checker, "_snapshot_google_sheet", lambda *args, **kwargs: "backup-id")
    monkeypatch.setattr(checker, "_write_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        checker, "_verify_open_tab_full_content",
        lambda *args, **kwargs: [["Offene Positionen+Check | Stand"], checker.HEADERS],
    )
    monkeypatch.setattr(checker, "_verify_tabs_after_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr(checker, "_verify_history_full_content", lambda *args, **kwargs: [])
    monkeypatch.setattr(checker, "_append_missing_history_rows", lambda *args, **kwargs: appended_history)
    monkeypatch.setattr(checker, "_sort_and_number_closed_history", lambda *args, **kwargs: len(archive_rows))
    monkeypatch.setattr(
        checker, "_swap_temp_tabs",
        lambda *args, **kwargs: {"Offene Positionen+Check": "__BACKUP_OFFEN__"},
    )
    monkeypatch.setattr(
        checker, "_rollback_productive_swap",
        lambda *args, **kwargs: state["rollback_calls"].append(args),
    )
    monkeypatch.setattr(
        checker, "_finalize_new_master",
        lambda *args, **kwargs: state["finalize_calls"].append(args),
    )
    monkeypatch.setattr(
        checker, "_quarantine_finalized_new_master",
        lambda *args, **kwargs: state["quarantine_calls"].append(args) or "INIT-FAILED",
    )
    if timestamp_fails:
        monkeypatch.setattr(
            checker, "_update_closed_history_timestamp",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulierter A1-Fehler")),
        )
    def enforce_order(*args, **kwargs):
        assert state["a1"] == stale_title
        state["enforce_called"] = True
    monkeypatch.setattr(checker, "_enforce_productive_tab_order", enforce_order)
    monkeypatch.chdir(tmp_path)

    call = lambda: checker.upsert_google_sheet(
        pd.DataFrame(columns=checker.HEADERS),
        closed_df,
        creds=object(),
    )
    if timestamp_fails:
        with pytest.raises(
            RuntimeError,
            match="Google-Sheet-Daten wurden erfolgreich aktualisiert und verifiziert",
        ):
            call()
        assert state["a1"] == stale_title
        assert state["rollback_calls"] == []
        if is_new_master:
            assert len(state["finalize_calls"]) == 1
            assert len(state["quarantine_calls"]) == 1
        else:
            assert state["finalize_calls"] == []
            assert state["quarantine_calls"] == []
    else:
        result = call()
        assert result == "spreadsheet-id"
        assert state["a1"].startswith("Geschlossene Positionen | historische Faktenbasis | Stand ")
        assert state["a1"] != stale_title
        assert state["rollback_calls"] == []
        assert state["quarantine_calls"] == []
        assert len(state["finalize_calls"]) == (1 if is_new_master else 0)

    assert state["enforce_called"] is True
    # Only the intended A1 timestamp may change; every archived data cell stays intact.
    assert history_rows[1:] == history_snapshot[1:]
    if timestamp_fails:
        assert history_rows[0] == history_snapshot[0]
    else:
        assert history_rows[0][0] == state["a1"]
    if not timestamp_fails:
        assert len(state["updates"]) == 1
        assert state["updates"][0]["range"] == "'Geschlossene Positionen'!A1"
        assert state["updates"][0]["body"]["values"] == [[state["a1"]]]
        assert state["updates"][0]["valueInputOption"] == "RAW"


@pytest.mark.parametrize(
    "history_integrity_fails,tab_order_fails,extra_column_fails",
    [
        (False, False, False),
        (False, True, False),
        (False, False, True),
        (True, False, False),
    ],
    ids=["erfolg", "tab-reihenfolge-fehler", "unerwartete-zusatzspalte", "historie-inkonsistent"],
)
def test_upsert_stages_drive_readback_until_all_google_checks_pass(
    monkeypatch, tmp_path, history_integrity_fails, tab_order_fails, extra_column_fails
):
    """Drive-Readback liest volle Sheet-Breite und publiziert CSV erst nach den finalen Gates."""
    state = {
        "history_reads": 0, "swap_calls": 0, "rollback_calls": 0,
        "open_read_range": None, "tab_order_calls": 0,
    }
    archive_rows = []
    for number in range(checker.MINIMUM_PROTECTED_HISTORY_ROWS):
        row = {column: "" for column in checker.HISTORY_HEADERS}
        row.update({"Name": f"Firma {number}", "Ticker": f"INT{number:03d}",
                    "Einstieg": str(100 + number), "Einstiegsdatum": "01.01.2025",
                    "Ausstiegsdatum": "02.02.2026", "Ausstiegskurs": str(101 + number),
                    "Status": "Verkauft"})
        archive_rows.append(row)
    history_rows = [["Geschlossene Positionen | Stand"], checker.HISTORY_DISPLAY_HEADERS]
    history_rows.extend([row[column] for column in checker.HISTORY_HEADERS] + [i + 1]
                        for i, row in enumerate(archive_rows))

    open_row = [""] * len(checker.HEADERS)
    open_row[checker.HEADERS.index("Ticker")] = "AAA"
    open_row[checker.HEADERS.index("Status")] = "Offen"
    open_row[checker.HEADERS.index("Richtung")] = "Long"
    open_rows = [["Offene Positionen+Check | Stand"], list(checker.HEADERS), open_row.copy()]
    if extra_column_fails:
        open_rows[2].append("UNEXPECTED_DATA")

    class Request:
        def __init__(self, result=None): self.result = result or {}
        def execute(self): return self.result
    class Values:
        def get(self, **kwargs):
            requested_range = kwargs["range"]
            if requested_range == "Geschlossene Positionen!A1:AA":
                state["history_reads"] += 1
                rows = [list(row) for row in history_rows]
                if history_integrity_fails: rows[1] = ["FALSCHER_HEADER"]
                return Request({"values": rows})
            if requested_range == "'Offene Positionen+Check'!A:ZZZ":
                state["open_read_range"] = requested_range
                return Request({"values": [list(row) for row in open_rows]})
            raise AssertionError(f"Unerwarteter Google-Sheets-Read: {requested_range}")
    class SpreadsheetResource:
        def get(self, **kwargs):
            return Request({"sheets": [
                {"properties": {"title": "Offene Positionen+Check", "sheetId": 1}},
                {"properties": {"title": "Geschlossene Positionen", "sheetId": 2}},
            ]})
        def batchUpdate(self, **kwargs): return Request()
        def values(self): return Values()
    class Sheets:
        def spreadsheets(self): return SpreadsheetResource()

    sheets = Sheets()
    monkeypatch.setattr(checker, "_validate_output_before_upload", lambda *args: None)
    monkeypatch.setattr(checker, "build", lambda *args, **kwargs: sheets, raising=False)
    monkeypatch.setattr(checker, "_find_or_initialize_master", lambda *args: ("spreadsheet-id", False, None))
    monkeypatch.setattr(checker, "read_existing_history", lambda *args, **kwargs: history_rows)
    monkeypatch.setattr(checker, "_snapshot_google_sheet", lambda *args, **kwargs: "backup-id")
    monkeypatch.setattr(checker, "_write_sheet", lambda *args, **kwargs: None)
    def _fake_full_open_verification(*args, **kwargs):
        state["open_read_range"] = "'Offene Positionen+Check'!A:ZZZ"
        return open_rows
    monkeypatch.setattr(checker, "_verify_open_tab_full_content", _fake_full_open_verification)
    monkeypatch.setattr(checker, "_verify_tabs_after_cleanup", lambda *args, **kwargs: None)
    monkeypatch.setattr(checker, "_verify_history_full_content", lambda *args, **kwargs: [])
    monkeypatch.setattr(checker, "_append_missing_history_rows", lambda *args, **kwargs: 0)
    monkeypatch.setattr(checker, "_sort_and_number_closed_history", lambda *args, **kwargs: len(archive_rows))
    def swap(*args, **kwargs):
        state["swap_calls"] += 1
        return {"Offene Positionen+Check": "__BACKUP_OFFEN__"}
    monkeypatch.setattr(checker, "_swap_temp_tabs", swap)
    monkeypatch.setattr(checker, "_rollback_productive_swap", lambda *args, **kwargs: state.__setitem__("rollback_calls", state["rollback_calls"] + 1))
    def enforce(*args, **kwargs):
        state["tab_order_calls"] += 1
        if tab_order_fails:
            raise RuntimeError("simulierter Tab-Reihenfolgefehler")
    monkeypatch.setattr(checker, "_enforce_productive_tab_order", enforce)
    monkeypatch.setattr(checker, "_update_closed_history_timestamp", lambda *args, **kwargs: None)
    monkeypatch.setattr(checker, "_finalize_new_master", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("KEEP;OLD;LOCAL\n", encoding="utf-8")
    open_df = pd.DataFrame([open_row], columns=checker.HEADERS)
    closed_df = pd.DataFrame(columns=checker.HISTORY_HEADERS)

    if history_integrity_fails:
        with pytest.raises(RuntimeError, match="Header der Historie stimmt nicht"):
            checker.upsert_google_sheet(open_df, closed_df, creds=object())
    elif tab_order_fails:
        with pytest.raises(RuntimeError, match="Tab-Reihenfolgefehler"):
            checker.upsert_google_sheet(open_df, closed_df, creds=object())
    elif extra_column_fails:
        with pytest.raises(RuntimeError, match="außerhalb des erwarteten Headers"):
            checker.upsert_google_sheet(open_df, closed_df, creds=object())
    else:
        result = checker.upsert_google_sheet(open_df, closed_df, creds=object())
        assert result == "spreadsheet-id"

    assert state["history_reads"] == 1
    if history_integrity_fails:
        assert state["swap_calls"] == 0
        assert state["open_read_range"] is None
        assert target.read_text(encoding="utf-8") == "KEEP;OLD;LOCAL\n"
    else:
        assert state["swap_calls"] == 1
        assert state["open_read_range"] == "'Offene Positionen+Check'!A:ZZZ"
        if tab_order_fails or extra_column_fails:
            assert state["rollback_calls"] == 1
            assert target.read_text(encoding="utf-8") == "KEEP;OLD;LOCAL\n"
            assert not list(tmp_path.glob(checker.OUTPUT_CSV + ".staged.*.tmp"))
        else:
            assert state["rollback_calls"] == 0
            with open(target, "r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle, delimiter=";"))
            assert rows[0] == checker.HEADERS
            assert rows[1][checker.HEADERS.index("Ticker")] == "AAA"
    assert state["tab_order_calls"] == (0 if history_integrity_fails or extra_column_fails else 1)


def test_closed_history_timestamp_restores_previous_a1_when_readback_mismatches(monkeypatch):
    previous_title = "Geschlossene Positionen | historische Faktenbasis | Stand 01.01.2026 00:00"
    state = {"a1": previous_title, "writes": []}

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Values:
        def get(self, **kwargs):
            def result():
                # First get is the previous value; second get simulates a stale/wrong read-back.
                if len(state["writes"]) == 0:
                    return {"values": [[state["a1"]]]}
                return {"values": [["unerwarteter Wert"]]}
            return Request(result)
        def update(self, **kwargs):
            def result():
                value = kwargs["body"]["values"][0][0]
                state["writes"].append(value)
                state["a1"] = value
                return {}
            return Request(result)
        def clear(self, **kwargs):
            raise AssertionError("Clear ist bei vorhandenem vorherigem A1-Wert nicht erwartet.")

    class Sheets:
        def spreadsheets(self):
            return self
        def values(self):
            return Values()

    with pytest.raises(RuntimeError, match="nicht zuverlässig aktualisiert"):
        checker._update_closed_history_timestamp(Sheets(), "spreadsheet-id")

    assert len(state["writes"]) == 2
    assert state["writes"][1] == previous_title
    assert state["a1"] == previous_title


def test_closed_history_timestamp_reports_restore_failure(monkeypatch):
    previous_title = "Alter Titel"
    writes = []

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Values:
        def get(self, **kwargs):
            def result():
                if not writes:
                    return {"values": [[previous_title]]}
                return {"values": [["falscher Read-back"]]}
            return Request(result)
        def update(self, **kwargs):
            def result():
                writes.append(kwargs["body"]["values"][0][0])
                if len(writes) == 2:
                    raise RuntimeError("simulierter Restore-Fehler")
                return {}
            return Request(result)
        def clear(self, **kwargs):
            raise AssertionError("Clear nicht erwartet.")

    class Sheets:
        def spreadsheets(self):
            return self
        def values(self):
            return Values()

    with pytest.raises(RuntimeError, match="Wiederherstellung des vorherigen A1-Werts fehlgeschlagen"):
        checker._update_closed_history_timestamp(Sheets(), "spreadsheet-id")

    assert len(writes) == 2
    assert writes[1] == previous_title


def test_closed_history_timestamp_initial_a1_read_failure_does_not_write(monkeypatch):
    state = {"updates": 0, "clears": 0}

    class Request:
        def execute(self):
            raise RuntimeError("A1 initial read fehlgeschlagen")

    class Values:
        def get(self, **kwargs):
            return Request()
        def update(self, **kwargs):
            state["updates"] += 1
            return Request()
        def clear(self, **kwargs):
            state["clears"] += 1
            return Request()

    class Sheets:
        def spreadsheets(self):
            return self
        def values(self):
            return Values()

    with pytest.raises(RuntimeError, match="A1 initial read fehlgeschlagen"):
        checker._update_closed_history_timestamp(Sheets(), "spreadsheet-id")

    assert state == {"updates": 0, "clears": 0}


def test_closed_history_timestamp_update_failure_restores_previous_a1(monkeypatch):
    previous_title = "Alter Tab-2-Zeitstempel"
    state = {"a1": previous_title, "updates": []}

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Values:
        def get(self, **kwargs):
            return Request(lambda: {"values": [[state["a1"]]]})
        def update(self, **kwargs):
            def execute():
                value = kwargs["body"]["values"][0][0]
                state["updates"].append(value)
                if len(state["updates"]) == 1:
                    raise RuntimeError("A1 write fehlgeschlagen")
                state["a1"] = value
                return {}
            return Request(execute)
        def clear(self, **kwargs):
            raise AssertionError("Bei vorhandenem vorherigem A1-Wert nicht erwartet.")

    class Sheets:
        def spreadsheets(self):
            return self
        def values(self):
            return Values()

    with pytest.raises(RuntimeError, match="nicht zuverlässig aktualisiert"):
        checker._update_closed_history_timestamp(Sheets(), "spreadsheet-id")

    assert len(state["updates"]) == 2
    assert state["updates"][1] == previous_title
    assert state["a1"] == previous_title


def test_closed_history_timestamp_clears_a1_when_previous_value_was_empty(monkeypatch):
    state = {"writes": [], "clears": 0, "read_count": 0}

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Values:
        def get(self, **kwargs):
            def execute():
                state["read_count"] += 1
                if state["read_count"] == 1:
                    return {"values": []}
                return {"values": [["falscher Read-back"]]}
            return Request(execute)
        def update(self, **kwargs):
            return Request(lambda: state["writes"].append(kwargs["body"]["values"][0][0]) or {})
        def clear(self, **kwargs):
            def execute():
                state["clears"] += 1
                return {}
            return Request(execute)

    class Sheets:
        def spreadsheets(self):
            return self
        def values(self):
            return Values()

    with pytest.raises(RuntimeError, match="nicht zuverlässig aktualisiert"):
        checker._update_closed_history_timestamp(Sheets(), "spreadsheet-id")

    assert len(state["writes"]) == 1
    assert state["clears"] == 1


def test_quarantine_finalized_new_master_renames_and_verifies_drive_file(monkeypatch):
    state = {"name": checker.DRIVE_NAME, "updates": []}

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Files:
        def update(self, **kwargs):
            def execute():
                state["name"] = kwargs["body"]["name"]
                state["updates"].append(state["name"])
                return {"id": kwargs["fileId"], "name": state["name"]}
            return Request(execute)
        def list(self, **kwargs):
            def execute():
                query = kwargs["q"]
                if f"name='{state['name']}'" in query:
                    items = [{"id": "new-master-id", "name": state["name"],
                              "mimeType": checker.GOOGLE_SHEETS_MIME_TYPE}]
                else:
                    items = []
                return {"files": items}
            return Request(execute)

    class Drive:
        def files(self):
            return Files()

    quarantine_name = checker._quarantine_finalized_new_master(
        Drive(), "new-master-id", "INIT-original"
    )

    assert quarantine_name == state["name"]
    assert quarantine_name.startswith(checker.MASTER_INIT_PREFIX + "FAILED_TIMESTAMP_")
    assert state["updates"] == [quarantine_name]


def test_quarantine_finalized_new_master_fails_closed_when_drive_readback_missing():
    class Request:
        def execute(self):
            return {"files": []}

    class Files:
        def update(self, **kwargs):
            return Request()
        def list(self, **kwargs):
            return Request()

    class Drive:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="nicht eindeutig aus dem Produktivnamen entfernt"):
        checker._quarantine_finalized_new_master(
            Drive(), "new-master-id", "INIT-original"
        )


def test_quarantine_finalized_new_master_accepts_timeout_when_drive_readback_confirms_rename():
    state = {"name": checker.DRIVE_NAME}

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Files:
        def update(self, **kwargs):
            def execute():
                state["name"] = kwargs["body"]["name"]
                raise RuntimeError("simulierter Timeout nach serverseitiger Umbenennung")
            return Request(execute)
        def list(self, **kwargs):
            def execute():
                query = kwargs["q"]
                if f"name='{state['name']}'" in query:
                    return {"files": [{
                        "id": "new-master-id", "name": state["name"],
                        "mimeType": checker.GOOGLE_SHEETS_MIME_TYPE,
                    }]}
                return {"files": []}
            return Request(execute)

    class Drive:
        def files(self):
            return Files()

    result = checker._quarantine_finalized_new_master(
        Drive(), "new-master-id", "INIT-original"
    )
    assert result == state["name"]
    assert result.startswith(checker.MASTER_INIT_PREFIX + "FAILED_TIMESTAMP_")


def test_quarantine_finalized_new_master_reports_when_rename_failed_and_master_remains_active():
    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self):
            return self.callback()

    class Files:
        def update(self, **kwargs):
            return Request(lambda: (_ for _ in ()).throw(RuntimeError("Drive rename fehlgeschlagen")))
        def list(self, **kwargs):
            def execute():
                query = kwargs["q"]
                if f"name='{checker.DRIVE_NAME}'" in query:
                    return {"files": [{
                        "id": "new-master-id", "name": checker.DRIVE_NAME,
                        "mimeType": checker.GOOGLE_SHEETS_MIME_TYPE,
                    }]}
                return {"files": []}
            return Request(execute)

    class Drive:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="weiterhin_produktiv=True"):
        checker._quarantine_finalized_new_master(
            Drive(), "new-master-id", "INIT-original"
        )


def test_read_existing_history_uses_raw_values_and_preserves_sub_cent_precision():
    class Request:
        def __init__(self, result):
            self.result = result
        def execute(self):
            return self.result

    raw_row = [""] * (len(checker.HISTORY_HEADERS) + 1)
    raw_row[checker.HISTORY_HEADERS.index("Ticker")] = "XAU/USD"
    raw_row[checker.HISTORY_HEADERS.index("Name")] = "Gold Spot"
    raw_row[checker.HISTORY_HEADERS.index("Einstieg")] = 4037.0
    raw_row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = 45965.0
    raw_row[checker.HISTORY_HEADERS.index("OS_Aktueller_Kurs")] = 0.001
    raw_row[-1] = 52

    calls = {}
    class Values:
        def get(self, **kwargs):
            calls.update(kwargs)
            return Request({"values": [
                ["Geschlossene Positionen | historische Faktenbasis"],
                checker.HISTORY_DISPLAY_HEADERS,
                raw_row,
            ]})
    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {"title": "Geschlossene Positionen"}}]})
        def values(self):
            return Values()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    rows = checker.read_existing_history(Sheets(), "sheet-id")
    assert calls["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert calls["dateTimeRenderOption"] == "SERIAL_NUMBER"
    assert rows[2][checker.HISTORY_HEADERS.index("OS_Aktueller_Kurs")] == 0.001
    assert rows[2][checker.HISTORY_HEADERS.index("Einstiegsdatum")] == 45965.0

    expected = checker._expected_history_records(
        rows, pd.DataFrame(columns=checker.HISTORY_HEADERS)
    )
    assert len(expected) == 1
    archived = next(iter(expected.values()))
    assert archived[checker.HISTORY_HEADERS.index("OS_Aktueller_Kurs")] == 0.001


def test_google_history_numbering_changes_only_aa_and_preserves_existing_row_order(monkeypatch):
    updates = []
    batch_requests = []
    original_rows = []

    def make_row(ticker, number):
        row = [""] * (len(checker.HISTORY_HEADERS) + 1)
        row[checker.HISTORY_HEADERS.index("Ticker")] = ticker
        row[len(checker.HISTORY_HEADERS)] = number
        return row

    first = make_row("ZETA", 8)
    second = make_row("ALPHA", 9)
    original_rows.extend([first.copy(), second.copy()])

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result
    class Values:
        def get(self, **kwargs):
            assert kwargs["range"] == "'Geschlossene Positionen'!A1:AA"
            assert kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"
            return Request({"values": [
                ["Geschlossene Positionen | Stand"],
                checker.HISTORY_DISPLAY_HEADERS,
                first.copy(),
                second.copy(),
            ]})
        def update(self, **kwargs):
            updates.append(kwargs)
            return Request()
        def clear(self, **kwargs):
            raise AssertionError("Ein Archiv-Clear ist verboten.")
    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77,
                "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()
    def record_batch(_sheets, _spreadsheet_id, body, context=""):
        batch_requests.extend(body["requests"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_batch)
    assert checker._sort_and_number_closed_history(Sheets(), "sheet-id") == 2

    assert [first[checker.HISTORY_HEADERS.index("Ticker")],
            second[checker.HISTORY_HEADERS.index("Ticker")]] == ["ZETA", "ALPHA"]
    assert [row[-1] for row in original_rows] == [8, 9]
    assert len(updates) == 1
    assert updates[0]["range"] == "'Geschlossene Positionen'!AA3:AA4"
    assert updates[0]["body"]["values"] == [[1], [2]]
    assert all("sortRange" not in request for request in batch_requests)
    assert all(
        request.get("repeatCell", {}).get("range", {}).get("startColumnIndex") == len(checker.HISTORY_HEADERS)
        for request in batch_requests
    )
    assert all("AB" not in str(item) and "AE" not in str(item) for item in updates + batch_requests)


def test_existing_history_upload_path_has_no_existing_row_format_mutator():
    import inspect
    source = inspect.getsource(checker.upsert_google_sheet)
    assert "_align_history_date_columns_best_effort(" not in source
    assert not hasattr(checker, "_align_history_date_columns")
    assert not hasattr(checker, "_align_history_date_columns_best_effort")
    assert "_sort_and_number_closed_history(" in source


def test_history_verification_accepts_exact_historical_os_price_without_current_price_revaluation():
    raw_row = [""] * (len(checker.HISTORY_HEADERS) + 1)
    raw_row[checker.HISTORY_HEADERS.index("Ticker")] = "XAU/USD"
    raw_row[checker.HISTORY_HEADERS.index("Name")] = "Gold Spot"
    raw_row[checker.HISTORY_HEADERS.index("Einstieg")] = 4037.0
    raw_row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = 45965.0
    raw_row[checker.HISTORY_HEADERS.index("OS_Aktueller_Kurs")] = 0.001
    raw_row[len(checker.HISTORY_HEADERS)] = 1
    actual = [
        ["Geschlossene Positionen | historische Faktenbasis"],
        checker.HISTORY_DISPLAY_HEADERS,
        raw_row,
    ]
    item = {
        column: (raw_row[index] if index < len(raw_row) else "")
        for index, column in enumerate(checker.HISTORY_HEADERS)
    }
    expected = {checker._history_key(item): [item[column] for column in checker.HISTORY_HEADERS]}

    class Request:
        def execute(self):
            return {"values": actual}
    class Values:
        def get(self, **kwargs):
            assert kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"
            assert kwargs["dateTimeRenderOption"] == "SERIAL_NUMBER"
            return Request()
    class Spreadsheets:
        def values(self):
            return Values()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    result = checker._verify_history_full_content(Sheets(), "sheet-id", expected)
    assert result[2][checker.HISTORY_HEADERS.index("OS_Aktueller_Kurs")] == 0.001


def test_history_numbering_expands_only_to_aa_when_legacy_header_has_26_columns(monkeypatch):
    batch = []
    updates = []
    row = [""] * len(checker.HISTORY_HEADERS)
    row[checker.HISTORY_HEADERS.index("Ticker")] = "XAU/USD"

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result
    class Values:
        def get(self, **kwargs):
            return Request({"values": [
                ["Historie"],
                checker.HISTORY_HEADERS,
                row,
            ]})
        def update(self, **kwargs):
            updates.append(kwargs)
            return Request()
    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77,
                "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 26},
            }}]})
        def values(self):
            return Values()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()
    def record_batch(_sheets, _id, body, context=""):
        batch.extend(body["requests"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_batch)
    assert checker._sort_and_number_closed_history(Sheets(), "sheet-id") == 1

    assert [item["range"] for item in updates] == [
        "'Geschlossene Positionen'!AA2",
        "'Geschlossene Positionen'!AA3:AA3",
    ]
    assert updates[0]["body"]["values"] == [["Nr."]]
    assert updates[1]["body"]["values"] == [[1]]
    expansion = [item for item in batch if "updateSheetProperties" in item]
    assert len(expansion) == 1
    assert expansion[0]["updateSheetProperties"]["properties"]["gridProperties"]["columnCount"] == 27
    assert all("sortRange" not in item for item in batch)


def test_history_numbering_fails_closed_on_blank_intermediate_row_without_archive_writes(monkeypatch):
    updates = []
    batch_updates = []
    first = [""] * (len(checker.HISTORY_HEADERS) + 1)
    first[checker.HISTORY_HEADERS.index("Ticker")] = "FIRST"
    second = [""] * (len(checker.HISTORY_HEADERS) + 1)
    second[checker.HISTORY_HEADERS.index("Ticker")] = "SECOND"

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result
    class Values:
        def get(self, **kwargs):
            return Request({"values": [
                ["Historie"],
                checker.HISTORY_DISPLAY_HEADERS,
                first,
                [],
                second,
            ]})
        def update(self, **kwargs):
            updates.append(kwargs)
            return Request()
    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77,
                "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 26},
            }}]})
        def values(self):
            return Values()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    monkeypatch.setattr(
        checker, "_google_batch_update_with_retry",
        lambda *args, **kwargs: batch_updates.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="leere Zwischenzeile"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")
    assert updates == []
    assert batch_updates == []


def _valid_history_test_row(ticker, number=1):
    row = {column: "" for column in checker.HISTORY_HEADERS}
    row.update({
        "Name": f"Unternehmen {ticker}",
        "Ticker": ticker,
        "Einstieg": 100 + number,
        "Einstiegsdatum": f"{((number - 1) % 28) + 1:02d}.01.2025",
        "Ausstiegsdatum": f"{((number - 1) % 28) + 1:02d}.01.2025",
        "Status": "Verkauft",
        "Richtung": "Long",
        "Ausstiegskurs": 110 + number,
    })
    return row


def test_append_aborts_on_blank_intermediate_history_row_before_append(monkeypatch):
    rows = [["Titel"], checker.HISTORY_DISPLAY_HEADERS]
    for number in range(checker.MINIMUM_PROTECTED_HISTORY_ROWS):
        row = _valid_history_test_row(f"OLD{number:02d}", number + 1)
        rows.append([row[column] for column in checker.HISTORY_HEADERS] + [number + 1])
        if number == 24:
            rows.append([])

    incoming = _valid_history_test_row("NEW", 60)
    calls = {"append": []}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class Values:
        def append(self, **kwargs):
            calls["append"].append(kwargs)
            return Request({"updates": {"updatedRows": 1}})

    class SpreadsheetResource:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77,
                "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 27},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return SpreadsheetResource()

    monkeypatch.setattr(
        checker, "read_existing_history", lambda *args, **kwargs: rows
    )
    with pytest.raises(RuntimeError, match="leere Zwischenzeile"):
        checker._append_missing_history_rows(
            Sheets(), "sheet-id",
            pd.DataFrame([incoming], columns=checker.HISTORY_HEADERS),
            allow_initialize=False,
        )
    assert calls["append"] == []


def test_full_history_verification_rejects_changed_physical_order():
    first = _valid_history_test_row("FIRST", 1)
    second = _valid_history_test_row("SECOND", 2)
    first_key = checker._history_key(first)
    second_key = checker._history_key(second)

    def raw_row(item, number):
        raw = [item[column] for column in checker.HISTORY_HEADERS]
        for index, column in enumerate(checker.HISTORY_HEADERS):
            if column in {"Einstiegsdatum", "Ausstiegsdatum"}:
                raw[index] = checker._date_to_sheets_serial(
                    raw[index], column, number + 2, strict=True
                )
        return raw + [number]

    expected = {
        first_key: [first[column] for column in checker.HISTORY_HEADERS],
        second_key: [second[column] for column in checker.HISTORY_HEADERS],
    }
    actual = [
        ["Historie"],
        checker.HISTORY_DISPLAY_HEADERS,
        raw_row(second, 1),
        raw_row(first, 2),
    ]

    class Request:
        def execute(self):
            return {"values": actual}

    class Values:
        def get(self, **kwargs):
            return Request()

    class SpreadsheetResource:
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return SpreadsheetResource()

    with pytest.raises(RuntimeError, match="physische Reihenfolge"):
        checker._verify_history_full_content(
            Sheets(), "sheet-id", expected,
            expected_order=[first_key, second_key],
        )


def test_upsert_preflight_rejects_blank_history_gap_before_any_sheet_write(monkeypatch):
    rows = [["Titel"], checker.HISTORY_DISPLAY_HEADERS]
    for number in range(checker.MINIMUM_PROTECTED_HISTORY_ROWS):
        row = _valid_history_test_row(f"OLD{number:02d}", number + 1)
        rows.append([row[column] for column in checker.HISTORY_HEADERS] + [number + 1])
        if number == 24:
            rows.append([])

    calls = {"batch_update": 0, "snapshot": 0}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class SpreadsheetResource:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
            }}]})
        def batchUpdate(self, **kwargs):
            calls["batch_update"] += 1
            return Request()

    class Sheets:
        def spreadsheets(self):
            return SpreadsheetResource()

    sheets = Sheets()
    monkeypatch.setattr(checker, "_validate_output_before_upload", lambda *args: None)
    monkeypatch.setattr(
        checker, "build", lambda service, version, credentials: sheets, raising=False
    )
    monkeypatch.setattr(
        checker, "_find_or_initialize_master",
        lambda drive, closed_df: ("spreadsheet-id", False, None),
    )
    monkeypatch.setattr(
        checker, "read_existing_history", lambda *args, **kwargs: rows
    )
    monkeypatch.setattr(
        checker, "_snapshot_google_sheet",
        lambda *args, **kwargs: calls.__setitem__("snapshot", calls["snapshot"] + 1),
    )

    with pytest.raises(RuntimeError, match="leere Zwischenzeile"):
        checker.upsert_google_sheet(
            pd.DataFrame(columns=checker.HEADERS),
            pd.DataFrame(columns=checker.HISTORY_HEADERS),
            creds=object(),
        )

    assert calls == {"batch_update": 0, "snapshot": 0}
