import datetime as dt
import openpyxl
import pandas as pd
import pytest

from positionen_sheet_upload import erstelle_sheet_upload_stream


NUMERIC = ["Einstieg", "Stop", "TP1", "TP2", "Ausstiegskurs",
           "Aktueller_Kurs", "Performance_Seit_Einstieg%",
           "OS_Einstiegskurs", "OS_Aktueller_Kurs", "OS_Performance%"]


def test_decimal_comma_and_dates_are_typed_xlsx_cells(tmp_path):
    csv = tmp_path / "positions.csv"
    csv.write_text(
        "Ticker;Einstieg;Stop;Einstiegsdatum;Ausstiegsdatum;Aktueller_Kurs\n"
        "LSUG.MI;2,06;1,90;15.07.2026;2026-08-01;2,35\n",
        encoding="utf-8-sig",
    )
    stream = erstelle_sheet_upload_stream(
        str(csv), NUMERIC,
        erforderliche_spalten=("Ticker", "Einstiegsdatum", "Status"),
    ) if False else erstelle_sheet_upload_stream(str(csv), NUMERIC)
    sheet = openpyxl.load_workbook(stream, data_only=False).active
    assert sheet["B2"].value == pytest.approx(2.06)
    assert sheet["B2"].data_type == "n"
    assert sheet["C2"].value == pytest.approx(1.90)
    assert sheet["C2"].data_type == "n"
    assert sheet["D2"].value == dt.datetime(2026, 7, 15)
    assert sheet["D2"].is_date
    assert sheet["D2"].number_format == "dd.mm.yyyy"
    assert sheet["E2"].value == dt.datetime(2026, 8, 1)
    assert sheet["F2"].value == pytest.approx(2.35)


def test_empty_date_and_numeric_cells_remain_empty(tmp_path):
    csv = tmp_path / "positions.csv"
    csv.write_text("Ticker;Einstieg;Einstiegsdatum\nABC;;\n", encoding="utf-8-sig")
    sheet = openpyxl.load_workbook(
        erstelle_sheet_upload_stream(str(csv), NUMERIC), data_only=False
    ).active
    assert sheet["B2"].value is None
    assert sheet["C2"].value is None


def test_invalid_numeric_value_aborts(tmp_path):
    csv = tmp_path / "positions.csv"
    csv.write_text("Ticker;Einstieg\nABC;2,06x\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="Ungueltige Werte"):
        erstelle_sheet_upload_stream(str(csv), ["Einstieg"])


def test_invalid_date_aborts(tmp_path):
    csv = tmp_path / "positions.csv"
    csv.write_text("Ticker;Einstiegsdatum\nABC;31.02.2026\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="Ungueltiges Datum"):
        erstelle_sheet_upload_stream(str(csv), [], datumsspalten=("Einstiegsdatum",))


def test_invalid_iso_date_does_not_fall_back_to_dayfirst_parsing(tmp_path):
    csv = tmp_path / "positions.csv"
    csv.write_text(
        "Ticker;Einstiegsdatum\nABC;2026-31-01\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError, match="Ungueltiges Datum"):
        erstelle_sheet_upload_stream(
            str(csv), [], datumsspalten=("Einstiegsdatum",)
        )


@pytest.mark.parametrize(
    "date_value",
    ["2026-08-01T12:30:00Z", "2026-08-01T12:30:00+02:00"],
)
def test_timezone_aware_iso_datetime_is_rejected_for_xlsx_dates(tmp_path, date_value):
    csv = tmp_path / "positions.csv"
    csv.write_text(
        f"Ticker;Einstiegsdatum\nABC;{date_value}\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError, match="Zeitzonenangaben sind"):
        erstelle_sheet_upload_stream(
            str(csv), [], datumsspalten=("Einstiegsdatum",)
        )


def test_shared_date_parser_is_used_by_xlsx_uploader():
    from positionen_sheet_upload import _parse_date

    assert _parse_date("46235", "Einstiegsdatum", 2).date().isoformat() == "2026-08-01"
    assert _parse_date("", "Einstiegsdatum", 2) is None
    with pytest.raises(ValueError, match="Ungueltiges Datum"):
        _parse_date("2026-31-01", "Einstiegsdatum", 2)


def test_excel_date_serial_in_price_field_aborts_xlsx_upload(tmp_path):
    csv = tmp_path / "positions_bad_price.csv"
    csv.write_text(
        "Ticker;Name;Status;Einstieg;Stop;Einstiegsdatum\nLSUG.MI;Sugar 2x;Offen;46175;2,06;18.03.2026\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError, match="Excel-Datumsseriennummer"):
        erstelle_sheet_upload_stream(str(csv), NUMERIC)


def _load_tracker_with_stubbed_integrations(monkeypatch):
    """Load the tracker without contacting Google, Alpaca, Yahoo or Twelve Data."""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    names = [
        "yfinance", "google", "google.oauth2", "google.oauth2.credentials",
        "google.auth", "google.auth.transport", "google.auth.transport.requests",
        "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
        "alpaca", "alpaca.data", "alpaca.data.historical", "alpaca.data.enums",
        "alpaca.data.requests", "alpaca.data.timeframe", "os_kurse",
    ]
    modules = {name: types.ModuleType(name) for name in names}
    modules["yfinance"].Ticker = object
    modules["google.oauth2.credentials"].Credentials = object
    modules["google.auth.transport.requests"].Request = object
    modules["googleapiclient.discovery"].build = lambda *a, **k: None
    modules["googleapiclient.http"].MediaIoBaseDownload = object
    modules["googleapiclient.http"].MediaIoBaseUpload = object
    modules["alpaca.data.historical"].StockHistoricalDataClient = object
    modules["alpaca.data.historical"].CryptoHistoricalDataClient = object
    modules["alpaca.data.enums"].Adjustment = object
    modules["alpaca.data.requests"].StockBarsRequest = object
    modules["alpaca.data.requests"].CryptoBarsRequest = object
    modules["alpaca.data.timeframe"].TimeFrame = object
    modules["os_kurse"].aktualisiere_optionsscheine = lambda *a, **k: None
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "positionen_tracker_schema_test", root / "positionen_tracker.py"
    )
    tracker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tracker)
    return tracker


class _FakeDownloadRequest:
    def __init__(self, payload):
        self.payload = payload


class _FakeMediaDownloader:
    def __init__(self, buffer, request):
        self.buffer = buffer
        self.request = request
        self.done = False

    def next_chunk(self):
        if not self.done:
            self.buffer.write(self.request.payload)
            self.done = True
        return None, True


class _FakeDriveFiles:
    def __init__(self, payload):
        self.payload = payload

    def export(self, **kwargs):
        return _FakeDownloadRequest(self.payload)

    def get_media(self, **kwargs):
        return _FakeDownloadRequest(self.payload)


class _FakeDriveService:
    def __init__(self, payload):
        self._files = _FakeDriveFiles(payload)

    def files(self):
        return self._files


def test_existing_drive_source_with_only_ticker_and_status_fails_closed(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = b"Ticker,Status\nLSUG.MI,Offen\n"
    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(
            _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
        )


def test_existing_drive_source_missing_one_essential_price_field_fails_closed(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = b"Ticker,Status,Einstiegsdatum,Einstieg\nLSUG.MI,Offen,18.03.2026,2.06\n"
    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(
            _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
        )


def test_existing_empty_drive_export_fails_closed(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = b"Ticker,Status,Einstiegsdatum,Einstieg,Stop\n"
    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(
            _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
        )


def test_existing_legacy_drive_schema_with_essential_columns_remains_compatible(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = (
        b"Ticker,Name,Status,Einstiegsdatum,Einstieg,Stop,TP1\n"
        b"LSUG.MI,Lyxor ETF,Offen,18.03.2026,2.06,2.06,3.00\n"
    )
    frame = tracker.lade_positionen_herunter(
        _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
    )
    assert frame.loc[0, "Ticker"] == "LSUG.MI"
    assert frame.loc[0, "Stop"] == 2.06
    assert "TP2" in frame.columns
    assert frame.loc[0, "TP2"] == ""



def test_drive_export_normalizes_german_decimal_commas_before_open_position_validation(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = (
        b"Ticker,Name,Status,Einstiegsdatum,Einstieg,Stop,TP1\n"
        b'VNA.DE,"Vanguard Example ETF",Offen,2026-03-18,"26,14","24,00","30,00"\n'
    )

    frame = tracker.lade_positionen_herunter(
        _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
    )

    assert frame.loc[0, "Ticker"] == "VNA.DE"
    assert frame.loc[0, "Einstieg"] == "26.14"
    assert frame.loc[0, "Stop"] == "24.00"
    assert frame.loc[0, "TP1"] == "30.00"
    assert tracker.sicheres_float(frame.loc[0, "Einstieg"], "VNA.DE", "Einstieg") == pytest.approx(26.14)


def test_drive_export_still_rejects_malformed_german_decimal_value(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = (
        b"Ticker,Name,Status,Einstiegsdatum,Einstieg,Stop\n"
        b'VNA.DE,"Vanguard Example ETF",Offen,2026-03-18,"26,14x","24,00"\n'
    )

    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(
            _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
        )


def test_tracker_schema_requires_name_and_numeric_open_prices(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    base = {
        "Ticker": ["LSUG.MI"], "Name": ["Lyxor ETF"], "Status": ["Offen"],
        "Einstiegsdatum": ["2026-06-02"], "Einstieg": ["2.06"], "Stop": ["1.90"],
    }
    import pandas as pd
    valid = pd.DataFrame(base)
    assert tracker.validate_position_source_schema(valid.copy()).equals(valid)

    for column in ("Name", "Einstieg", "Stop"):
        broken = valid.drop(columns=[column])
        with pytest.raises(ValueError, match="Pflichtspalten fehlen"):
            tracker.validate_position_source_schema(broken)

    for entry, stop in (
        ("abc", "1.90"), ("2.06", "abc"),
        ("46175", "1.90"), ("2.06", "46175.5"),
        ("2,06", "1.90"), ("2.06", "inf"), ("0", "1.90"),
    ):
        broken = valid.copy()
        broken.loc[0, "Einstieg"] = entry
        broken.loc[0, "Stop"] = stop
        with pytest.raises(ValueError):
            tracker.validate_position_source_schema(broken)



def test_drive_export_with_duplicate_headers_fails_closed(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = (
        b"Ticker,Name,name,Status,Einstiegsdatum,Einstieg,Stop\n"
        b"LSUG.MI,Lyxor,Lyxor,Offen,2026-06-02,2.06,1.90\n"
    )
    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(
            _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
        )

def test_tracker_schema_keeps_closed_legacy_and_instruction_rows_compatible(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    import pandas as pd
    frame = pd.DataFrame([
        {"Ticker": "ANLEITUNG", "Name": "Hinweis", "Status": "", "Einstiegsdatum": "", "Einstieg": "", "Stop": ""},
        {"Ticker": "OLD.DE", "Name": "Legacy", "Status": "Gestoppt", "Einstiegsdatum": "", "Einstieg": "10.00", "Stop": "9.00"},
    ])
    result = tracker.validate_position_source_schema(frame)
    assert len(result) == 2

    closed_only = pd.DataFrame({
        "Ticker": ["OLD.DE"], "Status": ["Verkauft"],
        "Einstieg": ["46175"], "Stop": ["46175.5"],
    })
    assert len(tracker.validate_position_source_schema(closed_only)) == 1
    instruction_only = pd.DataFrame({"Ticker": ["ANLEITUNG"], "Status": [""]})
    assert len(tracker.validate_position_source_schema(instruction_only)) == 1

def test_first_run_without_existing_drive_file_still_returns_empty_schema(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    frame = tracker.lade_positionen_herunter(None, None, tracker.SHEET_MIME)
    assert frame.empty
    assert list(frame.columns) == tracker.SPALTEN


def test_existing_drive_source_with_required_headers_but_missing_stop_value_fails_closed(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = (
        b"Ticker,Name,Status,Einstiegsdatum,Einstieg,Stop\n"
        b"LSUG.MI,Lyxor ETF,Offen,2026-06-02,2.06,\n"
    )
    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(
            _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
        )


def test_instruction_row_is_exempt_from_position_value_requirements(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "MediaIoBaseDownload", _FakeMediaDownloader)
    payload = (
        b"Ticker,Name,Status,Einstiegsdatum,Einstieg,Stop\n"
        b"ANLEITUNG,,,,,\n"
        b"LSUG.MI,Lyxor ETF,Offen,2026-06-02,2.06,2.06\n"
    )
    frame = tracker.lade_positionen_herunter(
        _FakeDriveService(payload), "existing-id", tracker.SHEET_MIME
    )
    assert frame.loc[0, "Ticker"] == "ANLEITUNG"
    assert frame.loc[1, "Ticker"] == "LSUG.MI"


def test_tracker_sheet_column_widths_are_reapplied_on_each_formatting_pass(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)

    class _Request:
        def execute(self):
            return {}

    class _Spreadsheets:
        def __init__(self):
            self.bodies = []
        def get(self, **kwargs):
            return _RequestWithResult({
                "sheets": [{"properties": {
                    "sheetId": 321, "title": "Offene_Positionen"
                }}]
            })
        def batchUpdate(self, **kwargs):
            self.bodies.append(kwargs["body"])
            return _Request()

    class _RequestWithResult:
        def __init__(self, result):
            self.result = result
        def execute(self):
            return self.result

    class _SheetsService:
        def __init__(self):
            self.api = _Spreadsheets()
        def spreadsheets(self):
            return self.api

    fake_service = _SheetsService()
    monkeypatch.setattr(tracker, "build", lambda *args, **kwargs: fake_service)

    # Der Tracker ruft diese Formatierung nach jedem erfolgreichen Datei-Upload auf.
    tracker.friere_erste_zwei_zeilen_ein(object(), "spreadsheet-id")
    tracker.friere_erste_zwei_zeilen_ein(object(), "spreadsheet-id")

    assert len(fake_service.api.bodies) == 2
    for body in fake_service.api.bodies:
        requests = body["requests"]
        width_requests = [
            request["updateDimensionProperties"]
            for request in requests
            if "updateDimensionProperties" in request
            and request["updateDimensionProperties"]["range"]["dimension"] == "COLUMNS"
        ]
        assert len(width_requests) == len(tracker.SPALTEN)
        for column, pixel_width in tracker.SPALTENBREITEN.items():
            index = tracker.SPALTEN.index(column)
            matching = [
                request for request in width_requests
                if request["range"]["startIndex"] == index
                and request["range"]["endIndex"] == index + 1
            ]
            assert matching
            assert matching[0]["properties"]["pixelSize"] == pixel_width

        wrapped = [
            request["repeatCell"]
            for request in requests
            if "repeatCell" in request
            and request["repeatCell"]["cell"].get("userEnteredFormat", {}).get("wrapStrategy") == "WRAP"
        ]
        assert len(wrapped) == 3



@pytest.mark.parametrize(
    "direction,stop,current,expected",
    [
        ("Long", 90.0, 125.0, 25.0),
        ("Short", 110.0, 90.0, 10.0),
    ],
)
def test_tracker_recalculates_performance_from_stored_course_when_live_quote_missing(
    monkeypatch, direction, stop, current, expected
):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    monkeypatch.setattr(tracker, "hole_aktuellen_kurs", lambda ticker, markt: None)
    # Match the tracker's real input normalization: numeric source columns
    # are numeric before the update loop assigns float values. Pandas 3.x uses
    # strict string dtypes for all-empty dict-built frames, so that fixture
    # would otherwise fail before the production method is exercised.
    frame = pd.DataFrame([{
        column: (float("nan") if column in tracker.NUMERISCHE_SPALTEN else "")
        for column in tracker.SPALTEN
    }])
    frame.loc[0, "Ticker"] = "TEST.DE"
    frame.loc[0, "Name"] = "Test AG"
    frame.loc[0, "Markt"] = "EU"
    frame.loc[0, "Richtung"] = direction
    frame.loc[0, "Status"] = "Offen"
    frame.loc[0, "Einstieg"] = 100.0
    frame.loc[0, "Aktueller_Kurs"] = current
    frame.loc[0, "Stop"] = stop
    frame.loc[0, "TP1"] = 110.0 if direction == "Long" else 95.0
    frame.loc[0, "TP2"] = 120.0 if direction == "Long" else 85.0
    frame.loc[0, "Status"] = "Offen"
    original_management = {column: frame.loc[0, column] for column in ("Stop", "TP1", "TP2", "Status")}

    updated, _ = tracker.aktualisiere_positionen(frame)

    assert updated.loc[0, "Aktueller_Kurs"] == current
    assert updated.loc[0, "Performance_Seit_Einstieg%"] == expected
    # Kein frischer Kurs: gespeicherter Kurs dient nur Anzeige/Performance,
    # darf aber weder Stop-/TP-Werte noch den Positionsstatus auslösen/verändern.
    for column, original_value in original_management.items():
        assert updated.loc[0, column] == original_value


def test_yahoo_quotes_use_30_calendar_days_and_keep_10_session_quality_gate(monkeypatch):
    """Regression: EU tickers need enough calendar coverage for ten sessions."""
    import datetime as dt
    import sys
    import types

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    requested = []
    gate_calls = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            requested.append((self.ticker, kwargs))
            return pd.DataFrame(
                {"Close": [123.45]},
                index=pd.DatetimeIndex([dt.datetime.now().date() - dt.timedelta(days=1)]),
            )

    def fake_gate(frame, ticker, **kwargs):
        gate_calls.append((ticker, kwargs))
        return frame, types.SimpleNamespace(quality_status="PASS")

    monkeypatch.setattr(tracker.yf, "Ticker", FakeTicker)
    monkeypatch.setitem(
        sys.modules, "ohlcv_quality_gate",
        types.SimpleNamespace(apply_ohlcv_quality_gate=fake_gate),
    )

    tickers = ("VNA.DE", "PPFD.SG", "SPYI.DE", "LI7U.DE", "GC=F")
    for ticker in tickers:
        market = "FUTURES" if ticker == "GC=F" else "EU"
        assert tracker.hole_aktuellen_kurs(ticker, market) == pytest.approx(123.45)

    assert [ticker for ticker, _ in requested] == list(tickers)
    for _, kwargs in requested:
        assert kwargs["interval"] == "1d"
        assert "period" not in kwargs
        start = kwargs["start"]
        assert isinstance(start, dt.datetime)
        age = dt.datetime.now() - start
        assert 29.99 <= age.total_seconds() / 86400 <= 30.01

    assert len(gate_calls) == 5
    for _, kwargs in gate_calls:
        assert kwargs == {
            "window_sessions": 10,
            "preserve_expected_sessions": True,
            "full_history": True,
        }


def test_new_eu_positions_receive_quote_performance_and_duplicate_ticker_stays_separate(monkeypatch):
    """Regression: new rows update normally; same ticker means separate trades."""
    import datetime as dt
    import sys
    import types

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    calls = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, **kwargs):
            calls.append((self.ticker, kwargs))
            close = {
                "VNA.DE": 17.92,
                "PPFD.SG": 39.84,
                "SPYI.DE": 11.80,
                "LI7U.DE": 9.61,
            }[self.ticker]
            return pd.DataFrame(
                {"Close": [close]},
                index=pd.DatetimeIndex([dt.datetime.now().date() - dt.timedelta(days=1)]),
            )

    def fake_gate(frame, ticker, **kwargs):
        return frame, types.SimpleNamespace(quality_status="PASS")

    monkeypatch.setattr(tracker.yf, "Ticker", FakeTicker)
    monkeypatch.setitem(
        sys.modules, "ohlcv_quality_gate",
        types.SimpleNamespace(apply_ohlcv_quality_gate=fake_gate),
    )

    entries = [
        ("VNA.DE", 17.07, 17.92),
        ("PPFD.SG", 37.94, 39.84),
        ("SPYI.DE", 11.24, 11.80),
        ("LI7U.DE", 9.15, 9.61),
        # Gleicher Ticker, aber eigenständiger Trade mit anderem Einstieg.
        ("VNA.DE", 16.00, 17.92),
    ]
    rows = []
    for ticker, entry, _ in entries:
        row = {
            column: (float("nan") if column in tracker.NUMERISCHE_SPALTEN else "")
            for column in tracker.SPALTEN
        }
        row.update({
            "Ticker": ticker, "Name": ticker, "Sektor": "", "Markt": "EU",
            "Waehrung": "EUR", "Richtung": "Long", "Ideen_Quelle": "Manuell",
            "Einstiegsdatum": "23.11.2026", "Einstieg": entry,
            "Stop": 1.0, "TP1": 100.0, "TP2": 120.0, "Status": "Offen",
        })
        rows.append(row)

    updated, _ = tracker.aktualisiere_positionen(pd.DataFrame(rows, columns=tracker.SPALTEN))

    assert len(updated) == 5
    assert updated["Ticker"].tolist() == [item[0] for item in entries]
    for idx, (_, entry, quote) in enumerate(entries):
        assert updated.loc[idx, "Aktueller_Kurs"] == pytest.approx(quote)
        expected = round((quote - entry) / entry * 100, 2)
        assert updated.loc[idx, "Performance_Seit_Einstieg%"] == pytest.approx(expected)
        assert updated.loc[idx, "Status"] == "Offen"
    assert [ticker for ticker, _ in calls] == ["VNA.DE", "PPFD.SG", "SPYI.DE", "LI7U.DE", "VNA.DE"]



def test_tracker_persists_drive_revision_baseline_atomically(tmp_path, monkeypatch):
    import importlib.util
    import json
    import sys
    import types
    from pathlib import Path

    # Reuse the existing import fixture so optional integrations never connect.
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "Offene_Positionen.csv"
    local.write_bytes(b"Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n")
    baseline_path = tmp_path / ".offene_positionen_drive_baseline.json"
    monkeypatch.setattr(tracker, "DRIVE_BASELINE_FILE", str(baseline_path))

    class Request:
        def execute(self):
            return {
                "id": "drive-id-1", "name": "Offene_Positionen",
                "mimeType": tracker.SHEET_MIME, "version": "42",
                "modifiedTime": "2026-11-26T20:00:00.000Z",
                "md5Checksum": "abc123", "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            assert kwargs["fileId"] == "drive-id-1"
            assert "version" in kwargs["fields"]
            return Request()

    class Service:
        def files(self):
            return Files()

    class SheetsRequest:
        def __init__(self, result): self.result = result
        def execute(self, **kwargs): return self.result

    class Sheets:
        def spreadsheets(self):
            class Spreadsheet:
                def get(self, **kwargs):
                    return SheetsRequest({"sheets": [{"properties": {"title": "Offene_Positionen", "index": 0}}]})
                class Values:
                    def batchGet(self, **kwargs):
                        return SheetsRequest({"valueRanges": [{"values": [["Ticker", "Einstiegsdatum", "Status"], ["ABC", 46023, "Offen"]]}]})
                def values(self): return self.Values()
            return Spreadsheet()

    monkeypatch.setattr(tracker, "build", lambda api, version, credentials=None: Sheets())
    tracker.speichere_drive_quell_baseline(Service(), "drive-id-1", str(local), creds=object())
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["file_id"] == "drive-id-1"
    assert payload["version"] == "42"
    assert payload["modified_time"] == "2026-11-26T20:00:00.000Z"
    assert payload["local_source_sha256"] == tracker._sha256_local_file(str(local))
    assert len(payload["remote_content_sha256"]) == 64
    assert baseline_path.stat().st_mode & 0o777 == 0o600



def test_tracker_refuses_baseline_when_remote_cells_differ_from_uploaded_csv(monkeypatch, tmp_path):
    import json

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "Offene_Positionen.csv"
    local.write_text(
        "Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n",
        encoding="utf-8",
    )
    baseline_path = tmp_path / ".offene_positionen_drive_baseline.json"
    monkeypatch.setattr(tracker, "DRIVE_BASELINE_FILE", str(baseline_path))

    class Request:
        def __init__(self, result): self.result = result
        def execute(self, **kwargs): return self.result
    class Files:
        def get(self, **kwargs):
            return Request({
                "id": "drive-id-1", "name": "Offene_Positionen",
                "mimeType": tracker.SHEET_MIME, "version": "42",
                "modifiedTime": "2026-11-26T20:00:00.000Z", "trashed": False,
            })
    class Service:
        def files(self): return Files()
    class Sheets:
        def spreadsheets(self):
            class Spreadsheet:
                def get(self, **kwargs):
                    return Request({"sheets": [{"properties": {"title": "Offene_Positionen", "index": 0}}]})
                class Values:
                    def batchGet(self, **kwargs):
                        return Request({"valueRanges": [{"values": [["Ticker", "Einstiegsdatum", "Status"], ["ABC", 46023, "Gestoppt"]]}]})
                def values(self): return self.Values()
            return Spreadsheet()
    monkeypatch.setattr(tracker, "build", lambda api, version, credentials=None: Sheets())

    with pytest.raises(RuntimeError, match="nicht mit der lokalen Positionsdatei überein"):
        tracker.speichere_drive_quell_baseline(Service(), "drive-id-1", str(local), creds=object())
    assert not baseline_path.exists()


def test_tracker_refuses_to_persist_baseline_without_remote_revision(monkeypatch, tmp_path):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "Offene_Positionen.csv"
    local.write_text("Ticker;Status\nABC;Offen\n", encoding="utf-8")

    class Request:
        def execute(self):
            return {"id": "drive-id-1", "name": "Offene_Positionen", "trashed": False}

    class Files:
        def get(self, **kwargs):
            return Request()

    class Service:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="Remote-Revision|belastbar erfasst"):
        tracker.speichere_drive_quell_baseline(Service(), "drive-id-1", str(local))


def test_tracker_remote_revision_guard_rejects_manual_edit_before_old_file_delete(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    expected = {
        "file_id": "drive-id-1", "version": "42",
        "modified_time": "2026-11-26T20:00:00.000Z",
        "mime_type": tracker.SHEET_MIME, "md5_checksum": "",
    }

    class Request:
        def execute(self):
            return {
                "id": "drive-id-1", "version": "43",
                "modifiedTime": "2026-11-26T20:01:00.000Z",
                "mimeType": tracker.SHEET_MIME, "md5Checksum": "",
                "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            return Request()

    class Service:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="extern geändert"):
        tracker._pruefe_drive_quellrevision(Service(), expected)


def test_drive_baseline_loader_rejects_local_file_drift(tmp_path, monkeypatch):
    import json
    import hashlib

    checker = __import__("offene_positionen_check")
    local = tmp_path / "Offene_Positionen.csv"
    local.write_bytes(b"Ticker;Status\nABC;Offen\n")
    baseline_path = tmp_path / ".offene_positionen_drive_baseline.json"
    payload = {
        "schema_version": 2, "file_id": "drive-id-1", "version": "42",
        "modified_time": "2026-11-26T20:00:00.000Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "local_source_sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
        "remote_content_sha256": "a" * 64,
    }
    baseline_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(checker, "DRIVE_BASELINE_FILE", str(baseline_path))
    local.write_bytes(local.read_bytes() + b"# manual local edit\n")

    with pytest.raises(RuntimeError, match="stimmt nicht mehr mit dem beim Tracker-Upload"):
        checker._load_drive_source_baseline(str(local))


def test_tracker_upload_cleans_new_temp_and_keeps_old_drive_file_on_revision_conflict(monkeypatch, tmp_path):
    import io

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "positions.csv"
    local.write_text("Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n", encoding="utf-8")
    monkeypatch.setattr(tracker, "_finde_upload_mit_token", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "erstelle_sheet_upload_stream", lambda *a, **k: io.BytesIO(b"xlsx"))
    monkeypatch.setattr(tracker, "MediaIoBaseUpload", lambda *a, **k: object())
    deleted = []

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self, **kwargs):
            return self.result

    class Files:
        def __init__(self):
            self.new_upload = None

        def create(self, body, **kwargs):
            self.new_upload = {
                "id": "new-upload-id",
                "name": body["name"],
                "mimeType": body["mimeType"],
                "parents": list(body["parents"]),
                "appProperties": dict(body["appProperties"]),
                "trashed": False,
            }
            return Request({
                "id": "new-upload-id",
                "name": "Offene_Positionen",
                "mimeType": tracker.SHEET_MIME,
                "appProperties": dict(body["appProperties"]),
            })

        def get(self, fileId, **kwargs):
            if fileId == "new-upload-id":
                return Request(dict(self.new_upload))
            return Request({
                "id": "old-source-id", "name": "Offene_Positionen",
                "mimeType": tracker.SHEET_MIME, "version": "99",
                "modifiedTime": "2026-11-26T21:00:00.000Z", "trashed": False,
            })

        def delete(self, fileId, **kwargs):
            deleted.append(fileId)
            self.new_upload["trashed"] = True
            return Request()

    class Service:
        def __init__(self):
            self._files = Files()

        def files(self):
            return self._files

    expected = {
        "file_id": "old-source-id", "version": "98",
        "modified_time": "2026-11-26T20:00:00.000Z",
        "mime_type": tracker.SHEET_MIME, "md5_checksum": "",
    }
    with pytest.raises(RuntimeError, match="extern geändert"):
        tracker.hochladen(
            Service(), str(local), "folder-id", "old-source-id",
            expected_old_revision=expected,
        )

    # Only the just-created temporary upload may be cleaned up. The externally
    # edited source remains present and is never deleted by the tracker.
    assert deleted == ["new-upload-id"]


def test_tracker_upload_captures_baseline_after_upload_and_sheet_formatting(monkeypatch, tmp_path):
    import io
    import json

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "positions.csv"
    local.write_text("Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n", encoding="utf-8")
    baseline_path = tmp_path / ".offene_positionen_drive_baseline.json"
    monkeypatch.setattr(tracker, "DRIVE_BASELINE_FILE", str(baseline_path))
    monkeypatch.setattr(tracker, "_finde_upload_mit_token", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "MediaIoBaseUpload", lambda *a, **k: object())
    order = []
    monkeypatch.setattr(tracker, "friere_erste_zwei_zeilen_ein", lambda *a, **k: order.append("format"))

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self, **kwargs):
            return self.result

    class Files:
        def create(self, **kwargs):
            order.append("create")
            return Request({"id": "new-drive-id", "name": "Offene_Positionen"})
        def get(self, **kwargs):
            order.append("baseline-get")
            return Request({
                "id": "new-drive-id", "name": "Offene_Positionen",
                "mimeType": tracker.SHEET_MIME, "version": "7",
                "modifiedTime": "2026-11-26T21:20:00.000Z", "trashed": False,
            })

    class Service:
        def files(self):
            return Files()

    class SheetsRequest:
        def __init__(self, result): self.result = result
        def execute(self, **kwargs): return self.result

    class Sheets:
        def spreadsheets(self):
            class Spreadsheet:
                def get(self, **kwargs):
                    return SheetsRequest({"sheets": [{"properties": {"title": "Offene_Positionen", "index": 0}}]})
                class Values:
                    def batchGet(self, **kwargs):
                        order.append("baseline-values")
                        return SheetsRequest({"valueRanges": [{"values": [["Ticker", "Einstiegsdatum", "Status"], ["ABC", 46023, "Offen"]]}]})
                def values(self): return self.Values()
            return Spreadsheet()

    monkeypatch.setattr(tracker, "build", lambda api, version, credentials=None: Sheets())
    new_id = tracker.hochladen(Service(), str(local), "folder-id", None, creds=object())
    assert new_id == "new-drive-id"
    assert order == ["create", "format", "baseline-get", "baseline-values"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["schema_version"] == 2
    assert baseline["file_id"] == "new-drive-id"
    assert baseline["version"] == "7"
    assert len(baseline["remote_content_sha256"]) == 64


def test_tracker_success_archives_old_master_for_14_day_retention(monkeypatch, tmp_path):
    import io

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "positions.csv"
    local.write_text(
        "Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tracker, "_finde_upload_mit_token", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "erstelle_sheet_upload_stream", lambda *a, **k: io.BytesIO(b"xlsx"))
    monkeypatch.setattr(tracker, "MediaIoBaseUpload", lambda *a, **k: object())
    monkeypatch.setattr(tracker, "_pruefe_drive_quellrevision", lambda *a, **k: {"file_id": "old-master"})
    monkeypatch.setattr(tracker, "friere_erste_zwei_zeilen_ein", lambda *a, **k: None)
    monkeypatch.setattr(tracker, "speichere_drive_quell_baseline", lambda *a, **k: None)
    cleanup_calls = []
    def fail_cleanup(service, folder_id):
        cleanup_calls.append((service, folder_id))
        raise RuntimeError("simulierter Drive-Loeschfehler")

    monkeypatch.setattr(tracker, "cleanup_expired_drive_backups", fail_cleanup)

    class Request:
        def __init__(self, result=None):
            self.result = result or {}
        def execute(self, **kwargs):
            return self.result

    class Files:
        def __init__(self):
            self.old = {
                "id": "old-master", "name": "Offene_Positionen",
                "mimeType": tracker.SHEET_MIME, "parents": ["folder-id"],
                "appProperties": {"existing": "preserved"}, "trashed": False,
            }
            self.deleted = []
        def create(self, **kwargs):
            return Request({"id": "new-master", "name": tracker.DRIVE_NAME})
        def get(self, **kwargs):
            return Request(dict(self.old))
        def update(self, **kwargs):
            self.old.update(kwargs["body"])
            return Request(dict(self.old))
        def delete(self, **kwargs):
            self.deleted.append(kwargs["fileId"])
            return Request()

    class Service:
        def __init__(self):
            self._files = Files()
        def files(self):
            return self._files

    service = Service()
    new_id = tracker.hochladen(
        service, str(local), "folder-id", "old-master",
        expected_old_revision={"file_id": "old-master"}, creds=object(),
    )

    assert new_id == "new-master"
    assert service._files.old["name"].startswith("Offene_Positionen.__BACKUP_")
    assert service._files.old["appProperties"]["nmm_backup_purpose"] == "positions-master-v1"
    assert service._files.old["appProperties"]["existing"] == "preserved"
    assert service._files.deleted == []
    assert cleanup_calls == [(service, "folder-id")]


def test_finde_datei_rejects_multiple_active_native_position_masters(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)

    class Request:
        def execute(self, **kwargs):
            return {"files": [
                {"id": "master-a", "name": tracker.DRIVE_NAME, "mimeType": tracker.SHEET_MIME},
                {"id": "master-b", "name": tracker.DRIVE_NAME, "mimeType": tracker.SHEET_MIME},
            ]}

    class Files:
        def list(self, **kwargs):
            return Request()

    class Service:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="Mehrere aktive native Positionsmaster"):
        tracker.finde_datei(Service(), "folder-id")


def test_finde_datei_preserves_single_native_plus_legacy_csv_transition(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)

    class Request:
        def execute(self, **kwargs):
            return {"files": [
                {"id": "legacy-csv", "name": tracker.DRIVE_NAME_ALT, "mimeType": "text/csv"},
                {"id": "native-sheet", "name": tracker.DRIVE_NAME, "mimeType": tracker.SHEET_MIME},
            ]}

    class Files:
        def list(self, **kwargs):
            return Request()

    class Service:
        def files(self):
            return Files()

    assert tracker.finde_datei(Service(), "folder-id") == (
        "native-sheet", tracker.SHEET_MIME
    )


def test_tracker_archive_pre_mutation_conflict_deletes_only_its_verified_upload(
    monkeypatch, tmp_path
):
    import io

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "positions.csv"
    local.write_text(
        "Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tracker, "_finde_upload_mit_token", lambda *a, **k: None)
    monkeypatch.setattr(
        tracker, "erstelle_sheet_upload_stream", lambda *a, **k: io.BytesIO(b"xlsx")
    )
    monkeypatch.setattr(tracker, "MediaIoBaseUpload", lambda *a, **k: object())

    class Request:
        def __init__(self, callback):
            self.callback = callback

        def execute(self, **kwargs):
            return self.callback()

    class Files:
        def __init__(self):
            self.old_gets = 0
            self.data = {}
            self.deleted = []
            self.updated = []

        def create(self, body, **kwargs):
            self.data["new-upload"] = {
                "id": "new-upload",
                "name": body["name"],
                "mimeType": body["mimeType"],
                "parents": list(body["parents"]),
                "appProperties": dict(body["appProperties"]),
                "trashed": False,
            }
            return Request(lambda: {
                key: self.data["new-upload"][key]
                for key in ("id", "name", "mimeType", "appProperties")
            })

        def get(self, fileId, fields=None):
            def result():
                if fileId == "old-master":
                    self.old_gets += 1
                    version = "10" if self.old_gets == 1 else "11"
                    return {
                        "id": "old-master",
                        "name": tracker.DRIVE_NAME,
                        "mimeType": tracker.SHEET_MIME,
                        "parents": ["folder-id"],
                        "appProperties": {},
                        "version": version,
                        "modifiedTime": (
                            "2026-11-26T20:00:00Z"
                            if version == "10"
                            else "2026-11-26T21:00:00Z"
                        ),
                        "md5Checksum": "old-checksum",
                        "trashed": False,
                    }
                return dict(self.data[fileId])
            return Request(result)

        def update(self, **kwargs):
            self.updated.append(kwargs)
            return Request(lambda: {})

        def delete(self, fileId):
            def result():
                self.deleted.append(fileId)
                self.data[fileId]["trashed"] = True
                return {}
            return Request(result)

    class Service:
        def __init__(self):
            self._files = Files()

        def files(self):
            return self._files

    service = Service()
    expected = {
        "file_id": "old-master",
        "version": "10",
        "modified_time": "2026-11-26T20:00:00Z",
        "mime_type": tracker.SHEET_MIME,
        "md5_checksum": "old-checksum",
    }

    with pytest.raises(RuntimeError, match="vor jeder Umbenennungs-Mutation"):
        tracker.hochladen(
            service, str(local), "folder-id", "old-master",
            expected_old_revision=expected,
        )

    assert service._files.deleted == ["new-upload"]
    assert service._files.updated == []
    assert service._files.data["new-upload"]["trashed"] is True
    # The old master was read but never renamed or deleted.
    assert service._files.old_gets == 2


def test_tracker_preserves_both_files_when_archive_mutation_state_is_ambiguous(
    monkeypatch, tmp_path
):
    import io
    from positions_drive_backups import DriveBackupArchiveMutationError

    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    local = tmp_path / "positions.csv"
    local.write_text(
        "Ticker;Einstiegsdatum;Status\nABC;01.01.2026;Offen\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tracker, "_finde_upload_mit_token", lambda *a, **k: None)
    monkeypatch.setattr(
        tracker, "erstelle_sheet_upload_stream", lambda *a, **k: io.BytesIO(b"xlsx")
    )
    monkeypatch.setattr(tracker, "MediaIoBaseUpload", lambda *a, **k: object())
    monkeypatch.setattr(tracker, "_pruefe_drive_quellrevision", lambda *a, **k: {})
    monkeypatch.setattr(
        tracker,
        "archive_drive_backup",
        lambda *a, **k: (_ for _ in ()).throw(
            DriveBackupArchiveMutationError(
                "simulierter Timeout nach Rename-Request", file_id="old-master"
            )
        ),
    )

    class Request:
        def __init__(self, result=None):
            self.result = result or {}

        def execute(self, **kwargs):
            return self.result

    class Files:
        def __init__(self):
            self.deleted = []
            self.created = []

        def create(self, body, **kwargs):
            self.created.append(dict(body))
            return Request({
                "id": "new-master",
                "name": body["name"],
                "mimeType": body["mimeType"],
                "appProperties": dict(body["appProperties"]),
            })

        def delete(self, fileId):
            self.deleted.append(fileId)
            return Request()

    class Service:
        def __init__(self):
            self._files = Files()

        def files(self):
            return self._files

    service = Service()
    with pytest.raises(RuntimeError, match="Endzustand ist aber nicht sicher bestätigbar"):
        tracker.hochladen(
            service, str(local), "folder-id", "old-master",
            expected_old_revision={"file_id": "old-master"}, creds=object(),
        )

    assert service._files.deleted == []
    assert len(service._files.created) == 1
    assert service._files.created[0]["name"] == tracker.DRIVE_NAME


def test_tracker_cleanup_refuses_to_delete_upload_with_foreign_token(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    deleted = []

    class Request:
        def execute(self, **kwargs):
            return {
                "id": "foreign-file",
                "name": tracker.DRIVE_NAME,
                "mimeType": tracker.SHEET_MIME,
                "parents": ["folder-id"],
                "appProperties": {"tracker_upload_token": "another-transaction"},
                "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            return Request()

        def delete(self, fileId):
            deleted.append(fileId)
            return Request()

    class Service:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="nicht eindeutig"):
        tracker._sicher_loesche_tracker_upload(
            Service(), "foreign-file", "folder-id", "this-transaction"
        )
    assert deleted == []


def test_finde_datei_checks_all_drive_pages_for_duplicate_native_masters(monkeypatch):
    tracker = _load_tracker_with_stubbed_integrations(monkeypatch)
    calls = []

    class Request:
        def __init__(self, result):
            self.result = result

        def execute(self, **kwargs):
            return self.result

    class Files:
        def list(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("pageToken") is None:
                return Request({
                    "files": [{
                        "id": "master-page-1",
                        "name": tracker.DRIVE_NAME,
                        "mimeType": tracker.SHEET_MIME,
                    }],
                    "nextPageToken": "page-2",
                })
            return Request({
                "files": [{
                    "id": "master-page-2",
                    "name": tracker.DRIVE_NAME,
                    "mimeType": tracker.SHEET_MIME,
                }]
            })

    class Service:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="Mehrere aktive native Positionsmaster"):
        tracker.finde_datei(Service(), "folder-id")
    assert len(calls) == 2
    assert calls[1]["pageToken"] == "page-2"
