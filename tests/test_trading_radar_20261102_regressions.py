import ast
import json
import re
from pathlib import Path

import Trading_Radar_Alleinauswertung as production
from trading_radar_market_first import normalize_event_section
from trading_radar_neuauswertung import audit_market_first_content, ensure_structured_thesis_block
from trading_radar_report_gate import validate_report

ROOT = Path(__file__).resolve().parents[1]


def test_name_normalizer_preserves_words_dates_markers_and_fenced_json(tmp_path):
    names = tmp_path / "names.csv"
    names.write_text(
        "Ticker;Name\nALLE;Allegion plc\nSCHW;The Charles Schwab Corporation\n"
        "MSCI;MSCI Inc.\n",
        encoding="utf-8",
    )
    code = (
        "```TRADING_RADAR_THESEN_JSON\n"
        '{"status":"NEU","event":"ISM Manufacturing PMI (2026-11-02)",'
        '"label":"LIVE-SYSTEM","example":"Fake Corp (FAKE)"}\n```'
    )
    report = (
        "1. Trading-Radar\n"
        "Der Markt bleibt schwächer; alle Anleger warten.\n"
        "MSCI-World-Performance bleibt die Benchmark. LIVE-SYSTEM ist die Vergleichsbasis.\n"
        "Quelle: ForexFactory (LIVE) / Official ISM.\n"
        "ISM Manufacturing PMI (2026-11-02) wird beobachtet.\n"
        "Fake ticker: FAKE\n"
        + code + "\n"
    )
    result = production._trading_radar_name_ticker_normalisieren(
        report, {"names": str(names)}
    )
    assert "schwächer" in result
    assert "alle Anleger" in result
    assert "MSCI-World-Performance" in result
    assert "LIVE-SYSTEM" in result
    assert "ForexFactory (LIVE)" in result
    assert "ISM Manufacturing PMI (2026-11-02)" in result
    assert code in result
    assert "Name nicht aufgelöst (FAKE)" in result
    assert "Fake Corp (FAKE)" not in result.replace(code, "")
    assert "Allegion plc (ALLE)" not in result


def test_treasury_provenance_never_mutates_json_code_fences():
    code = '```json\n{"note":"US 10Y Treasury bei 4.98%"}\n```'
    source = (
        "US 10Y Treasury: 4.98 | SOURCE=FRED | DATA_DATE=2026-10-30 | "
        "RETRIEVED_AT=2026-11-02T04:00:00Z | UNIT=% | DELAY_DAYS=3"
    )
    report = code + "\nUS 10Y Treasury bei 4.98%\n"
    result = production._trading_radar_treasury_provenienz_ergänzen(report, source, {})
    assert code in result
    assert "Quelle: FRED" in result


def test_event_parser_recognizes_inline_date_source_and_continuation_bullets():
    report = """3. Ereignisradar
- Ereignis-Uhrzeit: HEUTE (2026-11-02) | Zeitzone: NICHT VERFÜGBAR | Kennzahl: ISM Manufacturing PMI | Zeitraum: September 2026 | Letzter Wert: 54,5 | Konsens: NICHT VERFÜGBAR | Quelle: ForexFactory (LIVE) / Official ISM
  - Betroffene These/Assetklasse/konkrete offene Positionen: Sektorrotation / Aktien
- Szenarien unter/entsprechend/über Konsens: Über Konsens stützt zyklische Werte; unter Konsens belastet sie.
- Reaktionskanäle: Zyklische Aktien und Rohstofffutures.
- Was die These bestätigen/widerlegen würde: Bestätigung oberhalb 50; Widerlegung unter 50.

4. Assetklassenvergleich
Relative Stärke und Rotation.
"""
    normalized, findings = normalize_event_section(report)
    assert findings == []
    assert normalized.count("Ereignis-Uhrzeit:") == 1
    assert "Szenarien unter/entsprechend/über Konsens:" in normalized


def test_structured_thesis_json_is_market_first_record_when_complete():
    payload = {
        "data_cutoff": "2026-11-02T04:25:13+00:00",
        "theses": [{
            "thesis_id": "ROTATION-2026-11-02", "version": 1,
            "updated_at": "2026-11-02T04:25:13+00:00",
            "data_cutoff": "2026-11-02T04:25:13+00:00",
            "thesis": "Defensive und rohstoffnahe Segmente zeigen relative Stärke.",
            "horizon": "1 bis 4 Wochen",
            "alternative": "Technologie gewinnt relative Stärke zurück.",
            "evidence": ["XLE 3M +19.81%"],
            "counter_evidence": ["Nasdaq 3M -1.84%"],
            "confidence": "mittel",
            "confirm_triggers": ["XLE und XLV halten positive 3M-Werte."],
            "invalidate_triggers": ["XLE und XLV verlieren ihre relative Stärke."],
            "event_dependencies": [], "asset_classes": ["Aktien", "Energie"],
            "candidates": [], "status": "NEU",
        }],
    }
    report = """1. Trading-Radar
Marktregime: Risk-off
Indizes/Marktbreite: S&P 500 -1%; Marktbreite 8/11
Zinsen/Renditen: US 10Y 4.98%
Dollar/Liquidität/Volatilität: VIX 19.42; DXY 101.88
Basisszenario: Konsolidierung.
2. Thesen-Update
""" + "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(payload) + "\n```\n" + """3. Ereignisradar
Keine relevanten Ereignisse.
4. Assetklassenvergleich
Relative Stärke und Rotation.
5. Technische Kandidaten
Kandidatenquellen: Scanner
JETZT TECHNISCH PRÜFEN: keine Kandidaten
AUF TRIGGER WARTEN: keine Kandidaten
EREIGNIS ABWARTEN: keine Kandidaten
NICHT HANDELBAR: keine Kandidaten
6. Portfolio & Risiko
Portfolio geprüft.
7. MSCI-Vergleich
Benchmark verglichen.
8. Daten-/Prüfanhang
Daten geprüft.
"""
    errors = audit_market_first_content(report)
    assert not any("keine einzeln abgrenzbaren Thesen-Datensätze" in item for item in errors), errors
    assert not any("Alternative" in item for item in errors), errors


def test_existing_thesis_json_gets_alternative_only_from_explicit_report_scenarios():
    payload = {
        "data_cutoff": "2026-11-02T04:25:13+00:00",
        "theses": [{
            "thesis_id": "ROTATION-2026-11-02", "version": 1,
            "updated_at": "2026-11-02T04:25:13+00:00",
            "data_cutoff": "2026-11-02T04:25:13+00:00",
            "thesis": "Defensive und rohstoffnahe Segmente zeigen relative Stärke.",
            "horizon": "1 bis 4 Wochen", "evidence": ["XLE 3M +19.81%"],
            "counter_evidence": ["Nasdaq 3M -1.84%"], "confidence": "mittel",
            "confirm_triggers": ["XLE bleibt stark."], "invalidate_triggers": ["XLE wird schwach."],
            "event_dependencies": [], "asset_classes": ["Aktien"], "candidates": [], "status": "NEU",
        }],
    }
    report = """1. Trading-Radar
Hauptthese: Defensive Rotation.
Positives Alternativszenario: Energie und Gesundheit halten Stärke.
Negatives Alternativszenario: Technologie gewinnt Stärke zurück.
2. Thesen-Update
""" + "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(payload) + "\n```\n3. Ereignisradar\n"
    result, changed = ensure_structured_thesis_block(report)
    assert changed is True
    block = re.search(r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", result, re.S)
    parsed = json.loads(block.group(1))
    alternative = parsed["theses"][0]["alternative"]
    assert "Energie und Gesundheit halten Stärke" in alternative
    assert "Technologie gewinnt Stärke zurück" in alternative


def test_m01_validates_numeric_live_benchmark_even_when_portfolio_aggregate_is_unavailable():
    import importlib.util
    spec = importlib.util.spec_from_file_location("report_fixture_m01", ROOT / "tests/test_trading_radar_report_gate.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        "Benchmark-Performance: +0.80%\n"
        "Benchmark-Vergleichsbasis: ForexFactory (LIVE)-Name nicht aufgelöst (SYSTEM); Benchmark=EUNL.DE.\n"
        "System-Performance-ForexFactory (LIVE): -5.74%\n"
        "Out-/Underperformance: -6.54 Prozentpunkte",
    )
    errors = validate_report(report)
    assert any("M-01: Numerischer Benchmarkvergleich" in item for item in errors), errors


def test_m07_reads_thesis_identity_from_the_validated_json_block():
    import importlib.util
    spec = importlib.util.spec_from_file_location("report_fixture_m07", ROOT / "tests/test_trading_radar_report_gate.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    first_thesis = {
        "thesis_id": "T-1", "version": 1,
        "updated_at": "2026-10-30T00:00:00+00:00",
        "data_cutoff": "2026-10-30T00:00:00+00:00",
        "thesis": "Testthese", "horizon": "4 Wochen", "alternative": "Seitwärtsmarkt.",
        "evidence": ["Quelle A"], "counter_evidence": ["Quelle B"],
        "confidence": "mittel", "confirm_triggers": ["Trigger A"],
        "invalidate_triggers": ["Trigger B"], "event_dependencies": [],
        "asset_classes": ["Aktien"], "candidates": [], "status": "NEU",
    }
    second_thesis = dict(first_thesis, thesis_id="T-2", version=3, status="BESTÄTIGT")
    payload = {
        "data_cutoff": "2026-10-30T00:00:00+00:00",
        "theses": [first_thesis, second_thesis],
    }
    report = fixture.report_fixture().replace(
        "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion unverändert.",
        "Thesenregister-Datensatz siehe strukturierten Block.\n```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(payload) + "\n```",
    )
    errors = validate_report(report)
    assert not any("M-07: Thesen-Identität" in item for item in errors), errors


def test_register_update_is_not_repeated_after_first_stage_baseline_creation():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    start = source.index("# Persistenter Gemini-Langzeitkontext")
    end = source.index('print(f"\\nGespeichert: {ausgabe_datei}")', start)
    block = source[start:end]
    assert "update_register(" not in block
    assert "kein zweites Update" in block


def test_m01_accepts_reconciled_live_benchmark_when_portfolio_aggregate_is_unavailable():
    import importlib.util
    spec = importlib.util.spec_from_file_location("report_fixture_m01_valid", ROOT / "tests/test_trading_radar_report_gate.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        "Benchmark-Performance: +0.80%\n"
        "Benchmark-Vergleichsbasis: LIVE-SYSTEM; gleichgewichteter Trade-/Signalvergleich; gleicher Zeitraum je Trade; Benchmark=EUNL.DE.\n"
        "System-Performance-Live: -5.74%\n"
        "Out-/Underperformance: -6.54 Prozentpunkte",
    )
    errors = validate_report(report)
    assert not any("M-01: Numerischer Benchmarkvergleich" in item for item in errors), errors
    assert not any("M-01: Numerischer Live-Systemvergleich" in item for item in errors), errors


def test_production_event_normalizer_adds_only_explicit_header_date_and_keeps_scenario_as_field():
    report = """3. Ereignisradar
- Ereignis-Uhrzeit: HEUTE (2026-11-02) | Zeitzone: NICHT VERFÜGBAR | Kennzahl: ISM PMI | Zeitraum: September 2026 | Letzter Wert: 54,5 | Konsens: NICHT VERFÜGBAR | Quelle: ForexFactory (LIVE) / Official ISM
  - Betroffene These/Assetklasse/konkrete offene Positionen: Rotation / Aktien
- Szenarien unter/entsprechend/über Konsens: Über Konsens stützt zyklische Werte; unter Konsens belastet sie.
- Reaktionskanäle: Zyklische Aktien.
- Was die These bestätigen/widerlegen würde: Wert über 50 bestätigt; unter 50 widerlegt.

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized = production._trading_radar_berichtsfelder_normalisieren(report)
    assert normalized.count("Datum: 2026-11-02") == 1
    assert normalized.count("Ereignis-Uhrzeit:") == 1
    assert "Szenarien unter/entsprechend/über Konsens:" in normalized
    _, findings = normalize_event_section(normalized)
    assert findings == []


def test_structured_thesis_cleanup_keeps_one_marked_block_and_removes_unmarked_draft():
    payload = {
        "data_cutoff": "2026-11-02T04:25:13+00:00",
        "theses": [{
            "thesis_id": "ROTATION-2026-11-02", "version": 1,
            "updated_at": "2026-11-02T04:25:13+00:00",
            "data_cutoff": "2026-11-02T04:25:13+00:00",
            "thesis": "Defensive und rohstoffnahe Segmente zeigen relative Stärke.",
            "horizon": "1 bis 4 Wochen", "alternative": "Technologie gewinnt Stärke zurück.",
            "evidence": ["XLE 3M +19.81%"], "counter_evidence": ["Nasdaq 3M -1.84%"],
            "confidence": "mittel", "confirm_triggers": ["XLE bleibt stark."],
            "invalidate_triggers": ["XLE wird schwach."], "event_dependencies": [],
            "asset_classes": ["Aktien"], "candidates": [], "status": "NEU",
        }],
    }
    report = """1. Trading-Radar
Hauptthese: Defensive Rotation.
2. Thesen-Update
```json
{"thesis_id":"STALE","status":"INVALID"}
```
""" + "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(payload) + "\n```\n3. Ereignisradar\n"
    result, changed = ensure_structured_thesis_block(report)
    assert changed is True
    assert result.count("```TRADING_RADAR_THESEN_JSON") == 1
    assert "STALE" not in result
    assert "```json" not in result


def test_event_parser_rejects_placeholder_or_unrecognized_source():
    report = """3. Ereignisradar
- Ereignis-Uhrzeit: HEUTE (2026-11-02) | Zeitzone: UTC | Kennzahl: PMI | Zeitraum: September | Letzter Wert: 50 | Konsens: 51 | Quelle: NICHT VERFÜGBAR

4. Assetklassenvergleich
Relative Stärke.
"""
    _, findings = normalize_event_section(report)
    assert "Ereignis ohne verifizierte Quelle" in findings


def _run1269_unmarked_thesis_report():
    payload = {
        "data_cutoff": "2026-11-02T18:12:52+00:00",
        "theses": [{
            "thesis_id": "makro_stagflation_2026",
            "version": 1,
            "updated_at": "2026-11-02T18:12:52+00:00",
            "data_cutoff": "2026-11-02T18:12:52+00:00",
            "thesis": (
                "Erhöhte Realzinsen und hartnäckige Inflation belasten zinslose "
                "Assetklassen sowie zyklische Segmente, während infrastrukturnahe "
                "Versorger und Energiekonzerne als Profiteure agieren."
            ),
            "horizon": "4-6 Wochen",
            # This is the exact defect in Run 1269: alternative is absent.
            "evidence": [
                "US 10Y Treasury bei 4,98%",
                "Realzins 10Y TIPS bei 2,63%",
                "PPI YoY bei 12,84%",
            ],
            "counter_evidence": [
                "Stabiler US-Arbeitsmarkt mit Claims bei 194.000",
                "Robuste Nachfrage nach KI-Infrastruktur bei Versorgern",
            ],
            "confidence": "mittel",
            "confirm_triggers": [
                "Anhalten der 10Y-Renditen über 4,90%",
                "Outperformance von US-Versorgern wie Duke Energy Corporation (DUK)",
            ],
            "invalidate_triggers": [
                "Rückgang der 10Y-Renditen unter 4,50%",
                "Signifikantes Absinken der Inflation unter die Fed-Zielmarke",
            ],
            "event_dependencies": [],
            "asset_classes": ["Aktien", "Zinsen", "Edelmetalle"],
            "candidates": [
                "Duke Energy Corporation (DUK)",
                "Southern Company (SO)",
            ],
            "status": "BESTAETIGT",
        }],
    }
    report = """1. Trading-Radar
Positives Alternativszenario: NICHT ABLEITBAR – Datenlücke: Zeithorizont. Es werden keine Trigger-Schwellen, Zeiträume oder Indikatorwerte geschätzt.
Negatives Alternativszenario: NICHT ABLEITBAR – Datenlücke: Zeithorizont. Es werden keine Trigger-Schwellen, Zeiträume oder Indikatorwerte geschätzt.

2. Thesen-Update
Register-Vergleich nicht möglich; Vorzustand/strukturierter Nachweis fehlt.

```json
""" + json.dumps(payload, ensure_ascii=False, indent=2) + """
```

3. Ereignisradar
Keine relevanten Ereignisse im geprüften Zeitraum.
"""
    return report, payload


def test_run1269_unmarked_json_is_recovered_from_explicit_alternative_data_gaps():
    report, _ = _run1269_unmarked_thesis_report()
    recovered, changed = ensure_structured_thesis_block(report)
    assert changed is True
    assert recovered.count("```TRADING_RADAR_THESEN_JSON") == 1
    assert "```json" not in recovered
    match = re.search(
        r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", recovered, re.S
    )
    payload = json.loads(match.group(1))
    thesis = payload["theses"][0]
    assert thesis["status"] == "BESTÄTIGT"
    assert "Positives Alternativszenario: NICHT ABLEITBAR" in thesis["alternative"]
    assert "Negatives Alternativszenario: NICHT ABLEITBAR" in thesis["alternative"]
    from trading_radar_neuauswertung import validate_thesis_register
    assert validate_thesis_register(payload) == []
    assert not any(
        "keine einzeln abgrenzbaren Thesen-Datensätze" in finding
        for finding in audit_market_first_content(recovered)
    )


def test_run1269_recovery_does_not_invent_missing_alternative_scenarios():
    report, _ = _run1269_unmarked_thesis_report()
    report = re.sub(
        r"(?m)^Positives Alternativszenario:.*\n|^Negatives Alternativszenario:.*\n",
        "",
        report,
    )
    recovered, changed = ensure_structured_thesis_block(report)
    assert changed is False
    assert "```TRADING_RADAR_THESEN_JSON" not in recovered
    assert "```json" in recovered


def test_run1269_recovery_creates_first_baseline_without_claiming_comparison_pass(tmp_path):
    report, payload = _run1269_unmarked_thesis_report()
    recovered, changed = ensure_structured_thesis_block(report)
    assert changed is True
    from trading_radar_thesen import update_register
    register = tmp_path / ".trading_radar_thesen.json"
    result = update_register(
        recovered,
        str(register),
        data_cutoff=payload["data_cutoff"],
    )
    assert result["updated"] is True
    assert result["comparison_status"] == "BASELINE_MISSING"
    assert result["previous_sha256"] is None
    assert len(result["current_sha256"]) == 64
    assert register.is_file()


def test_trading_radar_prompt_and_structural_retry_both_require_alternative():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert '"alternative":"belegte Alternative oder ausdrücklich benannte Datenlücke"' in source
    assert '\\"alternative\\":\\"belegte Alternative oder konkrete Datenluecke\\"' in source



def test_report_status_validator_matches_persistence_status_taxonomy():
    from trading_radar_neuauswertung import (
        THESIS_STATUSES,
        canonicalize_thesis_status,
        validate_thesis_register,
    )
    from trading_radar_thesen import VALID_STATUS as PERSISTENCE_STATUSES

    # Canonical statuses must not be rejected by the report gate if persistence accepts them.
    canonical_persistence_statuses = {
        canonicalize_thesis_status(status) for status in PERSISTENCE_STATUSES
    }
    assert canonical_persistence_statuses == THESIS_STATUSES

    template = {
        "thesis_id": "STATUS-CHECK",
        "version": 1,
        "updated_at": "2026-11-02T00:00:00+00:00",
        "data_cutoff": "2026-11-02T00:00:00+00:00",
        "thesis": "Testthese",
        "horizon": "Mittelfristig",
        "alternative": "Alternative bleibt offen.",
        "evidence": ["Beleg"],
        "counter_evidence": ["Gegenbeleg"],
        "confidence": "Mittel",
        "confirm_triggers": ["Trigger A"],
        "invalidate_triggers": ["Trigger B"],
        "event_dependencies": [],
        "asset_classes": ["Aktien"],
        "candidates": [],
        "status": "NEU",
    }
    for status in sorted(PERSISTENCE_STATUSES):
        payload = {"data_cutoff": template["data_cutoff"],
                   "theses": [dict(template, status=status)]}
        assert validate_thesis_register(payload) == [], status

    assert canonicalize_thesis_status("BESTAETIGT") == "BESTÄTIGT"
    assert canonicalize_thesis_status("ABGESCHWAECHT") == "ABGESCHWÄCHT"
    assert canonicalize_thesis_status("WARTET_AUF_EVENT") == "WARTET AUF EREIGNIS"
    assert validate_thesis_register({
        "data_cutoff": template["data_cutoff"],
        "theses": [dict(template, status="UNBEKANNTER_STATUS")],
    })


def test_v3_extracts_json_fence_with_marker_as_first_content_line():
    from trading_radar_neuauswertung import extract_thesis_json
    report, expected = _run1269_unmarked_thesis_report()
    marker_json = report.replace(
        "```json\n" + json.dumps(expected, ensure_ascii=False, indent=2),
        "```json\nTRADING_RADAR_THESEN_JSON\n" + json.dumps(expected, ensure_ascii=False, indent=2),
    )
    parsed = extract_thesis_json(marker_json)
    assert parsed == expected


def test_v3_rejects_readable_thesis_missing_from_structured_json():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report
    report, payload = _run1269_unmarked_thesis_report()
    report = report.replace(
        "2. Thesen-Update\nRegister-Vergleich",
        "2. Thesen-Update\nThesen-ID: AI_ENERGY_2026\nStatus: BESTÄTIGT\n"
        "Thesen-ID: METALS_BOTTOM_2026\nStatus: WIDERLEGT\nRegister-Vergleich",
    )
    payload["theses"][0]["thesis_id"] = "AI_ENERGY_2026"
    findings = validate_thesis_ids_match_report(report, payload)
    assert findings
    assert "METALS_BOTTOM_2026" in findings[0]


def test_v3_accepts_exact_readable_and_json_thesis_id_match():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report
    report, payload = _run1269_unmarked_thesis_report()
    report = report.replace(
        "2. Thesen-Update\nRegister-Vergleich",
        "2. Thesen-Update\nThesen-ID: makro_stagflation_2026\nRegister-Vergleich",
    )
    assert validate_thesis_ids_match_report(report, payload) == []


def test_v3_first_baseline_still_persists_after_structural_recovery(tmp_path):
    from trading_radar_thesen import update_register
    report, payload = _run1269_unmarked_thesis_report()
    # Exercise the alternate ```json + marker form used by the failed production report.
    report = report.replace("```json\n", "```json\nTRADING_RADAR_THESEN_JSON\n", 1)
    recovered, changed = ensure_structured_thesis_block(report)
    assert changed is True
    assert recovered.count("```TRADING_RADAR_THESEN_JSON") == 1
    register = tmp_path / ".trading_radar_thesen.json"
    result = update_register(recovered, str(register), data_cutoff=payload["data_cutoff"])
    assert result["updated"] is True
    assert result["comparison_status"] == "BASELINE_MISSING"
    assert register.is_file()


def test_v3_compact_readable_thesis_ids_are_compared_even_without_thesen_id_labels():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report
    report, payload = _run1269_unmarked_thesis_report()
    report = report.replace(
        "2. Thesen-Update\nRegister-Vergleich",
        "2. Thesen-Update\nAI_ENERGY_2026 – BESTÄTIGT\n"
        "METALS_BOTTOM_2026 – WIDERLEGT\nRegister-Vergleich",
    )
    payload["theses"][0]["thesis_id"] = "AI_ENERGY_2026"
    findings = validate_thesis_ids_match_report(report, payload)
    assert findings and "METALS_BOTTOM_2026" in findings[0]


def test_v3_explicit_id_and_compact_status_on_same_line_are_not_double_counted():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report
    report, payload = _run1269_unmarked_thesis_report()
    report = report.replace(
        "2. Thesen-Update\nRegister-Vergleich",
        "2. Thesen-Update\nThesen-ID: makro_stagflation_2026 | BESTÄTIGT\nRegister-Vergleich",
    )
    assert validate_thesis_ids_match_report(report, payload) == []


# V4: exact production report from GitHub Actions run #1270 is the regression oracle.
def test_v4_original_run1270_report_fails_closed_on_missing_second_thesis():
    from trading_radar_neuauswertung import (
        extract_thesis_json, validate_thesis_ids_match_report,
        validate_radar_report_strict,
    )
    report = (ROOT / "tests/fixtures/trading_radar_run1270_original.txt").read_text(encoding="utf-8")
    payload = extract_thesis_json(report)
    findings = validate_thesis_ids_match_report(report, payload)
    assert findings
    assert "METALS_BOTTOM_2026" in findings[0]
    assert any("METALS_BOTTOM_2026" in item for item in validate_radar_report_strict(report))


def test_v4_original_run1270_ids_are_extracted_from_nested_name_normalizer_format():
    from trading_radar_neuauswertung import extract_thesis_json, validate_thesis_ids_match_report
    report = (ROOT / "tests/fixtures/trading_radar_run1270_original.txt").read_text(encoding="utf-8")
    payload = extract_thesis_json(report)
    section2 = report.split("### 2. Thesen-Update", 1)[1].split("### 3. Ereignisradar", 1)[0]
    assert "AI_ENERGY_2026" in section2
    assert "METALS_BOTTOM_2026" in section2
    assert validate_thesis_ids_match_report(report, payload)


def test_v4_complete_run1270_baseline_persists_both_readable_theses(tmp_path):
    from trading_radar_neuauswertung import (
        extract_thesis_json, validate_thesis_ids_match_report,
        validate_radar_report_strict,
    )
    from trading_radar_thesen import update_register
    report = (ROOT / "tests/fixtures/trading_radar_run1270_original.txt").read_text(encoding="utf-8")
    payload = extract_thesis_json(report)
    first = dict(payload["theses"][0])
    second = dict(first)
    second.update({
        "thesis_id": "METALS_BOTTOM_2026", "version": 1,
        "thesis": "Edelmetall-Bodenbildung",
        "evidence": ["Originalbericht: alle vier Edelmetalle unter EMA200/WMA200"],
        "counter_evidence": ["Originalbericht: keine bestätigte bullische Divergenz"],
        "status": "WIDERLEGT",
    })
    complete = {"data_cutoff": payload["data_cutoff"], "theses": [first, second]}
    assert validate_thesis_ids_match_report(report, complete) == []
    register = tmp_path / ".trading_radar_thesen.json"
    repaired_block = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(complete, ensure_ascii=False, indent=2) + "\n```"
    repaired, count = re.subn(
        r"(?ms)^```json\s*\nTRADING_RADAR_THESEN_JSON\s*\n.*?^```[ \t]*$",
        repaired_block,
        report,
        count=1,
    )
    assert count == 1
    assert validate_radar_report_strict(repaired) == []
    result = update_register(repaired, str(register), data_cutoff=complete["data_cutoff"])
    assert result["updated"] is True
    saved = json.loads(register.read_text(encoding="utf-8"))
    assert {item["thesis_id"] for item in saved["theses"]} == {"AI_ENERGY_2026", "METALS_BOTTOM_2026"}
    assert result["comparison_status"] == "BASELINE_MISSING"


def test_v4_readable_thesis_without_any_extractable_id_is_rejected():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report
    payload = {"theses": []}
    report = "### 2. Thesen-Update\n* These 1: Edelmetall-Bodenbildung – WIDERLEGT\n### 3. Ereignisradar\nKeine Ereignisse."
    findings = validate_thesis_ids_match_report(report, payload)
    assert findings and "keine eindeutigen These-IDs" in findings[0]


def test_v4_duplicate_json_thesis_ids_are_rejected():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report
    thesis = {"thesis_id": "AI_ENERGY_2026"}
    payload = {"theses": [dict(thesis), dict(thesis)]}
    report = "### 2. Thesen-Update\nAI_ENERGY_2026 – BESTÄTIGT\n### 3. Ereignisradar\nKeine Ereignisse."
    findings = validate_thesis_ids_match_report(report, payload)
    assert findings and "doppelte thesis_id" in findings[0]
