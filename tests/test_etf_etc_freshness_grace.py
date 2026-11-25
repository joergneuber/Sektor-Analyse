"""Regression tests for the 48-hour ETF/ETC OHLCV freshness allowance."""

import csv
import sys
import types

import numpy as np
import pandas as pd
import pytest

try:
    import yfinance  # noqa: F401
except ModuleNotFoundError:
    sys.modules["yfinance"] = types.SimpleNamespace(Ticker=lambda ticker: None)

import historical_ohlcv as ho
import offene_positionen_check as checker
import ohlcv_quality_gate as quality_gate


def _quality_frame(last_date="2026-11-23"):
    index = pd.bdate_range(end=last_date, periods=60)
    values = np.arange(60, dtype=float) + 100.0
    frame = pd.DataFrame(
        {
            "Open": values,
            "High": values + 1.0,
            "Low": values - 1.0,
            "Close": values + 0.5,
        },
        index=index,
    )
    frame.attrs["ohlcv_quality"] = {
        "quality_status": "PASS",
        "latest_bar_valid": True,
        "last_3_bars_valid": True,
        "reason_codes": [],
    }
    return frame


def _install_fake_exchange_calendar(monkeypatch, sessions, close_utc):
    monkeypatch.setattr(ho, "_calendar_name_for_ticker", lambda ticker: "XPAR")
    monkeypatch.setattr(
        ho, "_calendar_session_dates",
        lambda calendar_name, start, end: list(sessions),
    )

    class Calendar:
        @staticmethod
        def session_close(session):
            return pd.Timestamp(close_utc)

    monkeypatch.setitem(
        sys.modules,
        "exchange_calendars",
        types.SimpleNamespace(get_calendar=lambda name: Calendar()),
    )


def test_etf_etc_classification_uses_product_type_and_legacy_name():
    assert ho._is_etf_or_etc("ETF", "WisdomTree Sugar 2x Daily Leveraged")
    assert ho._is_etf_or_etc("", "iShares Physical Silver ETC")
    assert ho._is_etf_or_etc("Exchange Traded Commodity", "")
    assert ho._is_etf_or_etc("N/A", "iShares Physical Silver ETC")
    assert not ho._is_etf_or_etc("Optionsschein", "Optionsschein auf einen ETF")
    assert not ho._is_etf_or_etc("Aktie", "Apple Inc.")


def test_grace_accepts_previous_session_until_exact_48h_deadline(monkeypatch):
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    assert ho._etf_etc_freshness_grace_is_active(
        "TEST.PA", "2026-11-23", "2026-11-24",
        as_of="2026-11-26T16:30:00Z",
    )


def test_grace_expires_one_second_after_48h_deadline(monkeypatch):
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    assert not ho._etf_etc_freshness_grace_is_active(
        "TEST.PA", "2026-11-23", "2026-11-24",
        as_of="2026-11-26T16:30:01Z",
    )


def test_grace_never_accepts_two_missing_sessions(monkeypatch):
    _install_fake_exchange_calendar(
        monkeypatch,
        ["2026-11-20", "2026-11-23", "2026-11-24"],
        "2026-11-24T16:30:00Z",
    )
    assert not ho._etf_etc_freshness_grace_is_active(
        "TEST.PA", "2026-11-20", "2026-11-24",
        as_of="2026-11-25T16:00:00Z",
    )


def test_unknown_calendar_fails_closed(monkeypatch):
    monkeypatch.setattr(ho, "_calendar_name_for_ticker", lambda ticker: None)
    assert not ho._etf_etc_freshness_grace_is_active(
        "UNKNOWN", "2026-11-23", "2026-11-24",
        as_of="2026-11-25T16:00:00Z",
    )


def test_checker_accepts_one_stale_etf_session_but_not_for_stock(monkeypatch):
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    frame = _quality_frame("2026-11-23")

    accepted, reason = checker._quality_is_current_and_usable(
        frame, "TEST.PA", instrument_type="ETF",
        as_of="2026-11-25T16:00:00Z",
    )
    assert accepted and reason == ""

    accepted, reason = checker._quality_is_current_and_usable(
        frame, "TEST.PA", instrument_type="Aktie",
        instrument_name="Example AG",
        as_of="2026-11-25T16:00:00Z",
    )
    assert not accepted
    assert reason.startswith("STALE_HISTORY:")



def test_etf_grace_does_not_override_failed_last_three_bar_quality(monkeypatch):
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    frame = _quality_frame("2026-11-23")
    frame.attrs["ohlcv_quality"]["last_3_bars_valid"] = False

    accepted, reason = checker._quality_is_current_and_usable(
        frame, "TEST.PA", instrument_type="ETF",
        as_of="2026-11-25T16:00:00Z",
    )
    assert not accepted
    assert reason == "LAST_3_EXPECTED_BARS_INVALID"


def test_etf_grace_does_not_override_invalid_last_ohlc_bars(monkeypatch):
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    frame = _quality_frame("2026-11-23")
    frame.iloc[-1, frame.columns.get_loc("Close")] = np.nan

    accepted, reason = checker._quality_is_current_and_usable(
        frame, "TEST.PA", instrument_type="ETF",
        as_of="2026-11-25T16:00:00Z",
    )
    assert not accepted
    assert reason == "LAST_3_BARS_CONTAIN_INVALID_OHLC"


def test_run_local_passes_product_type_and_name_to_etf_etc_history_fetch(
    tmp_path, monkeypatch
):
    source = tmp_path / "Offene_Positionen.csv"
    output = tmp_path / "Offene Positionen+Check.csv"
    history = tmp_path / "Geschlossene Positionen.csv"
    source.write_text(
        "Ticker;Name;Produkt_Typ;Status;Einstiegsdatum;Einstieg;Stop\n"
        "LSUG.MI;WisdomTree Sugar 2x Daily Leveraged;ETF;Offen;18.03.2026;2,06;1,75\n"
        "NRJ.PA;Amundi MSCI New Energy UCITS ETF Dist;ETF;Offen;03.02.2026;38,04;38,00\n"
        "PPFD.SG;iShares Physical Silver ETC;;Offen;17.02.2026;57,20;45,00\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(
        checker, "extract_closed_history",
        lambda frame: pd.DataFrame(columns=checker.HISTORY_HEADERS),
    )
    monkeypatch.setattr(
        checker, "load_initial_history",
        lambda: pd.DataFrame(columns=checker.HISTORY_HEADERS),
    )
    seen = []

    def fake_fetch(ticker, **kwargs):
        seen.append((ticker, kwargs))
        return pd.DataFrame()

    monkeypatch.setattr(checker, "fetch_history", fake_fetch)
    monkeypatch.setattr(
        checker, "analyze_technical",
        lambda frame, row: checker.TechnicalResult(),
    )
    monkeypatch.setattr(
        checker, "make_row",
        lambda row, tech: {column: row.get(column, "") for column in checker.HEADERS},
    )

    checker.run_local(
        input_file=str(source), output_csv=str(output), history_csv=str(history)
    )

    assert seen == [
        ("LSUG.MI", {
            "instrument_type": "ETF",
            "instrument_name": "WisdomTree Sugar 2x Daily Leveraged",
        }),
        ("NRJ.PA", {
            "instrument_type": "ETF",
            "instrument_name": "Amundi MSCI New Energy UCITS ETF Dist",
        }),
        ("PPFD.SG", {
            "instrument_type": "",
            "instrument_name": "iShares Physical Silver ETC",
        }),
    ]


@pytest.mark.parametrize(
    ("instrument_type", "expects_historical_cutoff"),
    [("ETF", True), ("Aktie", False)],
)
def test_provider_quality_gate_uses_historical_cutoff_only_for_etf_etc(
    monkeypatch, instrument_type, expects_historical_cutoff
):
    provider_frame = _quality_frame("2026-11-23")
    monkeypatch.setattr(
        ho, "load_history", lambda ticker: pd.DataFrame()
    )

    class FakeTicker:
        def history(self, **kwargs):
            return provider_frame.copy()

    monkeypatch.setattr(checker.yf, "Ticker", lambda ticker: FakeTicker())
    expected_as_of = pd.Timestamp("2026-11-23T23:59:00Z")
    gate_calls = []

    class Quality:
        @staticmethod
        def to_dict():
            return {
                "quality_status": "PASS",
                "latest_bar_valid": True,
                "last_3_bars_valid": True,
                "reason_codes": [],
            }

    def fake_gate(frame, ticker, **kwargs):
        gate_calls.append(kwargs)
        return frame.copy(), Quality()

    monkeypatch.setattr(quality_gate, "apply_ohlcv_quality_gate", fake_gate)
    monkeypatch.setattr(
        quality_gate, "historical_frame_as_of",
        lambda frame, ticker: expected_as_of,
    )
    monkeypatch.setattr(
        checker, "_quality_is_current_and_usable",
        lambda frame, ticker, **kwargs: (
            (False, "NO_VALID_HISTORY") if frame.empty else (True, "")
        ),
    )
    monkeypatch.setattr(
        checker, "_persist_usable_provider_history",
        lambda ticker, frame, source: len(frame),
    )

    result = checker.fetch_history(
        "TEST.PA", instrument_type=instrument_type,
        instrument_name="Example instrument",
    )
    assert not result.empty
    assert len(gate_calls) == 1
    assert ("as_of" in gate_calls[0]) is expects_historical_cutoff
    if expects_historical_cutoff:
        assert gate_calls[0]["as_of"] == expected_as_of


def test_verify_open_positions_accepts_one_stale_etf_session(tmp_path, monkeypatch):
    positions = tmp_path / "Offene_Positionen.csv"
    positions.write_text(
        "Ticker;Name;Produkt_Typ;Status\n"
        "LSUG.MI;WisdomTree Sugar 2x Daily Leveraged;ETF;Offen\n",
        encoding="utf-8-sig",
    )
    frame = _quality_frame("2026-11-23")
    monkeypatch.setattr(ho, "load_history", lambda ticker, db_path: frame.copy())
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )

    report = ho.verify_open_positions(
        tmp_path / "unused.sqlite", positions,
        as_of="2026-11-25T16:00:00Z",
    )
    assert report["ok"] is True
    assert report["results"]["LSUG.MI"]["freshness_grace_applied"] is True


def test_verify_open_positions_keeps_non_etf_strict(tmp_path, monkeypatch):
    positions = tmp_path / "Offene_Positionen.csv"
    positions.write_text(
        "Ticker;Name;Produkt_Typ;Status\n"
        "ABC.PA;Example AG;Aktie;Offen\n",
        encoding="utf-8-sig",
    )
    frame = _quality_frame("2026-11-23")
    monkeypatch.setattr(ho, "load_history", lambda ticker, db_path: frame.copy())
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )

    report = ho.verify_open_positions(
        tmp_path / "unused.sqlite", positions,
        as_of="2026-11-25T16:00:00Z",
    )
    assert report["ok"] is False
    assert report["failures"]["ABC.PA"] == ["STALE_HISTORY:2026-11-23<2026-11-24"]


def test_central_quality_gate_accepts_one_missing_latest_etf_session_for_48h(
    tmp_path, monkeypatch
):
    frame = _quality_frame("2026-11-23")
    sessions = [pd.Timestamp(day).date().isoformat() for day in frame.index]
    sessions.append("2026-11-24")
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(quality_gate, "_calendar_sessions", lambda ticker, start, end: sessions)
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    registry = tmp_path / "quality.csv"
    gap_registry = tmp_path / "gaps.csv"
    registry.write_text("ticker,date,classification\\n", encoding="utf-8")
    gap_registry.write_text("ticker,missing_date,timeframe,quality_class\\n", encoding="utf-8")

    gated, quality = quality_gate.apply_ohlcv_quality_gate(
        frame, "TEST.PA",
        registry_path=registry,
        gap_registry_path=gap_registry,
        window_sessions=60,
        preserve_expected_sessions=True,
        as_of="2026-11-26T16:30:00Z",
        instrument_type="ETF",
    )
    assert quality.quality_status == "PASS_WITH_EXCLUSIONS"
    assert "ETF_ETC_FRESHNESS_GRACE_48H" in quality.reason_codes
    assert quality.latest_bar_valid is True
    assert quality.last_3_bars_valid is True
    assert pd.Timestamp(gated.index.max()).date().isoformat() == "2026-11-23"
    assert gated.attrs["ohlcv_quality_freshness_grace"]["missing_expected_sessions"] == 1


def test_central_quality_gate_rejects_etf_after_48h_deadline(tmp_path, monkeypatch):
    frame = _quality_frame("2026-11-23")
    sessions = [pd.Timestamp(day).date().isoformat() for day in frame.index]
    sessions.append("2026-11-24")
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(quality_gate, "_calendar_sessions", lambda ticker, start, end: sessions)
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    registry = tmp_path / "quality.csv"
    gap_registry = tmp_path / "gaps.csv"
    registry.write_text("ticker,date,classification\\n", encoding="utf-8")
    gap_registry.write_text("ticker,missing_date,timeframe,quality_class\\n", encoding="utf-8")

    _, quality = quality_gate.apply_ohlcv_quality_gate(
        frame, "TEST.PA",
        registry_path=registry,
        gap_registry_path=gap_registry,
        window_sessions=60,
        preserve_expected_sessions=True,
        as_of="2026-11-26T16:30:01Z",
        instrument_type="ETF",
    )
    assert quality.quality_status == "INCOMPLETE"
    assert "LATEST_EXPECTED_BAR_INVALID" in quality.reason_codes


def test_central_quality_gate_never_accepts_two_missing_etf_sessions(tmp_path, monkeypatch):
    frame = _quality_frame("2026-11-20")
    sessions = [pd.Timestamp(day).date().isoformat() for day in frame.index]
    sessions.extend(["2026-11-23", "2026-11-24"])
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(quality_gate, "_calendar_sessions", lambda ticker, start, end: sessions)
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    registry = tmp_path / "quality.csv"
    gap_registry = tmp_path / "gaps.csv"
    registry.write_text("ticker,date,classification\\n", encoding="utf-8")
    gap_registry.write_text("ticker,missing_date,timeframe,quality_class\\n", encoding="utf-8")

    _, quality = quality_gate.apply_ohlcv_quality_gate(
        frame, "TEST.PA",
        registry_path=registry,
        gap_registry_path=gap_registry,
        window_sessions=60,
        preserve_expected_sessions=True,
        as_of="2026-11-26T16:00:00Z",
        instrument_type="ETC",
    )
    assert quality.quality_status == "INCOMPLETE"
    assert "LATEST_EXPECTED_BAR_INVALID" in quality.reason_codes


def test_central_quality_gate_resolves_etf_etc_metadata_from_open_positions(
    tmp_path, monkeypatch
):
    frame = _quality_frame("2026-11-23")
    sessions = [pd.Timestamp(day).date().isoformat() for day in frame.index]
    sessions.append("2026-11-24")
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(quality_gate, "_calendar_sessions", lambda ticker, start, end: sessions)
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    monkeypatch.setattr(
        ho, "_load_open_position_metadata",
        lambda: {"PPFD.SG": ("N/A", "iShares Physical Silver ETC")},
    )
    registry = tmp_path / "quality.csv"
    gap_registry = tmp_path / "gaps.csv"
    registry.write_text("ticker,date,classification\\n", encoding="utf-8")
    gap_registry.write_text("ticker,missing_date,timeframe,quality_class\\n", encoding="utf-8")

    _, quality = quality_gate.apply_ohlcv_quality_gate(
        frame, "PPFD.SG",
        registry_path=registry,
        gap_registry_path=gap_registry,
        window_sessions=60,
        preserve_expected_sessions=True,
        as_of="2026-11-26T16:00:00Z",
    )
    assert quality.quality_status == "PASS_WITH_EXCLUSIONS"
    assert "ETF_ETC_FRESHNESS_GRACE_48H" in quality.reason_codes


def test_central_quality_gate_does_not_waive_structural_invalidity_during_grace(
    tmp_path, monkeypatch
):
    frame = _quality_frame("2026-11-23")
    frame.iloc[-1, frame.columns.get_loc("High")] = (
        frame.iloc[-1]["Low"] - 1.0
    )
    sessions = [pd.Timestamp(day).date().isoformat() for day in frame.index]
    sessions.append("2026-11-24")
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(quality_gate, "_calendar_sessions", lambda ticker, start, end: sessions)
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    registry = tmp_path / "quality.csv"
    gap_registry = tmp_path / "gaps.csv"
    registry.write_text("ticker,date,classification\\n", encoding="utf-8")
    gap_registry.write_text("ticker,missing_date,timeframe,quality_class\\n", encoding="utf-8")

    _, quality = quality_gate.apply_ohlcv_quality_gate(
        frame, "TEST.PA",
        registry_path=registry,
        gap_registry_path=gap_registry,
        window_sessions=60,
        preserve_expected_sessions=True,
        as_of="2026-11-26T16:00:00Z",
        instrument_type="ETF",
    )
    assert quality.quality_status == "INCOMPLETE"
    assert "HIGH_BELOW_OPEN_OR_CLOSE" in quality.reason_codes


def test_central_quality_gate_does_not_reduce_minimum_history_for_etf_grace(
    tmp_path, monkeypatch
):
    index = pd.bdate_range(end="2026-11-23", periods=56)
    values = np.arange(56, dtype=float) + 100.0
    frame = pd.DataFrame(
        {"Open": values, "High": values + 1, "Low": values - 1, "Close": values + 0.5},
        index=index,
    )
    sessions = [pd.Timestamp(day).date().isoformat() for day in frame.index]
    sessions.append("2026-11-24")
    _install_fake_exchange_calendar(
        monkeypatch, ["2026-11-23", "2026-11-24"], "2026-11-24T16:30:00Z"
    )
    monkeypatch.setattr(quality_gate, "_calendar_sessions", lambda ticker, start, end: sessions)
    monkeypatch.setattr(
        quality_gate, "_last_completed_session_date",
        lambda ticker, as_of=None: "2026-11-24",
    )
    registry = tmp_path / "quality.csv"
    gap_registry = tmp_path / "gaps.csv"
    registry.write_text("ticker,date,classification\\n", encoding="utf-8")
    gap_registry.write_text("ticker,missing_date,timeframe,quality_class\\n", encoding="utf-8")

    _, quality = quality_gate.apply_ohlcv_quality_gate(
        frame, "TEST.PA",
        registry_path=registry,
        gap_registry_path=gap_registry,
        window_sessions=60,
        preserve_expected_sessions=True,
        as_of="2026-11-26T16:00:00Z",
        instrument_type="ETF",
    )
    assert quality.quality_status == "INCOMPLETE"
    assert "INSUFFICIENT_EXPECTED_SESSIONS" in quality.reason_codes
