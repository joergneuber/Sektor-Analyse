"""Isolated production-parser boundary tests.

The uploaded package omits several unrelated application modules. Minimal
import stubs keep these tests focused on the production provenance enricher.
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_import_stubs():
    stubs = {
        "trade_story_universum": {
            "write_trade_story_universe": lambda *args, **kwargs: None,
        },
        "trading_radar_neuauswertung": {
            "audit_market_first_content": lambda *args, **kwargs: None,
            "normalize_market_first_report_structure": lambda *args, **kwargs: None,
        },
        "trading_radar_market_first": {
            # Match the production module's imported public constant.
            "SECTOR_ETF_LABELS": {},
            **{
                name: (lambda *args, **kwargs: None)
                for name in (
                    "append_live_market_snapshot",
                    "downgrade_ohlcv_affected_candidates",
                    "normalize_event_section",
                    "report_explicitly_declares_no_technical_candidates",
                    "report_has_explicit_empty_candidate_groups",
                    "validate_ohlcv_audit",
                )
            },
        },
        "quellenwert_vertrag": {
            name: (lambda *args, **kwargs: None)
            for name in (
                "values_match_display",
                "find_exact_labeled_line",
                "extract_four_week_percent",
                "extract_metal_four_week_value",
            )
        },
    }
    for module_name, attributes in stubs.items():
        if module_name not in sys.modules:
            module = types.ModuleType(module_name)
            for name, value in attributes.items():
                setattr(module, name, value)
            sys.modules[module_name] = module


_STUB_MODULE_NAMES = (
    "trade_story_universum",
    "trading_radar_neuauswertung",
    "trading_radar_market_first",
    "quellenwert_vertrag",
)
_previous_modules = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}
_install_import_stubs()
try:
    spec = importlib.util.spec_from_file_location(
        "trading_radar_production_boundary_test_module",
        ROOT / "Trading_Radar_Alleinauswertung.py",
    )
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
finally:
    # The production module retains the imported dependencies it needs.
    # Restore sys.modules so these focused tests cannot poison the full suite.
    for _name, _previous in _previous_modules.items():
        if _previous is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _previous


def test_production_parser_rejects_fields_after_foreign_indicator_note_even_with_matching_macro():
    line = (
        "US 10Y Treasury 4.00% "
        "(Kommentar: fremder Indikator (Quelle: Yahoo Finance ^TNX)) "
        "| Quelle: FRED DGS10 | Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DGS10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "(Quelle: NICHT VERFÜGBAR;" in result
    assert "fremder Indikator" in result
    assert "Yahoo Finance ^TNX" in result
    assert "Quelle: FRED DGS10;" not in result


def test_production_parser_invalidates_fields_seen_before_foreign_note_and_does_not_resume():
    line = (
        "US 10Y Treasury 4.00% | Quelle: FRED DGS10 "
        "| (anderer Indikator: 10Y TIPS / DFII10) "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, "")
    assert "(Quelle: NICHT VERFÜGBAR;" in result
    assert "anderer Indikator" in result
    assert "10Y TIPS / DFII10" in result



def test_m05_deterministic_register_changes_replace_model_no_change_claim():
    report = (
        "1. Trading-Radar\n"
        "2. Thesen-Update\n"
        "- Keine neue oder aktualisierte These\n"
        "3. Ereignisradar\n"
    )
    evidence = [
        "THESENREGISTER-VERGLEICH: PASS",
        "THESEN_NEU: TH-NEW-01",
        "THESEN_AKTUALISIERT: TH-OLD-02",
        "THESEN_ARCHIVIERT: KEINE",
    ]
    result = production._trading_radar_thesenregister_nachweis_text(report, evidence)
    assert "Keine neue oder aktualisierte These" not in result
    assert "Registeränderungen laut Vergleich:" in result
    assert "Neue: TH-NEW-01" in result
    assert "Aktualisiert: TH-OLD-02" in result


def test_m05_no_change_claim_is_preserved_when_register_is_unchanged():
    report = (
        "1. Trading-Radar\n"
        "2. Thesen-Update\n"
        "- Keine neue oder aktualisierte These\n"
        "3. Ereignisradar\n"
    )
    evidence = [
        "THESENREGISTER-VERGLEICH: PASS",
        "THESEN_NEU: KEINE",
        "THESEN_AKTUALISIERT: KEINE",
        "THESEN_ARCHIVIERT: KEINE",
    ]
    result = production._trading_radar_thesenregister_nachweis_text(report, evidence)
    assert "Keine neue oder aktualisierte These" in result
    assert "Registeränderungen laut Vergleich:" not in result



def test_production_parser_keeps_macro_fallback_for_structured_local_us10y_annotation():
    line = "US 10Y Treasury 4.00% (Quelle: FRED DGS10)"
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DGS10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: FRED DGS10" in result
    assert "Abrufzeit: 2026-11-03T07:00:00+00:00" in result
    assert "Datenstichtag: 2026-11-02" in result
    assert "NICHT VERFÜGBAR" not in result



def test_production_parser_does_not_fill_structured_annotation_after_foreign_nested_note():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; (fremder Indikator: 10Y TIPS / DFII10))"
    )
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DGS10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "(Quelle: NICHT VERFÜGBAR;" in result
    assert "fremder Indikator: 10Y TIPS / DFII10" in result



def test_production_parser_preserves_unknown_local_annotation_text_verbatim():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; Hinweis: keep exactly  x  y)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, "")
    assert "Hinweis: keep exactly  x  y" in result


def test_production_parser_marks_conflicting_duplicate_source_fields():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; Quelle: FRED DFII10; "
        "Abrufzeit: 2026-11-03T07:00:00+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertage)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, "")
    assert "MEHRFACH WIDERSPRÜCHLICHE ANGABEN" in result



def test_m05_report_gate_accepts_deterministic_change_summary_without_loosening_gate():
    import hashlib
    import json
    from trading_radar_report_gate import _validate_m05_m10

    previous_hash = "a" * 64
    current_hash = "b" * 64
    comparison_payload = {
        "previous_sha256": previous_hash,
        "current_sha256": current_hash,
        "schema": "trading-radar-thesis-register-comparison-v1",
    }
    comparison_id = hashlib.sha256(
        json.dumps(comparison_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    evidence = [
        "THESENREGISTER-VERGLEICH: PASS",
        f"VORHER-SHA256: {previous_hash}",
        f"NACHHER-SHA256: {current_hash}",
        f"THESENREGISTER-VERGLEICH-ID: {comparison_id}",
        "VORHER_THESEN_COUNT: 2",
        "VORHER_AKTIVE_THESEN: 2",
        "NACHHER_THESEN_COUNT: 3",
        "NACHHER_AKTIVE_THESEN: 3",
        "THESEN_NEU: TH-NEW",
        "THESEN_AKTUALISIERT: TH-OLD",
        "THESEN_UNVERAENDERT: TH-STABLE",
        "THESEN_ARCHIVIERT: KEINE",
        "VORHER-THESIS-ID: TH-OLD",
        "VORHER-REGISTER-VERSION: 1",
        "VORHER-THESIS-ACTIVE: YES",
        "VORHER-THESIS-ID: TH-STABLE",
        "VORHER-REGISTER-VERSION: 1",
        "VORHER-THESIS-ACTIVE: YES",
        "THESIS_ID: TH-OLD",
        "REGISTER-VERSION: 2",
        "THESIS_ACTIVE: YES",
        "THESIS_ID: TH-STABLE",
        "REGISTER-VERSION: 1",
        "THESIS_ACTIVE: YES",
        "THESIS_ID: TH-NEW",
        "REGISTER-VERSION: 1",
        "THESIS_ACTIVE: YES",
    ]
    report = (
        "2. Thesen-Update\n"
        "Status: BESTÄTIGT\n"
        "Registeränderungen laut Vergleich: Neue: TH-NEW; Aktualisiert: TH-OLD.\n\n"
        "3. Ereignisradar\n"
        "Ereignisse werden separat bewertet.\n"
    )
    repaired = production._trading_radar_thesenregister_nachweis_text(report, evidence)
    errors = []
    _validate_m05_m10(repaired, errors)
    assert not any("Bericht behauptet 'keine Änderung'" in error for error in errors), errors


def test_production_parser_foreign_context_is_terminal_even_with_local_tail_and_macro_fallback():
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; (fremder Indikator: 10Y TIPS / DFII10)) "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DGS10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: NICHT VERFÜGBAR" in result
    assert "Abrufzeit: NICHT VERFÜGBAR" in result
    assert "Datenstichtag: NICHT VERFÜGBAR" in result
    assert "fremder Indikator: 10Y TIPS / DFII10" in result
    assert "FRED DGS10" not in result


def test_production_parser_preserves_entire_row_tail_after_nested_foreign_context():
    row_tail = (
        " | Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    line = (
        "US 10Y Treasury 4.00% "
        "(Quelle: FRED DGS10; (fremder Indikator: 10Y TIPS / DFII10))"
        + row_tail
    )
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DGS10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert row_tail in result
    assert "Quelle: NICHT VERFÜGBAR" in result
    assert "fremder Indikator: 10Y TIPS / DFII10" in result
    assert "FRED DGS10" not in result


def test_production_parser_rejects_fred_dfii10_macro_fallback():
    line = "US 10Y Treasury 4.00%"
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DFII10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: NICHT VERFÜGBAR" in result
    assert "FRED DFII10" not in result


def test_production_parser_rejects_mixed_dgs10_dfii10_macro_fallback():
    line = "US 10Y Treasury 4.00%"
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: FRED DGS10 / DFII10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: NICHT VERFÜGBAR" in result
    assert "DGS10 / DFII10" not in result


def test_production_parser_rejects_dfii10_macro_fallback_without_fred_provider_name():
    line = "US 10Y Treasury 4.00%"
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: Yahoo Finance DFII10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: NICHT VERFÜGBAR" in result
    assert "Yahoo Finance DFII10" not in result


def test_production_parser_rejects_mixed_dgs10_dfii10_without_fred_provider_name():
    line = "US 10Y Treasury 4.00%"
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: Yahoo Finance DGS10 / DFII10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: NICHT VERFÜGBAR" in result
    assert "DGS10 / DFII10" not in result


def test_production_parser_accepts_yahoo_dgs10_without_fred_provider_name():
    line = "US 10Y Treasury 4.00%"
    macro = (
        "US 10Y Treasury: 4.00% | Quelle: Yahoo Finance DGS10 "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line, macro)
    assert "Quelle: Yahoo Finance DGS10" in result
    assert "NICHT VERFÜGBAR" not in result


def test_m07_uses_explicit_value_bound_conflict_provenance_when_macro_cache_is_ambiguous():
    report = (
        "1. Die US-Leitzinsen bleiben stabil (US 10Y Treasury bei 4,88% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)).\n"
        "- GETRENNT DOKUMENTIERTE US-10Y-BEOBACHTUNGEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.8800% (Makro-Provenienz: Quelle=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations); "
        "Abrufzeit=2026-11-12T07:11:41+00:00; Datenstichtag=2026-11-09; "
        "Einheit=%; Verzögerung=6 Kalendertage) | Live-Wert: 5.1540% "
        "(Quelle: Yahoo Finance (^TNX); Abrufzeit: 2026-11-15T12:47:41+00:00; "
        "Datenstichtag: 2026-11-13T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs)."
    )
    macro = (
        "US 10Y Treasury: 4.8800 | Datenstand=2026-11-09 | STATUS=REAL_CACHED "
        "| SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) "
        "| RETRIEVED_AT=2026-11-12T07:11:41+00:00 | DELAY_DAYS=6 | UNIT=%\n"
        "US 10Y Treasury: 4.8800 | Datenstand=2026-11-08 | STATUS=REAL_CACHED "
        "| SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) "
        "| RETRIEVED_AT=2026-11-11T07:11:41+00:00 | DELAY_DAYS=7 | UNIT=%"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro)
    first_line = result.splitlines()[0]
    assert "Quelle: FRED DGS10" in first_line
    assert "Abrufzeit: 2026-11-12T07:11:41+00:00" in first_line
    assert "Datenstichtag: 2026-11-09" in first_line
    assert "Einheit: %" in first_line
    assert "Verzögerung: 6 Kalendertage" in first_line


def test_m07_keeps_value_unresolved_when_explicit_conflict_records_disagree():
    report = (
        "US 10Y Treasury bei 4,88%\n"
        "- GETRENNT DOKUMENTIERTE US-10Y-BEOBACHTUNGEN | Instrument: US-10Y Treasury | Makro-Wert: 4.8800% "
        "(Makro-Provenienz: Quelle=FRED DGS10; Abrufzeit=2026-11-12T07:11:41+00:00; "
        "Datenstichtag=2026-11-09; Einheit=%; Verzögerung=6 Kalendertage)\n"
        "- GETRENNT DOKUMENTIERTE US-10Y-BEOBACHTUNGEN | Instrument: US-10Y Treasury | Makro-Wert: 4.8800% "
        "(Makro-Provenienz: Quelle=FRED DGS10; Abrufzeit=2026-11-11T07:11:41+00:00; "
        "Datenstichtag=2026-11-08; Einheit=%; Verzögerung=7 Kalendertage)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, "")
    assert "Quelle: NICHT VERFÜGBAR" in result.splitlines()[0]
    assert "Abrufzeit: NICHT VERFÜGBAR" in result.splitlines()[0]


def test_production_parser_accepts_multiple_separately_sourced_us10y_observations_on_one_rates_line():
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance

    line = (
        "Zinsen/Renditen: US-10J-Rendite (^TNX): 5.15% "
        "(Quelle: Yahoo Finance (^TNX); Abrufzeit: 2026-11-15T12:47:41+00:00; "
        "Datenstichtag: 2026-11-13T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs) | "
        "US 10Y Treasury: 4.8800% "
        "(Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations); "
        "Abrufzeit: 2026-11-12T07:11:41+00:00; Datenstichtag: 2026-11-09; "
        "Einheit: %; Verzögerung: 6 Kalendertage)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(line)
    assert "MEHRERE_WERTE_NICHT_EINDEUTIG_ZUORDENBAR" not in result
    assert _treasury_observation_has_complete_provenance(result)


def test_report_gate_accepts_nested_narrative_parentheses_around_sourced_us10y():
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance

    line = (
        "1. Die US-Leitzinsen bleiben stabil (US 10Y Treasury bei 4,88% "
        "(Quelle: FRED DGS10; Abrufzeit: 2026-11-12T07:11:41+00:00; "
        "Datenstichtag: 2026-11-09; Einheit: %; Verzögerung: 6 Kalendertage))."
    )
    assert _treasury_observation_has_complete_provenance(line)


def test_thesisregister_prompt_context_is_compact_hash_bound_and_preserves_terminal_ids(tmp_path):
    import hashlib
    import json

    register_path = tmp_path / ".trading_radar_thesen.json"
    raw = json.dumps({
        "schema_version": 1,
        "theses": [
            {
                "thesis_id": "active_macro_01", "version": 3, "status": "BESTÄTIGT",
                "thesis": "Aktive Makrothese", "horizon": "2-4 Wochen", "alternative": "Keine",
                "active": True, "replaced_by": None,
                "evidence": ["FRED DGS10"], "counter_evidence": [],
                "confidence": "mittel", "confirm_triggers": [], "invalidate_triggers": [],
                "event_dependencies": [], "asset_classes": ["Gold"], "candidates": [],
            },
            {
                "thesis_id": "closed_macro_02", "version": 2, "status": "ARCHIVIERT",
                "thesis": "Archivierte These", "horizon": "alt", "alternative": "alt",
                "active": False, "replaced_by": None, "evidence": [], "counter_evidence": [],
                "confidence": "niedrig", "confirm_triggers": [], "invalidate_triggers": [],
                "event_dependencies": [], "asset_classes": [], "candidates": [],
            },
        ],
    }, ensure_ascii=False).encode("utf-8")
    register_path.write_bytes(raw)

    context = json.loads(
        production._trading_radar_thesenregister_prompt_context(str(register_path))
    )
    assert context["register_status"] == "VERIFIZIERT"
    assert context["register_sha256"] == hashlib.sha256(raw).hexdigest()
    assert context["active_theses"][0]["thesis_id"] == "active_macro_01"
    assert context["active_theses"][0]["version"] == 3
    assert context["terminal_ids"] == [{
        "thesis_id": "closed_macro_02", "version": 2, "status": "ARCHIVIERT", "replaced_by": None
    }]


def test_failed_thesisregister_update_reports_real_unchanged_hashes_without_pass(tmp_path):
    import hashlib

    register_path = tmp_path / ".trading_radar_thesen.json"
    raw = b'{"schema_version":1,"theses":[]}'
    register_path.write_bytes(raw)
    evidence = production._trading_radar_thesenregister_unveraendert_evidenz(
        str(register_path), "new identity has invalid version"
    )
    digest = hashlib.sha256(raw).hexdigest()
    assert "THESENREGISTER-VERGLEICH: NOT_UPDATED" in evidence
    assert f"VORHER-SHA256: {digest}" in evidence
    assert f"NACHHER-SHA256: {digest}" in evidence
    assert not any("THESENREGISTER-VERGLEICH: PASS" in line for line in evidence)


def test_invalid_thesis_register_is_not_exposed_as_trusted_prompt_context(tmp_path):
    import json

    register_path = tmp_path / ".trading_radar_thesen.json"
    register_path.write_text(json.dumps({
        "schema_version": 1,
        "theses": [{
            "thesis_id": "invalid_archive", "version": 1,
            "status": "ARCHIVIERT", "active": True,
            "thesis": "Invalid active archive", "horizon": "kurz",
            "alternative": "Keine", "replaced_by": None,
            "evidence": [], "counter_evidence": [], "confidence": "niedrig",
            "confirm_triggers": [], "invalidate_triggers": [],
            "event_dependencies": [], "asset_classes": [], "candidates": [],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    context = json.loads(
        production._trading_radar_thesenregister_prompt_context(str(register_path))
    )
    assert context["register_status"] == "UNGÜLTIG"
    assert context["active_theses"] == []
    assert context["terminal_ids"] == []
    assert context["validation_errors"]
