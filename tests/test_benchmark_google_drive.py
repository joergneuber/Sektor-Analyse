import importlib
import sys
import types

import pandas as pd
import pytest

# The project CI installs yfinance. Keep this focused contract test importable
# in minimal local environments where market-data dependencies are absent.
if "yfinance" not in sys.modules:
    try:
        importlib.import_module("yfinance")
    except ModuleNotFoundError:
        sys.modules["yfinance"] = types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame())

import performance_benchmark as benchmark
import offene_positionen_check as check


def _sheet(title, headers, rows):
    return [[title], headers, *rows]


def test_drive_tabs_combine_open_and_archived_positions_and_preserve_short():
    open_headers = [
        "Ticker", "Name", "Steuerungsart", "Status", "Einstiegsdatum",
        "Einstieg", "Aktueller_Kurs", "Performance_Seit_Einstieg%", "Richtung",
    ]
    closed_headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Ausstiegsdatum", "Ausstiegskurs", "Richtung", "Ideen_Quelle", "Nr.",
    ]
    open_values = _sheet(
        "Offene Positionen+Check",
        open_headers,
        [["OPEN", "Open Co", "Aktiver Trade", "Offen", "10.08.2026", "100", "110", "10", "Long"]],
    )
    closed_values = _sheet(
        "Geschlossene Positionen",
        closed_headers,
        [["SHORT", "Short Co", "Verkauft", "10.08.2026", "100", "20.08.2026", "80", "Short", "Trendwende", "1"]],
    )

    combined = benchmark._combine_position_tabs(open_values, closed_values)

    assert len(combined) == 2
    assert combined.loc[combined["Ticker"] == "OPEN", "Richtung"].iloc[0] == "Long"
    assert combined.loc[combined["Ticker"] == "SHORT", "Richtung"].iloc[0] == "Short"
    assert combined.loc[combined["Ticker"] == "OPEN", "Ausstiegsdatum"].iloc[0] == ""
    assert combined.loc[combined["Ticker"] == "OPEN", "Ideen_Quelle"].iloc[0] == ""
    assert benchmark._system_return(100, 80, short=True) == pytest.approx(25.0)


def test_open_tab_fails_closed_if_direction_missing_or_invalid():
    headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Aktueller_Kurs", "Performance_Seit_Einstieg%",
    ]
    values = _sheet(
        "Offene Positionen+Check",
        headers,
        [["OPEN", "Open Co", "Offen", "10.08.2026", "100", "110", "10"]],
    )
    closed_headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Ausstiegsdatum", "Ausstiegskurs", "Richtung",
    ]
    closed = _sheet("Geschlossene Positionen", closed_headers, [])
    with pytest.raises(RuntimeError, match="fehlende Spalten: Richtung"):
        benchmark._combine_position_tabs(values, closed)


def test_tab2_rejects_non_closed_status():
    open_headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Aktueller_Kurs", "Performance_Seit_Einstieg%", "Richtung",
    ]
    closed_headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Ausstiegsdatum", "Ausstiegskurs", "Richtung",
    ]
    open_values = _sheet("Offene Positionen+Check", open_headers, [])
    closed_values = _sheet(
        "Geschlossene Positionen", closed_headers,
        [["BAD", "Bad Co", "Offen", "10.08.2026", "100", "20.08.2026", "80", "Long"]],
    )
    with pytest.raises(RuntimeError, match="unerwartete Statuswerte"):
        benchmark._combine_position_tabs(open_values, closed_values)


def test_make_row_carries_tracker_direction_into_output_contract():
    tech = types.SimpleNamespace(
        close=90.0, data_quality="PASS", state="Trend", trend="Bärisch",
        confluence="", resistance1=None, resistance1_label="",
        fib_status="", fib1=None, fib2=None, fib3=None,
        channel_upper=None, measured_move=None, round_number=None,
        support1=None, support2=None, resistance2=None, resistance2_label="",
        breakout_status="", abc_status="", formation="", major_resistance=None,
        major_resistance_label="", overextension="", sector_rs="", retest_support=None,
        note="",
    )
    row = {
        "Ticker": "SHORT", "Name": "Short Co", "Einstieg": "100",
        "Aktueller_Kurs": "90", "Richtung": "Short", "Ideen_Quelle": "Trendwende",
    }

    result = check.make_row(row, tech)

    assert "Richtung" in check.HEADERS
    assert result["Richtung"] == "Short"
    assert result["Performance_Seit_Einstieg%"] == pytest.approx(10.0)


def test_make_row_rejects_missing_tracker_direction():
    tech = types.SimpleNamespace(
        close=90.0, data_quality="PASS", state="Trend", trend="Bärisch",
        confluence="", resistance1=None, resistance1_label="",
        fib_status="", fib1=None, fib2=None, fib3=None,
        channel_upper=None, measured_move=None, round_number=None,
        support1=None, support2=None, resistance2=None, resistance2_label="",
        breakout_status="", abc_status="", formation="", major_resistance=None,
        major_resistance_label="", overextension="", sector_rs="", retest_support=None,
        note="",
    )
    with pytest.raises(RuntimeError, match="keine gültige Richtung"):
        check.make_row({"Ticker": "SHORT", "Einstieg": "100"}, tech)


def test_loader_has_no_local_csv_fallback(monkeypatch):
    monkeypatch.delenv("GDRIVE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CSV-Fallback ist bewusst deaktiviert"):
        benchmark._load_positions()



def test_loader_finds_one_master_and_reads_both_tabs(monkeypatch):
    open_headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Aktueller_Kurs", "Performance_Seit_Einstieg%", "Richtung",
    ]
    closed_headers = [
        "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
        "Ausstiegsdatum", "Ausstiegskurs", "Richtung",
    ]
    open_values = _sheet(
        "Offene Positionen+Check", open_headers,
        [["OPEN", "Open Co", "Offen", "10.08.2026", "100", "110", "10", "Long"]],
    )
    closed_values = _sheet(
        "Geschlossene Positionen", closed_headers,
        [["CLOSED", "Closed Co", "Gestoppt", "10.08.2026", "100", "20.08.2026", "90", "Short"]],
    )

    class _Credentials:
        expired = False
        refresh_token = None
        valid = True

        @classmethod
        def from_authorized_user_info(cls, info):
            assert info == {"token": "fixture"}
            return cls()

    class _Request:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class _DriveFiles:
        def list(self, **kwargs):
            assert benchmark.DRIVE_NAME in kwargs["q"]
            assert benchmark.FOLDER_ID in kwargs["q"]
            return _Request({"files": [{
                "id": "master-id",
                "name": benchmark.DRIVE_NAME,
                "mimeType": benchmark.GOOGLE_SHEETS_MIME_TYPE,
            }]})

    class _Drive:
        def files(self):
            return _DriveFiles()

    class _Values:
        def get(self, **kwargs):
            assert kwargs["spreadsheetId"] == "master-id"
            if "Offene Positionen+Check" in kwargs["range"]:
                return _Request({"values": open_values})
            if "Geschlossene Positionen" in kwargs["range"]:
                return _Request({"values": closed_values})
            raise AssertionError(kwargs["range"])

    class _SheetsApi:
        def values(self):
            return _Values()

    class _Sheets:
        def spreadsheets(self):
            return _SheetsApi()

    google = types.ModuleType("google")
    oauth2 = types.ModuleType("google.oauth2")
    credentials = types.ModuleType("google.oauth2.credentials")
    credentials.Credentials = _Credentials
    auth = types.ModuleType("google.auth")
    transport = types.ModuleType("google.auth.transport")
    requests = types.ModuleType("google.auth.transport.requests")
    requests.Request = lambda: object()
    googleapiclient = types.ModuleType("googleapiclient")
    discovery = types.ModuleType("googleapiclient.discovery")

    def _build(api, version, **kwargs):
        assert version == "v3" if api == "drive" else version == "v4"
        return _Drive() if api == "drive" else _Sheets()

    discovery.build = _build
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests)
    monkeypatch.setitem(sys.modules, "googleapiclient", googleapiclient)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setenv("GDRIVE_TOKEN", '{"token":"fixture"}')

    positions = benchmark._load_positions()

    assert positions["Ticker"].tolist() == ["OPEN", "CLOSED"]
    assert positions["Richtung"].tolist() == ["Long", "Short"]



def test_calculation_uses_short_direction_for_closed_archive_trade(monkeypatch, tmp_path):
    today = pd.Timestamp.now().normalize()
    start = pd.Timestamp("2026-08-10")
    exit_day = pd.Timestamp("2026-08-20")
    history = pd.Series(
        [100.0, 105.0, 110.0],
        index=pd.DatetimeIndex([start, exit_day, today]),
    )
    monkeypatch.setattr(benchmark, "_load_positions", lambda: pd.DataFrame([
        {
            "Ticker": "SHORT", "Name": "Short Co", "Richtung": "Short",
            "Ideen_Quelle": "Trendwende", "Status": "Verkauft",
            "Einstiegsdatum": "10.08.2026", "Einstieg": "100",
            "Ausstiegsdatum": "20.08.2026", "Ausstiegskurs": "80",
            "Aktueller_Kurs": "80", "Performance_Seit_Einstieg%": "-20",
        },
    ]))
    monkeypatch.setattr(
        benchmark, "_download_close",
        lambda ticker, start_date, end_date: history.copy(),
    )
    monkeypatch.setattr(benchmark, "OUTPUT_FILE", tmp_path / "Benchmark_Live.txt")

    result = benchmark.calculate_benchmark()

    assert result["trade_count"] == 1
    assert "SHORT | Short | Trendwende" in result["text"]
    assert "System +25,00%" in result["text"]
    assert (tmp_path / "Benchmark_Live.txt").exists()
