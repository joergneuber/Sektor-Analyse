import datetime

import pandas as pd
import pytest

from positionen_datum import parse_position_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-01", "2026-08-01"),
        ("2026-08-01T12:30:00", "2026-08-01"),
        ("01.08.2026", "2026-08-01"),
        ("01/08/2026", "2026-08-01"),
        (46235, "2026-08-01"),
        (46235.5, "2026-08-01"),
    ],
)
def test_position_date_formats_are_normalized_without_day_month_swap(raw, expected):
    assert parse_position_date(raw, field="Einstiegsdatum").date().isoformat() == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-01T00:00:00Z",
        "2026-08-01T00:00:00+02:00",
        "2026-31-01",
        "not-a-date",
        0,
    ],
)
def test_position_date_rejects_timezone_and_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_position_date(raw, field="Einstiegsdatum")


def test_position_date_blank_values_remain_empty():
    assert parse_position_date(None) is None
    assert parse_position_date("") is None
    assert parse_position_date(float("nan")) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("46235", "2026-08-01"),
        ("46235.5", "2026-08-01"),
        ("7/15/2026", "2026-07-15"),
        ("15/7", f"{datetime.datetime.now().year:04d}-07-15"),
        ("07/08/2026", "2026-08-07"),
    ],
)
def test_numeric_text_us_and_legacy_formats_are_deterministic(raw, expected):
    assert parse_position_date(raw, field="Einstiegsdatum").date().isoformat() == expected


def test_numeric_text_outside_serial_range_is_rejected():
    with pytest.raises(ValueError, match="Numerischer Text"):
        parse_position_date("2026", field="Einstiegsdatum")


def test_datetime_objects_with_timezone_are_rejected():
    aware = datetime.datetime.fromisoformat("2026-08-01T12:00:00+02:00")
    with pytest.raises(ValueError, match="Zeitzonenangaben"):
        parse_position_date(aware, field="Einstiegsdatum")


def test_tracker_wrapper_uses_shared_parser_and_returns_german_date():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "positionen_tracker.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_datum_vereinheitlichen")
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"parse_position_date": parse_position_date}
    exec(compile(module, str(source), "exec"), namespace)
    normalize = namespace["_datum_vereinheitlichen"]
    assert normalize("2026-08-01T12:30:00") == "01.08.2026"
    assert normalize("46235") == "01.08.2026"
    with pytest.raises(ValueError, match="Zeitzonenangaben"):
        normalize("2026-08-01T00:00:00Z")


def test_recovery_history_normalizer_uses_same_canonical_date():
    import sys
    import types

    sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))
    from scripts.restore_positions import _normalize_history_value

    expected = ("date", "2026-08-01")
    assert _normalize_history_value("Einstiegsdatum", "2026-08-01") == expected
    assert _normalize_history_value("Einstiegsdatum", "01.08.2026") == expected
    assert _normalize_history_value("Einstiegsdatum", "46235") == expected
    with pytest.raises(ValueError, match="Zeitzonenangaben"):
        _normalize_history_value("Einstiegsdatum", "2026-08-01T00:00:00Z")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07/08/2026", "2026-08-07"),
        ("7/15/2026", "2026-07-15"),
        ("15.07.2026", "2026-07-15"),
        ("15-07-2026", "2026-07-15"),
        ("46235,5", "2026-08-01"),
    ],
)
def test_documented_locale_and_serial_conventions(raw, expected):
    assert parse_position_date(raw).date().isoformat() == expected


@pytest.mark.parametrize("raw", ["2026", "1234", 2026, 1234, "2026-31-01", "31.02.2026"])
def test_years_small_numbers_and_invalid_calendar_dates_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_position_date(raw)


@pytest.mark.parametrize("raw", ["2026-08-01T12:30:00Z", "2026-08-01T12:30:00+02:00",
                                  "2026-08-01T12:30:00-05:00"])
def test_all_timezone_forms_are_rejected(raw):
    with pytest.raises(ValueError, match="Zeitzonenangaben"):
        parse_position_date(raw)


def test_sheet_serial_roundtrip_preserves_naive_time():
    from positionen_datum import position_date_to_sheets_serial
    raw = "2026-08-01T12:30:00"
    serial = position_date_to_sheets_serial(raw)
    assert serial == pytest.approx(46235.5208333333)
    assert parse_position_date(serial) == parse_position_date(raw)


def test_station_adapters_share_the_same_parser_rules():
    from positionen_sheet_upload import _parse_date
    import offene_positionen_check as checker
    from scripts.restore_positions import _normalize_history_value

    assert _parse_date("2026-08-01", "Einstiegsdatum", 2).date().isoformat() == "2026-08-01"
    assert checker._date_to_sheets_serial("2026-08-01", "Einstiegsdatum", 2, strict=True) == 46235.0
    assert _normalize_history_value("Einstiegsdatum", "2026-08-01") == ("date", "2026-08-01")

    for bad in ("2026-31-01", "2026-08-01T00:00:00Z"):
        with pytest.raises(ValueError):
            _parse_date(bad, "Einstiegsdatum", 2)
        with pytest.raises(RuntimeError):
            checker._date_to_sheets_serial(bad, "Einstiegsdatum", 2, strict=True)
        with pytest.raises(ValueError):
            _normalize_history_value("Einstiegsdatum", bad)


def test_sheet_stand_date_is_validated_by_shared_parser():
    import sys
    import types

    sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))
    import offene_positionen_check as checker

    assert checker._parse_sheet_stand_date([["Stand 15.11.2026"]]) == "15.11.2026"
    with pytest.raises(RuntimeError, match="Ungueltiges Sheet-Standdatum"):
        checker._parse_sheet_stand_date([["Stand 31.02.2026"]])
    with pytest.raises(RuntimeError, match="NaN-Wert"):
        checker._parse_sheet_stand_date([["Stand nan"]])
