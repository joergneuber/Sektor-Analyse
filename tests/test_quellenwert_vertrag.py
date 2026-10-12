import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from quellenwert_vertrag import (
    decimal_places,
    display_tolerance,
    four_week_return,
    same_measurement_basis,
    values_match_display,
    find_exact_labeled_line,
    extract_four_week_percent,
)


def test_display_precision_accepts_normal_rounding():
    assert values_match_display(3.13, 3.129, "3,13")
    assert values_match_display(6.77, 6.7735, "6,77")
    assert values_match_display(1.1206, 1.120574, "1,1206")


def test_display_precision_rejects_real_conflict():
    assert not values_match_display(2803.38, 2806.98, "2.803,38")
    assert not values_match_display(334.131, 337.765, "334,1310")


def test_precision_uses_displayed_decimal_count():
    assert decimal_places("334,1310") == 4
    assert decimal_places("2.803,38") == 2
    assert display_tolerance("3,13") == 0.005


def test_live_quote_and_close_are_not_same_basis():
    assert not same_measurement_basis("price", "intraday", "price", "last_close")
    assert same_measurement_basis("price", "last_close", "price", "last_close")


def test_four_week_return_uses_20_intervals_not_20_rows():
    closes = list(range(100, 121))  # 21 closes, 20 completed intervals
    result = four_week_return(closes, sessions=20)
    assert result is not None
    assert abs(result - ((120 / 100 - 1) * 100)) < 1e-9
    assert four_week_return(list(range(100, 120)), sessions=20) is None


def test_four_week_return_rejects_zero_base():
    assert four_week_return([0, 1], sessions=1) is None


def test_exact_label_does_not_confuse_core_cpi_with_cpi():
    source = "CPI: 334.1310 | YOY=+3.35%\nCore CPI: 337.7650 | YOY=+2.45%"
    assert find_exact_labeled_line(source, "CPI").startswith("CPI:")
    assert find_exact_labeled_line(source, "Core CPI").startswith("Core CPI:")

def test_four_week_extractor_ignores_52_week_distance():
    line = "Gold: Kurs 4196.20 | -3.6% 4W | nahe 52W-Tief (3901.30, 7.6% darüber)"
    assert extract_four_week_percent(line) == "-3.6"
    assert extract_four_week_percent("Gold: 7.6% darüber zum 52W-Tief") is None
    assert extract_four_week_percent("Silber: Kurs 60.81 | -7.1% in den letzten 4 Wochen") == "-7.1"


def test_metal_four_week_source_rows_detect_source_conflict():
    from quellenwert_vertrag import extract_metal_four_week_value
    market = "- Gold: Kurs 4196.20 | +0.9% 5T | -3.6% 4W | nahe 52W-Tief (3901.30, 7.6% darüber)"
    detail = "  Gold: Kurs 4196.20 | -4.6% in den letzten 4 Wochen - nahe seinem 52-Wochen-Tief (3901.30, 7.6% darüber)"
    assert extract_metal_four_week_value(market, "Gold") == ("-3.6", -3.6)
    assert extract_metal_four_week_value(detail, "Gold") == ("-4.6", -4.6)
    assert extract_metal_four_week_value("Gold: Kurs 4196.20 | 7.6% darüber zum 52W-Tief", "Gold") is None
