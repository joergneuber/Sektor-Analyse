from pathlib import Path

from trading_radar_market_first import normalize_event_section
from trading_radar_neuauswertung import (
    audit_market_first_content,
    ensure_structured_thesis_block,
    extract_thesis_json as extract_strict_thesis_json,
    validate_radar_report_strict,
    validate_thesis_ids_match_report,
)
from trading_radar_report_gate import validate_report
from trading_radar_thesen import (
    extract_thesis_payload,
    synchronize_report_versions,
    update_register,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trading_radar_run430071_original.txt"


def _report():
    return FIXTURE.read_text(encoding="utf-8")


def test_run430071_hyphenated_thesis_id_is_matched_and_market_first_fields_stay_in_one_record():
    report = _report()
    payload = extract_strict_thesis_json(report)
    assert payload["theses"][0]["thesis_id"] == "THESIS-01"
    assert validate_thesis_ids_match_report(report, payload) == []

    findings = audit_market_first_content(report)
    assert not any("These 1: Pflichtfeld" in finding for finding in findings)
    normalized, changed = ensure_structured_thesis_block(report)
    assert changed is True
    assert "```TRADING_RADAR_THESEN_JSON" in normalized
    assert not any("Thesen-ID-Abgleich" in finding for finding in validate_radar_report_strict(normalized))


def test_run430071_generic_json_fence_can_be_persisted_and_version_synchronized(tmp_path):
    report = _report()
    payload = extract_thesis_payload(report)
    assert payload is not None
    assert payload["theses"][0]["thesis_id"] == "THESIS-01"

    register_path = tmp_path / "register.json"
    first = update_register(report, str(register_path))
    assert first["updated"] is True
    assert first["comparison_status"] == "BASELINE_MISSING"

    # Once a real baseline exists, the exact same generic-fence report can
    # produce the deterministic PASS + before/after SHA256 evidence required
    # by M-05; the first baseline creation remains deliberately non-PASS.
    second = update_register(report, str(register_path))
    assert second["updated"] is True
    assert second["comparison_status"] == "PASS"
    assert len(second["previous_sha256"]) == 64
    assert len(second["current_sha256"]) == 64
    assert len(second["comparison_sha256"]) == 64

    synchronized = synchronize_report_versions(report, second)
    # The first write's persisted version is authoritative even when the model
    # supplied a legacy generic JSON fence.
    assert "```TRADING_RADAR_THESEN_JSON" in synchronized
    assert extract_thesis_payload(synchronized)["theses"][0]["version"] == 1


def test_run430071_event_placeholders_and_cache_sources_are_not_verified():
    report, findings = normalize_event_section(_report())
    assert "Ereignis ohne Datum" in findings
    assert "Ereignis ohne verifizierte Quelle" in findings
    section = report.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
    assert "Quellenprüfung: NICHT VERIFIZIERT" in section
    # The affected-assets continuation field must not be split into a new event.
    assert section.count("Quellenprüfung: NICHT VERIFIZIERT") == 2


def test_run430071_m07_placeholder_narrative_repetitions_do_not_override_complete_sources():
    errors = validate_report(_report())
    assert not any("Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors)


def test_run430071_unavailable_direction_is_inferred_only_from_consistent_geometry():
    errors = validate_report(_report())
    assert not any("unplausible Stop-/TP1-Lage" in error for error in errors)


def test_m07_rejects_second_formal_observation_with_same_value_and_placeholder_provenance():
    report = _report()
    anchor = "Zinsen/Renditen: US-10J-Rendite (^TNX): 4.98%"
    assert anchor in report
    additional_observation = (
        "\nUS 10Y Treasury: 4.9000% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)"
    )
    report = report.replace(anchor, anchor + additional_observation, 1)
    errors = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors), errors


def test_m09_explicit_short_is_not_erased_by_unavailability_phrase_in_same_field():
    report = _report()
    original = "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)"
    assert original in report
    report = report.replace(
        original,
        "Richtung: SHORT (keine Richtungsangabe in der Scannerquelle)",
        1,
    )
    errors = validate_report(report)
    assert any("M-09:" in error and "Stop-/TP1" in error for error in errors), errors


def test_event_normalizer_downgrades_existing_live_status_when_date_or_source_is_unverified():
    report = """3. Ereignisradar
- Ereignis: CPI-Veröffentlichung
  - Datum: NICHT VERFÜGBAR
  - Quelle: FRED CACHE
  - **Quellenprüfung:** LIVE_VERIFIZIERT — alter, widersprüchlicher Status

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    event_section = normalized.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
    assert "Ereignis ohne Datum" in findings
    assert "Ereignis ohne verifizierte Quelle" in findings
    assert "**Quellenprüfung:** NICHT VERIFIZIERT" in event_section
    assert "LIVE_VERIFIZIERT" not in event_section
    assert event_section.count("Quellenprüfung:") == 1



def test_m05_generic_json_is_rejected_without_complete_section_boundaries():
    section1_only = """1. Trading-Radar
```json
{"theses": []}
```
3. Ereignisradar
"""
    missing_section3 = """2. Thesen-Update
```json
{"theses": []}
```
4. Assetklassenvergleich
"""
    assert extract_thesis_payload(section1_only) is None
    assert extract_thesis_payload(missing_section3) is None


def test_market_first_downgrades_all_duplicate_source_status_fields():
    report = """3. Ereignisradar
- Ereignis: CPI-Veröffentlichung
  - Datum: NICHT VERFÜGBAR
  - Quelle: FRED CACHE
  - Quellenprüfung: LIVE_VERIFIZIERT
  - Quellenprüfung: LIVE_VERIFIZIERT

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    event_section = normalized.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
    assert "Ereignis ohne Datum" in findings
    assert "Ereignis ohne verifizierte Quelle" in findings
    assert "LIVE_VERIFIZIERT" not in event_section
    assert event_section.count("Quellenprüfung: NICHT VERIFIZIERT") == 2


def test_m07_later_independent_placeholder_observation_cannot_borrow_earlier_narrative_context():
    report = _report()
    original = (
        "- Zinsen / Dollar: Zinsen verbleiben auf erhöhtem Niveau (US 10Y bei 4,90 % "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR), 2Y bei 4,58 %)."
    )
    assert original in report
    independent = (
        " | separate Beobachtung: US 10Y at 4.90% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)"
    )
    report = report.replace(original, original + independent, 1)
    errors = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors), errors


# Focused positive/negative coverage for the three follow-up corrections.

def test_market_first_missing_status_uses_a_real_line_break_not_literal_backslash_n():
    report = """3. Ereignisradar
- Ereignis: CPI-Veröffentlichung
  - Datum: NICHT VERFÜGBAR
  - Quelle: FRED CACHE

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    event_section = normalized.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
    assert "Ereignis ohne Datum" in findings
    assert "Ereignis ohne verifizierte Quelle" in findings
    assert "\\n- Quellenprüfung: NICHT VERIFIZIERT" not in event_section
    assert "\n- Quellenprüfung: NICHT VERIFIZIERT" in event_section


def test_market_first_complete_event_without_status_is_not_downgraded():
    report = """3. Ereignisradar
- Ereignis: CPI-Veröffentlichung
  - Datum: 2026-12-01 13:30 UTC
  - Quelle: FRED, https://fred.stlouisfed.org/
  - Quellenprüfung: LIVE_VERIFIZIERT

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    event_section = normalized.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
    assert "Ereignis ohne Datum" not in findings
    assert "Ereignis ohne verifizierte Quelle" not in findings
    assert "Quellenprüfung: LIVE_VERIFIZIERT" in event_section


def test_m07_second_observation_cannot_borrow_placeholder_fields_from_prior_narrative():
    report = _report()
    original = (
        "- Zinsen / Dollar: Zinsen verbleiben auf erhöhtem Niveau (US 10Y bei 4,90 % "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR), 2Y bei 4,58 %)."
    )
    assert original in report
    # The second observation has narrative wording before its label, but no
    # provenance of its own. The first observation's placeholders must not
    # qualify this second record for the narrative exception.
    later = " | Zinsen verbleiben weiterhin erhöht; separate Beobachtung: US 10Y at 4.90%"
    report = report.replace(original, original + later, 1)
    errors = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors), errors


def test_m07_single_narrative_reference_keeps_its_own_placeholder_exception():
    report = _report()
    errors = validate_report(report)
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors)


def test_m09_short_with_explanation_is_rejected_when_levels_have_long_geometry():
    report = _report()
    report = report.replace(
        "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)",
        "Richtung: SHORT (keine Richtungsangabe in der Scannerquelle)",
        1,
    )
    errors = validate_report(report)
    assert any("M-09:" in error and "unplausible" in error for error in errors), errors


def test_m09_short_with_explanation_is_accepted_with_consistent_short_geometry():
    report = _report()
    report = report.replace(
        "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)",
        "Richtung: SHORT (keine Richtungsangabe in der Scannerquelle)",
        1,
    )
    report = report.replace("  - Stop: 71.39", "  - Stop: 89.93", 1)
    report = report.replace("  - TP1: 89.93", "  - TP1: 71.39", 1)
    errors = validate_report(report)
    assert not any("M-09:" in error for error in errors), errors


def test_market_first_rejects_impossible_calendar_date():
    report = _report().replace(
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)",
        "Datum: 2026-99-99",
        1,
    )
    _, findings = normalize_event_section(report)
    assert "Ereignis ohne Datum" in findings


def test_market_first_accepts_valid_iso_and_german_calendar_dates():
    report = _report().replace(
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)",
        "Datum: 2026-11-12",
    )
    _, findings = normalize_event_section(report)
    assert "Ereignis ohne Datum" not in findings

    german_report = _report().replace(
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)",
        "Datum: 12.11.2026",
    )
    _, german_findings = normalize_event_section(german_report)
    assert "Ereignis ohne Datum" not in german_findings


def test_market_first_rejects_non_leap_day_and_accepts_valid_leap_day():
    invalid_report = _report().replace(
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)",
        "Datum: 2026-02-29",
        1,
    )
    _, invalid_findings = normalize_event_section(invalid_report)
    assert "Ereignis ohne Datum" in invalid_findings

    valid_report = _report().replace(
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)",
        "Datum: 2028-02-29",
    )
    _, valid_findings = normalize_event_section(valid_report)
    assert "Ereignis ohne Datum" not in valid_findings


def test_m09_rejects_direction_contradiction_inside_parenthetical_note():
    report = _report().replace(
        "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)",
        "Richtung: SHORT (LONG)",
        1,
    )
    errors = validate_report(report)
    assert any("M-09:" in error for error in errors), errors


def test_version_synchronization_supports_mixed_heading_and_id_only_thesis_records():
    report = """2. Thesen-Update
These 1: Erste These
Thesen-ID: THESIS-01
Version: 1

Thesen-ID: THESIS-02
Version: 1
3. Ereignisradar
"""
    synchronized = synchronize_report_versions(
        report,
        {"thesis_versions": [
            {"thesis_id": "THESIS-01", "version": 3},
            {"thesis_id": "THESIS-02", "version": 4},
        ]},
    )
    assert "Thesen-ID: THESIS-01\nVersion: 3" in synchronized
    assert "Thesen-ID: THESIS-02\nVersion: 4" in synchronized


def test_version_synchronization_keeps_heading_record_id_as_its_own_identity():
    report = """2. Thesen-Update
These 1: Erste These
Thesen-ID: THESIS-01
Version: 1
These 2: Zweite These
Thesen-ID: THESIS-02
Version: 1
3. Ereignisradar
"""
    synchronized = synchronize_report_versions(
        report,
        {"thesis_versions": [
            {"thesis_id": "THESIS-01", "version": 2},
            {"thesis_id": "THESIS-02", "version": 5},
        ]},
    )
    assert "THESEN-ID: THESIS-01" not in synchronized
    assert "Thesen-ID: THESIS-01\nVersion: 2" in synchronized
    assert "Thesen-ID: THESIS-02\nVersion: 5" in synchronized


def test_market_first_rejects_impossible_german_calendar_date():
    report = _report().replace(
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)",
        "Datum: 31.04.2026",
        1,
    )
    _, findings = normalize_event_section(report)
    assert "Ereignis ohne Datum" in findings


def test_m09_rejects_opposite_direction_note_symmetrically():
    report = _report().replace(
        "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)",
        "Richtung: LONG (SHORT)",
        1,
    )
    errors = validate_report(report)
    assert any("M-09:" in error for error in errors), errors


def test_m07_separate_observation_is_not_exempted_by_nearby_narrative_context():
    report = _report()
    original = (
        "- Zinsen / Dollar: Zinsen verbleiben auf erhöhtem Niveau (US 10Y bei 4,90 % "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR), 2Y bei 4,58 %)."
    )
    assert original in report
    separate = (
        " | Zinsen verbleiben weiterhin erhöht; separate Beobachtung: "
        "US 10Y at 4.90% (Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)"
    )
    report = report.replace(original, original + separate, 1)
    errors = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors), errors


def test_m09_unavailable_marker_does_not_hide_explicit_long_direction_in_note():
    report = _report()
    original = "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)"
    assert original in report
    report = report.replace(original, "Richtung: NICHT VERFÜGBAR (LONG)", 1)
    # Make the levels internally SHORT-consistent: LONG in the note must still
    # be rejected rather than silently discarded and inferred away.
    report = report.replace("  - Stop: 71.39", "  - Stop: 89.93", 1)
    report = report.replace("  - TP1: 89.93", "  - TP1: 71.39", 1)
    errors = validate_report(report)
    assert any("M-09:" in error for error in errors), errors


def test_version_synchronization_supports_german_plural_thesen_heading():
    report = """2. Thesen-Update
Thesen 1: Erste These
Version: 1
Thesen-ID: THESIS-01
3. Ereignisradar
"""
    synchronized = synchronize_report_versions(
        report,
        {"thesis_versions": [{"thesis_id": "THESIS-01", "version": 7}]},
    )
    assert "Thesen 1: Erste These\nVersion: 7" in synchronized


def test_m09_unavailable_marker_does_not_hide_explicit_short_direction_in_note():
    report = _report()
    original = "Richtung: NICHT VERFÜGBAR (keine Richtungsangabe in der Scannerquelle)"
    assert original in report
    report = report.replace(original, "Richtung: NICHT VERFÜGBAR (SHORT)", 1)
    errors = validate_report(report)
    assert any("M-09:" in error for error in errors), errors
