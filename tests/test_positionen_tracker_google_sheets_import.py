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
