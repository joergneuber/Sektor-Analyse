from trading_radar_report_gate import (
    _treasury_observation_has_complete_provenance,
    _us10y_provenance_fields,
    extract_us10y_observations,
    _validate_m05_m10,
)


def _complete_us10y_line(source="FRED DGS10"):
    return (
        "US 10Y Treasury 4.00% "
        f"(Quelle: {source}; Abrufzeit: 2026-11-03T07:00:00+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertage)"
    )


def test_m07_nested_foreign_provenance_cannot_rescue_us10y():
    line = (
        "US 10Y Treasury 4.00% "
        "(anderer Indikator (Quelle: Yahoo Finance ^TNX; "
        "Abrufzeit: 2026-11-03T07:01:00+00:00; Datenstichtag: 2026-11-02; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs))"
    )
    assert _us10y_provenance_fields(line)[0] == {}
    assert not _treasury_observation_has_complete_provenance(line)
    # The foreign-indicator note remains a separate, non-US10Y string.
    assert "Quelle: Yahoo Finance ^TNX" in line


def test_m07_conflicting_duplicate_source_fields_fail_closed():
    line = _complete_us10y_line().replace(
        "Abrufzeit:", "Quelle: FRED DFII10; Abrufzeit:"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert "source" in conflicts
    assert not _treasury_observation_has_complete_provenance(line)


def test_m07_identical_duplicate_fields_are_not_misclassified_as_conflicts():
    line = _complete_us10y_line().replace(
        "Abrufzeit:", "Quelle: FRED DGS10; Abrufzeit:"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert not conflicts
    assert fields["source"] == "FRED DGS10"
    assert _treasury_observation_has_complete_provenance(line)


def test_m07_fred_requires_nominal_dgs10_not_real_yield_dfii10():
    assert _treasury_observation_has_complete_provenance(_complete_us10y_line("FRED DGS10"))
    assert not _treasury_observation_has_complete_provenance(_complete_us10y_line("FRED DFII10"))
    assert not _treasury_observation_has_complete_provenance(
        _complete_us10y_line("FRED DGS10 / DFII10")
    )


def test_m07_foreign_indicator_parenthetical_context_closes_gate_provenance_scope():
    line = (
        "US 10Y Treasury 4.00% "
        "(Kommentar: fremder Indikator (Quelle: Yahoo Finance ^TNX)) "
        "| Quelle: FRED DGS10 | Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert fields == {}
    assert conflicts
    assert not _treasury_observation_has_complete_provenance(line)
    assert _m07_gate_errors(line)


def test_m07_gate_does_not_resume_after_foreign_note_inside_provenance_suffix():
    line = (
        "US 10Y Treasury 4.00% | Quelle: FRED DGS10 "
        "| (anderer Indikator: 10Y TIPS / DFII10) "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert fields == {}
    assert "FOREIGN_CONTEXT" in conflicts
    assert not _treasury_observation_has_complete_provenance(line)
    assert _m07_gate_errors(line)


def test_m07_gate_invalidates_local_fields_when_foreign_note_follows_inside_annotation():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; (fremder Indikator: 10Y TIPS / DFII10); "
        "Abrufzeit: 2026-11-03T07:00:00+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertage)"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert fields == {}
    assert "FOREIGN_CONTEXT" in conflicts
    assert not _treasury_observation_has_complete_provenance(line)
    assert _m07_gate_errors(line)


def test_m07_malformed_parenthesis_nesting_is_rejected():
    line = _complete_us10y_line().replace("DGS10;", "DGS10 (")
    assert not _treasury_observation_has_complete_provenance(line)


def test_m07_other_indicators_are_not_reclassified_as_us10y():
    line = "10Y TIPS 2.00% (Quelle: FRED DFII10)"
    assert extract_us10y_observations(line) == []



def _m07_gate_errors(line):
    errors = []
    _validate_m05_m10("1. Trading-Radar\n" + line + "\n", errors)
    return [error for error in errors if "Jede abweichende 10Y-Treasury-Beobachtung" in error]


def test_m07_report_gate_rejects_dfii10_on_nominal_us10y():
    assert not _m07_gate_errors(_complete_us10y_line("FRED DGS10"))
    assert _m07_gate_errors(_complete_us10y_line("FRED DFII10"))


def test_m07_report_gate_does_not_borrow_nested_foreign_indicator_provenance():
    line = (
        "US 10Y Treasury 4.00% "
        "(anderer Indikator (Quelle: Yahoo Finance ^TNX; "
        "Abrufzeit: 2026-11-03T07:01:00+00:00; Datenstichtag: 2026-11-02; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs))"
    )
    assert _m07_gate_errors(line)


def test_m07_report_gate_does_not_treat_tips_or_foreign_indicators_as_nominal_us10y():
    assert extract_us10y_observations("10Y TIPS 2.00%") == []
    assert extract_us10y_observations("UK 10Y Treasury 4.00%") == []
    assert not _m07_gate_errors("10Y TIPS 2.00% (Quelle: FRED DFII10)")



def test_m07_gate_accepts_complete_local_provenance_before_ordinary_trailing_prose():
    line = (
        "US 10Y Treasury: 4.20% "
        "(Quelle: Yahoo Finance ^TNX; "
        "Abrufzeitpunkt: 2026-11-03T10:00:00+01:00; "
        "Datenstand: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertag) "
        "weiterer Kommentar"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert not conflicts
    assert fields["source"] == "Yahoo Finance ^TNX"
    assert _treasury_observation_has_complete_provenance(line)
    assert not _m07_gate_errors(line)


def test_m07_gate_does_not_borrow_row_tail_fields_after_ordinary_prose():
    line = (
        "US 10Y Treasury: 4.20% "
        "(Quelle: Yahoo Finance ^TNX; Abrufzeitpunkt: 2026-11-03T10:00:00+01:00) "
        "Kommentar | Datenstand: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertag"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert fields == {"source": "Yahoo Finance ^TNX", "retrieved": "2026-11-03T10:00:00+01:00"}
    assert not conflicts
    assert not _treasury_observation_has_complete_provenance(line)
    assert _m07_gate_errors(line)


def test_m07_gate_still_rejects_opaque_parenthetical_after_complete_local_provenance():
    line = (
        "US 10Y Treasury: 4.20% "
        "(Quelle: Yahoo Finance ^TNX; Abrufzeitpunkt: 2026-11-03T10:00:00+01:00; "
        "Datenstand: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertag) "
        "(fremder Indikator: 10Y TIPS / DFII10)"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert "FOREIGN_CONTEXT" in conflicts
    assert not _treasury_observation_has_complete_provenance(line)
    assert _m07_gate_errors(line)


def test_m07_gate_rejects_top_level_fields_after_foreign_context():
    line = (
        "US 10Y Treasury 4.00% | anderer Indikator: 10Y TIPS "
        "| Quelle: Yahoo Finance ^TNX "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert not fields
    assert _m07_gate_errors(line)



def _conflict_line(macro_source="FRED DGS10", live_source="Yahoo Finance ^TNX"):
    return (
        "QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.00% Makro-Provenienz: Quelle=" + macro_source +
        "; Abrufzeit=2026-11-03T07:00:00+00:00; Datenstichtag=2026-11-02; "
        "Einheit=%; Verzögerung=1 Kalendertage | "
        "Live-Wert: 4.10% (Quelle=" + live_source +
        "; Abrufzeit=2026-11-03T07:01:00+00:00; Datenstichtag=2026-11-03; "
        "Einheit=%; Verzögerung=Tages-Schlusskurs)"
    )


def test_m07_conflict_roles_are_parsed_and_validated_independently():
    from trading_radar_report_gate import _us10y_conflict_observation_records
    records = _us10y_conflict_observation_records(_conflict_line())
    assert [item[0] for item in records] == ["Makro", "Live"]
    assert all(_treasury_observation_has_complete_provenance(item[2]) for item in records)


def test_m07_conflict_role_cannot_borrow_nested_foreign_provenance():
    from trading_radar_report_gate import _us10y_conflict_observation_records
    line = _conflict_line().replace(
        "Makro-Provenienz: Quelle=FRED DGS10; ",
        "Makro-Provenienz: anderer Indikator (Quelle=Yahoo Finance ^TNX; "
        "Abrufzeit=2026-11-03T07:00:00+00:00; Datenstichtag=2026-11-02; "
        "Einheit=%; Verzögerung=1 Kalendertage); ",
    )
    records = _us10y_conflict_observation_records(line)
    assert len(records) == 2
    assert not _treasury_observation_has_complete_provenance(records[0][2])
    assert _treasury_observation_has_complete_provenance(records[1][2])


def test_m07_conflict_role_rejects_conflicting_duplicate_source_fields():
    from trading_radar_report_gate import _us10y_conflict_observation_records
    line = _conflict_line().replace(
        "Quelle=FRED DGS10; ",
        "Quelle=FRED DGS10; Quelle=FRED DFII10; ",
    )
    records = _us10y_conflict_observation_records(line)
    assert len(records) == 2
    assert not _treasury_observation_has_complete_provenance(records[0][2])


def test_m07_gate_rejects_foreign_nested_annotation_even_if_tail_fields_follow():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; (fremder Indikator: 10Y TIPS / DFII10)) "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert fields == {}
    assert "FOREIGN_CONTEXT" in conflicts
    assert _m07_gate_errors(line)


def test_m07_gate_rejects_fred_dfii10_with_complete_metadata():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DFII10; Abrufzeit: 2026-11-03T07:00:00+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertage)"
    )
    fields, conflicts = _us10y_provenance_fields(line)
    assert not conflicts
    assert fields["source"] == "FRED DFII10"
    assert not _treasury_observation_has_complete_provenance(line)
    assert _m07_gate_errors(line)


def test_m07_gate_rejects_dfii10_without_fred_provider_name():
    line = _complete_us10y_line("Yahoo Finance DFII10")
    assert _m07_gate_errors(line)


def test_m07_gate_rejects_mixed_dgs10_dfii10_without_fred_provider_name():
    line = _complete_us10y_line("Yahoo Finance DGS10 / DFII10")
    assert _m07_gate_errors(line)


def test_m07_gate_accepts_yahoo_dgs10_without_fred_provider_name():
    line = _complete_us10y_line("Yahoo Finance DGS10")
    assert not _m07_gate_errors(line)


def test_m07_accepts_two_distinct_us10y_values_with_separate_complete_provenance():
    line = (
        "Zinsen/Renditen: US-10J-Rendite (^TNX): 4.85% "
        "(Quelle: Yahoo Finance (^TNX); Abrufzeit: 2026-11-06T17:18:23+00:00; "
        "Datenstichtag: 2026-11-06T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs) | Quelldatei: Makro_Briefing(2026-11-06).txt; "
        "US 10Y Treasury bei 4.9000% "
        "(Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations); "
        "Abrufzeit: 2026-11-06T04:31:01+00:00; Datenstichtag: 2026-11-04; "
        "Einheit: %; Verzögerung: 2 Kalendertage)"
    )
    assert not _m07_gate_errors(line)


def test_m07_does_not_borrow_first_us10y_provenance_for_second_value_on_same_line():
    line = (
        "Zinsen/Renditen: US-10J-Rendite (^TNX): 4.85% "
        "(Quelle: Yahoo Finance (^TNX); Abrufzeit: 2026-11-06T17:18:23+00:00; "
        "Datenstichtag: 2026-11-06T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs); US 10Y Treasury bei 4.9000%"
    )
    assert _m07_gate_errors(line)
