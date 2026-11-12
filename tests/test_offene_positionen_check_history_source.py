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
                {"Ticker": "AAA.DE", "Status": "Offen", "Einstiegsdatum": "01.01.2026"},
                {"Ticker": "BBB.N", "Status": " offen ", "Einstiegsdatum": "02.01.2026"},
                {"Ticker": "CLOSED.DE", "Status": "Verkauft", "Einstiegsdatum": "03.01.2026"},
            ],
            ["AAA.DE", "BBB.N"],
        ),
        (
            [
                {"Ticker": "NEW1.DE", "Status": "Offen", "Einstiegsdatum": "01.01.2026"},
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
    fieldnames = ["Ticker", "Status", "Einstiegsdatum", "Name"]
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
