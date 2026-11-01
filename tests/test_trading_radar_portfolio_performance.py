from pathlib import Path
from trading_radar_portfolio_performance import calculate_portfolio_performance, format_performance_block


def _write(path: Path, rows: str):
    path.write_text(
        "Ticker;Waehrung;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegsdatum;Ausstiegskurs\n" + rows,
        encoding="utf-8",
    )
    return path


def test_eur_plus_usd_trade_returns_are_equally_averaged(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;105;Offen;;\n"
        "BBB;USD;Long;02.01.2026;100;102;Verkauft;03.01.2026;102\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 2
    assert result["portfolio_total_pct"] == 3.5
    assert result["aggregation"] == "arithmetic_mean_of_individual_trade_return_percentages"
    block = format_performance_block(result)
    assert "arithmetischen Mittelwert" in block
    assert "keine Währungsumrechnung" in block


def test_positive_and_negative_trade_returns_are_averaged(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "BBB;USD;Short;02.01.2026;100;105;Verkauft;03.01.2026;105\n")
    result = calculate_portfolio_performance([source])
    # Long +10%; short +? entry 100, cover at 105 => -5%.
    assert result["status"] == "AVAILABLE"
    assert result["portfolio_total_pct"] == 2.5


def test_one_unit_per_trade_and_open_average_remains_separate(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "BBB;EUR;Long;02.01.2026;200;210;Verkauft;03.01.2026;210\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert (result["trade_count"], result["closed_count"], result["open_count"]) == (2, 1, 1)
    assert result["unit_per_trade"] == 1 and result["costs"] == 0
    assert result["portfolio_total_pct"] == 7.5
    assert result["average_position_pct"] == 10.0


def test_missing_currency_and_mixed_currencies_do_not_block_percentage_calculation(tmp_path):
    source = tmp_path / "positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status\n"
        "AAA;Long;01.01.2026;100;105;Offen\n"
        "BBB;Short;02.01.2026;100;98;Offen\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["portfolio_total_pct"] == 3.5


def test_open_trade_without_current_price_is_not_guessed(tmp_path):
    source = _write(tmp_path / "positions.csv", "AAA;EUR;Long;01.01.2026;100;;Offen;;\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["portfolio_total_pct"] is None
    assert any("aktueller Kurs" in issue for issue in result["issues"])


def test_duplicate_ticker_entry_date_within_same_source_is_ambiguous(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["duplicate_conflicts"]


def test_conflicting_entry_for_same_identity_fails_closed(tmp_path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    header = "Ticker;Trade_ID;Waehrung;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegsdatum;Ausstiegskurs\n"
    first.write_text(header + "AAA;T-1;EUR;Long;01.01.2026;100;110;Offen;;\n", encoding="utf-8")
    second.write_text(header + "AAA;T-1;EUR;Long;01.01.2026;101;105;Offen;;\n", encoding="utf-8")
    result = calculate_portfolio_performance([first, second])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["duplicate_conflicts"]


def test_missing_direction_is_not_assumed_long(tmp_path):
    source = tmp_path / "positions.csv"
    source.write_text("Ticker;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status\nAAA;01.01.2026;100;90;Offen\n", encoding="utf-8")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert any("Richtung" in issue for issue in result["issues"])


def test_invalid_nonempty_rows_are_not_silently_skipped(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "BBB;EUR;;02.01.2026;200;210;Offen;;\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert any("BBB" in issue and "Richtung" in issue for issue in result["issues"])


def test_unparseable_entry_date_is_not_accepted_as_identity(tmp_path):
    source = _write(tmp_path / "positions.csv", "AAA;EUR;Long;unknown;100;110;Offen;;\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert any("Einstiegsdatum" in issue for issue in result["issues"])


def test_closed_snapshot_supersedes_same_open_trade(tmp_path):
    open_path = _write(tmp_path / "open.csv", "AAA;EUR;Long;01.01.2026;100;105;Offen;;\n")
    closed_path = _write(tmp_path / "closed.csv", "AAA;USD;Long;01.01.2026;100;106;Verkauft;02.01.2026;106\n")
    result = calculate_portfolio_performance([open_path, closed_path])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 1 and result["closed_count"] == 1
    assert result["portfolio_total_pct"] == 6.0


def test_66_trade_fixture_sums_52_closed_and_14_open_trade_returns(tmp_path):
    closed = ["Ticker;Waehrung;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegsdatum;Ausstiegskurs"]
    from datetime import date, timedelta
    for i in range(52):
        entry_date = date(2026, 1, 1) + timedelta(days=i)
        exit_date = entry_date + timedelta(days=1)
        closed.append(f"C{i:02d};EUR;Long;{entry_date:%Y-%m-%d};100;101;Verkauft;{exit_date:%Y-%m-%d};101")
    opened = ["Ticker;Waehrung;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegsdatum;Ausstiegskurs"]
    for i in range(14):
        entry_date = date(2026, 2, 1) + timedelta(days=i)
        opened.append(f"O{i:02d};USD;Long;{entry_date:%Y-%m-%d};100;102;Offen;;")
    cp = tmp_path / "Geschlossene Positionen_Tab2.csv"; cp.write_text("\n".join(closed)+"\n", encoding="utf-8")
    op = tmp_path / "Offene Positionen+Check.csv"; op.write_text("\n".join(opened)+"\n", encoding="utf-8")
    result = calculate_portfolio_performance([op, cp])
    assert result["status"] == "AVAILABLE"
    assert (result["trade_count"], result["closed_count"], result["open_count"]) == (66, 52, 14)
    assert abs(result["portfolio_total_pct"] - (80.0 / 66.0)) < 1e-9


def test_equal_weighting_is_independent_of_entry_price_and_includes_open_and_closed(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "BBB;USD;Long;02.01.2026;1000;900;Verkauft;03.01.2026;900\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 2
    assert result["portfolio_total_pct"] == 0.0


def test_two_same_ticker_same_day_entries_at_different_prices_remain_distinct(tmp_path):
    source = _write(tmp_path / "positions.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "AAA;EUR;Long;01.01.2026;200;180;Offen;;\n")
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 2
    assert abs(result["portfolio_total_pct"] - 0.0) < 1e-9


def test_incomplete_lower_priority_duplicate_does_not_invalidate_enriched_trade(tmp_path):
    enriched = _write(tmp_path / "Offene Positionen+Check.csv",
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n")
    raw = _write(tmp_path / "Offene_Positionen.csv",
        "AAA;EUR;Long;01.01.2026;100;;Offen;;\n")
    result = calculate_portfolio_performance([enriched, raw])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 1
    assert result["portfolio_total_pct"] == 10.0



def test_optionsschein_uses_option_return_not_underlying_return(tmp_path):
    source = tmp_path / "options.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Produkt_Typ;OS_Einstiegskurs;OS_Manueller_Kurs;OS_Performance%\n"
        "AAA;Long;01.01.2026;100;150;Offen;Optionsschein;5,00;3,50;-30,00\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["portfolio_total_pct"] == -30.0
    assert result["records"][0]["entry"] == 5.0


def test_optionsschein_without_own_return_fails_closed(tmp_path):
    source = tmp_path / "options.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Produkt_Typ;OS_Einstiegskurs\n"
        "AAA;Long;01.01.2026;100;150;Offen;Optionsschein;5,00\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["portfolio_total_pct"] is None
    assert any("Optionsschein-Einstiegs- oder Optionsschein-Ausstiegskurs" in issue for issue in result["issues"])


def test_closed_optionsschein_return_is_derived_from_own_exit_quote(tmp_path):
    source = tmp_path / "closed_option.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Ausstiegskurs;Status;Produkt_Typ;OS_Einstiegskurs;OS_Aktueller_Kurs;OS_Performance%\n"
        "FCX;Long;19.08.2026;66.32;69.05;Gestoppt;Optionsschein;1.19;1.29;\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert abs(result["portfolio_total_pct"] - ((1.29 - 1.19) / 1.19 * 100)) < 1e-9
    assert result["records"][0]["entry"] == 1.19


def test_incomplete_open_set_does_not_emit_partial_average(tmp_path):
    source = tmp_path / "open.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status\\n"
        "AAA;Long;01.01.2026;100;110;Offen\\n"
        "BBB;Long;02.01.2026;100;;Offen\\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["portfolio_total_pct"] is None
    assert result["average_position_pct"] is None


def test_external_eunl_benchmark_is_excluded_from_equal_weighted_system_trades(tmp_path):
    source = _write(
        tmp_path / "positions.csv",
        "EUNL.DE;EUR;Long;01.01.2022;100;120;Offen;;\n"
        "AAA;EUR;Long;01.01.2026;100;110;Offen;;\n"
        "BBB;USD;Long;02.01.2026;100;90;Verkauft;03.01.2026;90\n",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 2
    assert result["portfolio_total_pct"] == 0.0
    assert result["benchmark_excluded_tickers"] == ["EUNL.DE"]
    assert result["benchmark_excluded_count"] == 1
    block = format_performance_block(result)
    assert "Benchmarkpositionen ausgeschlossen: EUNL.DE (Anzahl=1)" in block


def test_external_benchmark_ticker_cannot_be_reintroduced_by_role_label(tmp_path):
    source = tmp_path / "positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Trade_Rolle\n"
        "EUNL.DE;Long;01.01.2026;100;110;Offen;SYSTEM_TRADE\n"
        "AAA;Long;02.01.2026;100;105;Offen;SYSTEM_TRADE\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["trade_count"] == 1
    assert result["portfolio_total_pct"] == 5.0
    assert result["benchmark_excluded_tickers"] == ["EUNL.DE"]



def test_stored_trade_return_must_reconcile_with_entry_and_exit_prices(tmp_path):
    source = tmp_path / "conflicting_return.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Ausstiegskurs;Status;Ausstiegsdatum;Performance_Seit_Einstieg%\n"
        "AAA;Long;01.01.2026;100;110;Verkauft;02.01.2026;-15.00\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["portfolio_total_pct"] is None
    assert any("widerspricht der aus Einstieg" in issue for issue in result["issues"])


def test_stored_trade_return_with_rounding_difference_is_accepted(tmp_path):
    source = tmp_path / "rounded_return.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Ausstiegskurs;Status;Ausstiegsdatum;Performance_Seit_Einstieg%\n"
        "AAA;Long;01.01.2026;100;110;Verkauft;02.01.2026;10.05\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "AVAILABLE"
    assert result["portfolio_total_pct"] == 10.0


def test_unusable_open_source_positions_are_counted_separately_from_valid_open_trades(tmp_path):
    source = tmp_path / "open_positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status\n"
        "AAA;;01.01.2026;100;110;Offen\n"
        "BBB;Long;02.01.2026;200;;Offen\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    assert result["status"] == "NOT_AVAILABLE"
    assert result["open_count"] == 0
    assert result["unusable_open_count"] == 2
    assert result["unusable_open_tickers"] == ["AAA", "BBB"]
    block = format_performance_block(result)
    assert "nicht aggregierbare offene Quellpositionen=2 (AAA, BBB)" in block
