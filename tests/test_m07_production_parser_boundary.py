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
