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
