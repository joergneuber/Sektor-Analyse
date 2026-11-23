from trading_radar_report_gate import validate_scenario_condition_consistency
from trading_radar_szenarien import trading_radar_szenarien_ergänzen


def _report(*, include_horizon=False, xle_3m="-3.55", include_breadth=True, include_cutoff=True):
    horizon = "Zeithorizont: 1 bis 3 Monate\n" if include_horizon else ""
    breadth = "Quantitative Marktbreite: 3/11\n" if include_breadth else ""
    cutoff = "Datenstichtag: 2026-11-20\n" if include_cutoff else ""
    return (
        "1. Trading-Radar\n"
        "Marktregime: Restriktives Zinsregime mit selektiver Sektorrotation.\n"
        + breadth
        + "Volatilität VIX: 35.89\n"
        + horizon
        + "Basisszenario: NICHT ABLEITBAR – Datenlücke: Zeithorizont.\n\n"
        "2. Thesen-Update\n\n"
        "3. Ereignisradar\n\n"
        "4. Assetklassenvergleich\n"
        + cutoff
        + "Gesundheit-ETF (XLV) 1M +0.50%; 3M +1.84%\n"
        + f"Energie-ETF (XLE) 1M +0.20%; 3M {xle_3m}%\n"
        "5. Technische Kandidaten\n"
    )


def test_missing_horizon_does_not_suppress_measurable_scenario_conditions():
    result = trading_radar_szenarien_ergänzen(_report())
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert "Zeithorizont: nicht festgelegt" in result
    assert "Basisszenario: Restriktives Zinsregime" in result
    assert "Status: NICHT ERFÜLLT" in result
    assert "Status: TRIGGER AUSGELÖST" in result


def test_generated_statuses_match_the_current_measurements():
    result = trading_radar_szenarien_ergänzen(_report())
    baseline = result.split("Basisszenario:", 1)[1].split("Positives Alternativszenario:", 1)[0]
    positive = result.split("Positives Alternativszenario:", 1)[1].split("Negatives Alternativszenario:", 1)[0]
    negative = result.split("Negatives Alternativszenario:", 1)[1].split("2. Thesen-Update", 1)[0]

    assert "Status: NICHT ERFÜLLT" in baseline
    assert "Status: NICHT ERFÜLLT" in positive
    assert "Status: TRIGGER AUSGELÖST" in negative
    assert not validate_scenario_condition_consistency(result)


def test_missing_sector_measurement_is_named_and_not_claimed_as_fulfilled():
    result = trading_radar_szenarien_ergänzen(_report(xle_3m="NICHT VERFÜGBAR"))
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert "Status: NICHT ABLEITBAR" in result
    assert "Datenlücke: XLE 3M-Relative-Stärke fehlt" in result



def test_missing_market_breadth_generates_three_complete_not_ableitbar_scenarios():
    result = trading_radar_szenarien_ergänzen(_report(include_breadth=False))
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert result.count("Eintrittsbedingung:") == 3
    assert result.count("Status: NICHT ERFÜLLT") == 2
    assert result.count("Status: TRIGGER AUSGELÖST") == 1
    assert result.count("Datenlücke: Marktbreite fehlt") == 3
    assert "Marktbreite muss messbar vorliegen" in result


def test_missing_common_cutoff_marks_all_scenarios_not_ableitbar():
    result = trading_radar_szenarien_ergänzen(_report(include_cutoff=False))
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert result.count("Eintrittsbedingung:") == 3
    assert result.count("Status: NICHT ABLEITBAR") == 3
    assert result.count("Datenlücke: gemeinsamer Datenstichtag fehlt") == 3
    assert "Gemeinsamer Datenstichtag: NICHT VERFÜGBAR" in result


def test_missing_breadth_and_cutoff_are_both_named_without_inventing_values():
    result = trading_radar_szenarien_ergänzen(
        _report(include_breadth=False, include_cutoff=False)
    )
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert result.count("Status: NICHT ABLEITBAR") == 3
    assert result.count("Datenlücke: Marktbreite, gemeinsamer Datenstichtag fehlt") == 3
    assert "Marktbreite mindestens 0/1" not in result


def test_invalid_calendar_cutoff_is_not_accepted_as_common_data_date():
    report = _report().replace("2026-11-20", "2026-02-30")
    result = trading_radar_szenarien_ergänzen(report)
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert result.count("Status: NICHT ABLEITBAR") == 3
    assert result.count("Datenlücke: gemeinsamer Datenstichtag fehlt") == 3



def test_missing_breadth_with_positive_sectors_keeps_statuses_not_ableitbar():
    result = trading_radar_szenarien_ergänzen(
        _report(include_breadth=False, xle_3m="+2.10")
    )
    errors = validate_scenario_condition_consistency(result)

    assert not errors, errors
    assert result.count("Status: NICHT ABLEITBAR") == 2
    assert result.count("Status: NICHT ERFÜLLT") == 1
    assert result.count("Datenlücke: Marktbreite fehlt") == 3
