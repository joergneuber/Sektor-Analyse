import datetime as dt
import openpyxl
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
