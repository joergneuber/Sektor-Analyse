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
        ["LSUG.MI", "WisdomTree Sugar", "", "", "", "", "Offen",
         "18.03.2026", 2.06, 2.06, 2.35] + [""] * (len(checker.HEADERS) - 11),
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



def test_google_history_tab_uses_three_level_sort_and_numbers_after_sort(monkeypatch):
    requests, updates, clears = [], [], []

    class Request:
        def __init__(self, result=None): self.result = result or {}
        def execute(self): return self.result

    class Values:
        def get(self, **kwargs):
            def history_row(ticker, exit_date, entry_date):
                row = [""] * len(checker.HISTORY_HEADERS)
                row[checker.HISTORY_HEADERS.index("Ticker")] = ticker
                row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = exit_date
                row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = entry_date
                return row
            return Request({"values": [
                ["Geschlossene Positionen | Stand"], checker.HISTORY_HEADERS,
                history_row("ZETA", "16.11.2026", "01.01.2026"),
                history_row("BETA", "01.02.2026", "01.01.2026"),
                history_row("ALPHA", "01.02.2026", "01.01.2026"),
                history_row("ALPHA", "01.02.2026", "02.01.2026"),
            ]})
        def update(self, **kwargs): updates.append(kwargs); return Request()
        def clear(self, **kwargs): clears.append(kwargs); return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 26},
            }}]})
        def values(self): return Values()
    class Sheets:
        def spreadsheets(self): return Spreadsheets()
    def record_batch(_sheets, _spreadsheet_id, body, context=""):
        requests.append((body, context)); return {"replies": []}
    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_batch)

    count = checker._sort_and_number_closed_history(Sheets(), "sheet-id")
    assert count == 4
    sort_requests = [item for body, _ in requests for item in body["requests"] if "sortRange" in item]
    assert len(sort_requests) == 1
    sort = sort_requests[0]["sortRange"]
    assert sort["sortSpecs"] == [
        {"dimensionIndex": len(checker.HISTORY_DISPLAY_HEADERS), "sortOrder": "ASCENDING"},
        {"dimensionIndex": len(checker.HISTORY_DISPLAY_HEADERS) + 1, "sortOrder": "ASCENDING"},
        {"dimensionIndex": len(checker.HISTORY_DISPLAY_HEADERS) + 2, "sortOrder": "ASCENDING"},
        {"dimensionIndex": len(checker.HISTORY_DISPLAY_HEADERS) + 3, "sortOrder": "ASCENDING"},
    ]
    assert any(item.get("range") == "'Geschlossene Positionen'!AA2" for item in updates)
    assert not any(item.get("range") == "'Geschlossene Positionen'!AB2:AD2" for item in updates)
    sort_keys = next(item for item in updates if item.get("range") == "'Geschlossene Positionen'!AB3:AE6")
    assert sort_keys["body"]["values"] == [
        [checker._date_to_sheets_serial("16.11.2026", "Ausstiegsdatum", 3, strict=True), "zeta", checker._date_to_sheets_serial("01.01.2026", "Einstiegsdatum", 3, strict=True), 1],
        [checker._date_to_sheets_serial("01.02.2026", "Ausstiegsdatum", 4, strict=True), "beta", checker._date_to_sheets_serial("01.01.2026", "Einstiegsdatum", 4, strict=True), 2],
        [checker._date_to_sheets_serial("01.02.2026", "Ausstiegsdatum", 5, strict=True), "alpha", checker._date_to_sheets_serial("01.01.2026", "Einstiegsdatum", 5, strict=True), 3],
        [checker._date_to_sheets_serial("01.02.2026", "Ausstiegsdatum", 6, strict=True), "alpha", checker._date_to_sheets_serial("02.01.2026", "Einstiegsdatum", 6, strict=True), 4],
    ]
    numbered = next(item for item in updates if item.get("range") == "'Geschlossene Positionen'!AA3:AA6")
    assert numbered["body"]["values"] == [[1], [2], [3], [4]]
    assert [item.get("range") for item in clears].count("'Geschlossene Positionen'!AB:AE") == 2


def test_google_history_accepts_existing_27_column_header_and_repeat_runs(monkeypatch):
    calls = {"runs": 0, "sorts": [], "numbers": []}
    class Request:
        def __init__(self, result=None): self.result = result or {}
        def execute(self): return self.result
    class Values:
        def get(self, **kwargs):
            calls["runs"] += 1
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "01.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_DISPLAY_HEADERS, row + [1]]})
        def update(self, **kwargs):
            if kwargs.get("range", "").endswith("AA3:AA3"):
                calls["numbers"].append(kwargs["body"]["values"])
            return Request()
        def clear(self, **kwargs): return Request()
    class Spreadsheets:
        def get(self, **kwargs): return Request({"sheets": [{"properties": {
            "sheetId": 7, "title": "Geschlossene Positionen",
            "gridProperties": {"columnCount": 31},
        }}]})
        def values(self): return Values()
    class Sheets:
        def spreadsheets(self): return Spreadsheets()
    def record_batch(_sheets, _id, body, context=""):
        calls["sorts"].extend([r["sortRange"]["sortSpecs"] for r in body["requests"] if "sortRange" in r])
        return {"replies": []}
    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_batch)
    assert checker._sort_and_number_closed_history(Sheets(), "id") == 1
    assert checker._sort_and_number_closed_history(Sheets(), "id") == 1
    assert calls["runs"] == 2
    assert calls["numbers"] == [[[1]], [[1]]]
    assert len(calls["sorts"]) == 2
    assert calls["sorts"][0] == calls["sorts"][1]


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


def test_google_sort_really_preserves_order_for_identical_three_keys_across_runs(monkeypatch):
    # Simuliert die tatsächliche SortRange-Wirkung auf persistente Sheet-Zeilen,
    # statt nur zwei identische API-Spezifikationen zu vergleichen.
    class Request:
        def __init__(self, result=None): self.result = result or {}
        def execute(self): return self.result

    rows = []
    header = checker.HISTORY_DISPLAY_HEADERS[:]
    for name in ("SECOND", "FIRST", "THIRD"):
        row = [""] * len(checker.HISTORY_HEADERS)
        row[checker.HISTORY_HEADERS.index("Ticker")] = "SAME"
        row[checker.HISTORY_HEADERS.index("Name")] = name
        row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "01.02.2026"
        row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
        rows.append(row + [None])
    state = {
        "rows": rows, "headers": header, "sorts": 0, "clears": [],
        "helper_header": ["old_exit", "old_ticker", "old_entry", "old_row"],
    }

    class Values:
        def get(self, **kwargs):
            return Request({"values": [
                ["Geschlossene Positionen | Stand"], state["headers"], *[r[:27] for r in state["rows"]]
            ]})
        def update(self, **kwargs):
            target = kwargs["range"].split("!")[-1]
            vals = kwargs["body"]["values"]
            if target == "AA2":
                state["headers"] = checker.HISTORY_DISPLAY_HEADERS[:]
            elif target.startswith("AB3:AE"):
                for index, key_row in enumerate(vals):
                    state["rows"][index][27:31] = key_row
            elif target.startswith("AA3:AA"):
                for index, value in enumerate(vals):
                    state["rows"][index][26] = value[0]
            return Request()
        def clear(self, **kwargs):
            target = kwargs["range"].split("!")[-1]
            state["clears"].append(target)
            if target == "AB:AE":
                state["helper_header"] = [None, None, None, None]
                for row in state["rows"]:
                    row[27:31] = [None, None, None, None]
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self): return Values()
    class Sheets:
        def spreadsheets(self): return Spreadsheets()

    def apply_google_sort(_sheets, _id, body, context=""):
        for request in body["requests"]:
            if "sortRange" not in request:
                continue
            specs = request["sortRange"]["sortSpecs"]
            state["rows"].sort(key=lambda row: tuple(row[spec["dimensionIndex"]] for spec in specs))
            state["sorts"] += 1
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", apply_google_sort)
    sheets = Sheets()
    assert checker._sort_and_number_closed_history(sheets, "id") == 3
    first_order = [row[checker.HISTORY_HEADERS.index("Name")] for row in state["rows"]]
    assert first_order == ["SECOND", "FIRST", "THIRD"]
    assert [row[26] for row in state["rows"]] == [1, 2, 3]
    assert all(row[27:31] == [None, None, None, None] for row in state["rows"])
    assert state["helper_header"] == [None, None, None, None]

    assert checker._sort_and_number_closed_history(sheets, "id") == 3
    second_order = [row[checker.HISTORY_HEADERS.index("Name")] for row in state["rows"]]
    assert second_order == first_order
    assert [row[26] for row in state["rows"]] == [1, 2, 3]
    assert state["sorts"] == 2
    assert state["clears"].count("AB:AE") == 4


def test_google_history_helper_columns_are_cleared_and_hidden_when_sort_request_fails(monkeypatch):
    state = {
        "helper_values": None, "clear_ranges": [], "sort_attempts": 0,
        "hide_requests": [],
    }

    class Request:
        def __init__(self, result=None, error=None):
            self.result, self.error = result or {}, error
        def execute(self):
            if self.error:
                raise self.error
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "02.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_HEADERS, row]})
        def update(self, **kwargs):
            if kwargs.get("range", "").endswith("AB3:AE3"):
                state["helper_values"] = kwargs["body"]["values"]
            return Request()
        def clear(self, **kwargs):
            target = kwargs.get("range")
            state["clear_ranges"].append(target)
            if target == "'Geschlossene Positionen'!AB:AE":
                state["helper_values"] = None
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def fail_sort(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "sortRange" in request:
                state["sort_attempts"] += 1
                raise RuntimeError("simulierter Google-Sheets-Sortierfehler")
            if "updateDimensionProperties" in request:
                state["hide_requests"].append(request["updateDimensionProperties"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", fail_sort)
    with pytest.raises(RuntimeError, match="simulierter Google-Sheets-Sortierfehler"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["sort_attempts"] == 1
    assert state["helper_values"] is None
    assert state["clear_ranges"].count("'Geschlossene Positionen'!AB:AE") == 2
    assert len(state["hide_requests"]) == 1
    hidden = state["hide_requests"][0]
    assert hidden["range"]["startIndex"] == 27
    assert hidden["range"]["endIndex"] == 31
    assert hidden["properties"]["hiddenByUser"] is True


def test_google_history_cleanup_failure_still_hides_helper_columns_and_preserves_primary_error(monkeypatch):
    state = {"clear_calls": 0, "hide_requests": []}

    class Request:
        def __init__(self, result=None, error=None):
            self.result, self.error = result or {}, error
        def execute(self):
            if self.error:
                raise self.error
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "02.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_HEADERS, row]})
        def update(self, **kwargs):
            return Request()
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            if state["clear_calls"] == 2:
                return Request(error=RuntimeError("simulierter Cleanup-Fehler"))
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def fail_sort_and_record_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "sortRange" in request:
                raise RuntimeError("ursprünglicher Sortierfehler")
            if "updateDimensionProperties" in request:
                state["hide_requests"].append(request["updateDimensionProperties"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", fail_sort_and_record_hide)
    with pytest.raises(RuntimeError, match="ursprünglicher Sortierfehler") as caught:
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 2
    assert len(state["hide_requests"]) == 1
    assert state["hide_requests"][0]["properties"]["hiddenByUser"] is True
    assert any("Cleanup-Fehler" in note for note in getattr(caught.value, "__notes__", []))


def test_google_history_cleanup_failure_without_primary_error_is_raised_after_hide_attempt(monkeypatch):
    state = {"clear_calls": 0, "hide_requests": []}

    class Request:
        def __init__(self, result=None, error=None):
            self.result, self.error = result or {}, error
        def execute(self):
            if self.error:
                raise self.error
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "02.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_HEADERS, row]})
        def update(self, **kwargs):
            return Request()
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            if state["clear_calls"] == 2:
                return Request(error=RuntimeError("simulierter Cleanup-Fehler"))
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def record_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "updateDimensionProperties" in request:
                state["hide_requests"].append(request["updateDimensionProperties"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_hide)
    with pytest.raises(RuntimeError, match="simulierter Cleanup-Fehler"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 2
    assert len(state["hide_requests"]) == 1
    assert state["hide_requests"][0]["properties"]["hiddenByUser"] is True


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


def test_google_history_initial_helper_clear_failure_still_retries_cleanup_and_hides(monkeypatch):
    state = {"clear_calls": 0, "hide_requests": []}

    class Request:
        def __init__(self, result=None, error=None):
            self.result, self.error = result or {}, error
        def execute(self):
            if self.error:
                raise self.error
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "02.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_HEADERS, row]})
        def update(self, **kwargs):
            return Request()
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            if state["clear_calls"] == 1:
                return Request(error=RuntimeError("initialer Clear fehlgeschlagen"))
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def record_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "updateDimensionProperties" in request:
                state["hide_requests"].append(request["updateDimensionProperties"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_hide)
    with pytest.raises(RuntimeError, match="initialer Clear fehlgeschlagen"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 2
    assert len(state["hide_requests"]) == 1
    assert state["hide_requests"][0]["properties"]["hiddenByUser"] is True


def test_google_history_empty_intermediate_row_still_cleans_and_hides(monkeypatch):
    state = {"clear_calls": 0, "hide_requests": []}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            return Request({"values": [
                ["Titel"], checker.HISTORY_HEADERS, row, [], row
            ]})
        def update(self, **kwargs):
            return Request()
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def record_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "updateDimensionProperties" in request:
                state["hide_requests"].append(request["updateDimensionProperties"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", record_hide)
    with pytest.raises(RuntimeError, match="leere Zwischenzeile"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 2
    assert len(state["hide_requests"]) == 1
    assert state["hide_requests"][0]["range"]["startIndex"] == 27
    assert state["hide_requests"][0]["range"]["endIndex"] == 31


@pytest.mark.parametrize(
    "existing_column_count,expected_hide_end",
    [(26, None), (27, None), (28, 28), (29, 29), (30, 30)],
)
def test_google_history_column_expansion_failure_only_hides_existing_helper_columns(
    monkeypatch, existing_column_count, expected_hide_end
):
    state = {"clear_calls": 0, "hide_requests": []}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class Values:
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": existing_column_count},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def fail_expand_record_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "updateSheetProperties" in request:
                raise RuntimeError("Spalten-Erweiterung fehlgeschlagen")
            if "updateDimensionProperties" in request:
                state["hide_requests"].append(request["updateDimensionProperties"])
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", fail_expand_record_hide)
    with pytest.raises(RuntimeError, match="Spalten-Erweiterung fehlgeschlagen"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 1
    if expected_hide_end is None:
        assert state["hide_requests"] == []
    else:
        assert len(state["hide_requests"]) == 1
        hide_range = state["hide_requests"][0]["range"]
        assert hide_range["startIndex"] == 27
        assert hide_range["endIndex"] == expected_hide_end
        assert state["hide_requests"][0]["properties"]["hiddenByUser"] is True


def test_google_history_hide_failure_does_not_replace_primary_sort_error(monkeypatch):
    state = {"clear_calls": 0, "hide_attempts": 0}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "02.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_HEADERS, row]})
        def update(self, **kwargs):
            return Request()
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def fail_sort_and_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "sortRange" in request:
                raise RuntimeError("primärer Sortierfehler")
            if "updateDimensionProperties" in request:
                state["hide_attempts"] += 1
                raise RuntimeError("Hide fehlgeschlagen")
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", fail_sort_and_hide)
    with pytest.raises(RuntimeError, match="primärer Sortierfehler") as caught:
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 2
    assert state["hide_attempts"] == 1
    assert any("AB:AE ausblenden" in note for note in getattr(caught.value, "__notes__", []))


def test_google_history_hide_failure_without_primary_error_is_raised(monkeypatch):
    state = {"clear_calls": 0, "hide_attempts": 0}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self):
            return self.result

    class Values:
        def get(self, **kwargs):
            row = [""] * len(checker.HISTORY_HEADERS)
            row[checker.HISTORY_HEADERS.index("Ticker")] = "AAA"
            row[checker.HISTORY_HEADERS.index("Ausstiegsdatum")] = "02.02.2026"
            row[checker.HISTORY_HEADERS.index("Einstiegsdatum")] = "01.01.2026"
            return Request({"values": [["Titel"], checker.HISTORY_HEADERS, row]})
        def update(self, **kwargs):
            return Request()
        def clear(self, **kwargs):
            state["clear_calls"] += 1
            return Request()

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "sheetId": 77, "title": "Geschlossene Positionen",
                "gridProperties": {"columnCount": 31},
            }}]})
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    def fail_hide(_sheets, _spreadsheet_id, body, context=""):
        for request in body["requests"]:
            if "updateDimensionProperties" in request:
                state["hide_attempts"] += 1
                raise RuntimeError("Hide ohne Primärfehler fehlgeschlagen")
        return {"replies": []}

    monkeypatch.setattr(checker, "_google_batch_update_with_retry", fail_hide)
    with pytest.raises(RuntimeError, match="Hide ohne Primärfehler fehlgeschlagen"):
        checker._sort_and_number_closed_history(Sheets(), "sheet-id")

    assert state["clear_calls"] == 2
    assert state["hide_attempts"] == 1


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
