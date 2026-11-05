"""Regression tests for the 2026-11-05 Trading-Radar report-gate fixes."""
import importlib.util
from pathlib import Path

from trading_radar_market_first import normalize_event_section
from trading_radar_report_gate import validate_report


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "trading_radar_report_gate_fixture",
    ROOT / "tests" / "test_trading_radar_report_gate.py",
)
_FIXTURE_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE_MODULE)


def _base_report():
    return _FIXTURE_MODULE.report_fixture()


def test_m09_nested_candidate_bullets_stay_with_their_identity_record():
    candidates = """- Asset/Ticker: Example Corp (EXM) | Setup-ID: EXM-LONG-01
  - Sektor: Technologie
  - Richtung: LONG
  - Trigger: Ausbruch über Widerstand
  - Einstieg: 100
  - Stop: 95
  - TP1: 110
  - TP2: NICHT VERFÜGBAR
  - CRV: 2
  - Gegenargument: Ausbruch kann fehlschlagen
  - Regel-/Datenstatus: Daten geprüft
  - Statusgruppe: JETZT TECHNISCH PRÜFEN
- Asset/Ticker: Second Corp (SEC) | Setup-ID: SEC-LONG-01
  - Sektor: Industrie
  - Richtung: LONG
  - Trigger: Rückeroberung des Widerstands
  - Einstieg: 200
  - Stop: 190
  - TP1: 220
  - TP2: NICHT VERFÜGBAR
  - CRV: 2
  - Gegenargument: Momentum kann nachlassen
  - Regel-/Datenstatus: Daten geprüft
  - Statusgruppe: JETZT TECHNISCH PRÜFEN"""
    report = _base_report().replace(
        "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | "
        "Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; "
        "Regelstatus: valide.",
        candidates,
    )
    errors = validate_report(report)
    assert not [item for item in errors if item.startswith("M-09:")], errors


def test_m04_matrix_rows_are_checked_inside_their_category_blocks():
    matrix = """Relative-Stärke-Matrix / Kapitalrotationsvergleich
Datenstichtag: 2026-11-04T00:00:00+00:00. Fenster: 1M und 3M.
Gewinner: Technologie; Verlierer: Versorger; Divergenz: Growth vs. Value.
Indizes:
- S&P 500 | 1M: +1.2% | 3M: +3.4%
- Nasdaq 100 | 1M: +1.5% | 3M: +4.0%
Regionen:
- USA | 1M: +1.0% | 3M: +2.8%
- Europa | 1M: +0.5% | 3M: +1.7%
Sektoren:
- Technologie | 1M: +2.0% | 3M: +5.2%
- Energie | 1M: -0.5% | 3M: +1.1%
Anlageklassen:
- Aktien | 1M: +1.3% | 3M: +3.0%
- Gold | NICHT VERFÜGBAR (Datenlücke: keine vergleichbaren Reihen)
"""
    report = _base_report()
    start = report.index("4. Assetklassenvergleich")
    end = report.index("5. Technische Kandidaten", start)
    report = report[:start] + "4. Assetklassenvergleich\n" + matrix + "\n" + report[end:]
    errors = validate_report(report)
    assert not [item for item in errors if item.startswith("M-04:")], errors


def test_m04_does_not_borrow_values_from_another_category_block():
    matrix = """Relative-Stärke-Matrix / Kapitalrotationsvergleich
Datenstichtag: 2026-11-04. Fenster: 1M und 3M.
Gewinner: Technologie; Verlierer: Versorger; Divergenz: Growth vs. Value.
Indizes:
- S&P 500 | 1M: +1.2% | 3M: +3.4%
Regionen:
- USA | keine Daten
Sektoren:
- Technologie | 1M: +2.0% | 3M: +5.2%
Anlageklassen:
- Aktien | 1M: +1.3% | 3M: +3.0%
"""
    report = _base_report()
    start = report.index("4. Assetklassenvergleich")
    end = report.index("5. Technische Kandidaten", start)
    report = report[:start] + "4. Assetklassenvergleich\n" + matrix + "\n" + report[end:]
    errors = validate_report(report)
    assert any("M-04: Regionen benötigen" in item for item in errors), errors


def test_event_source_newline_wirkung_and_wrapped_live_verification_are_continuations():
    report = """3. Ereignisradar
- Ereignis: FOMC-Sitzung
  - Datum: 2026-12-09
  - Uhrzeit: 14:00
  - Zeitzone: America/New_York
  - Kennzahl: FOMC-Zinsentscheidung
  - Berichtszeitraum: Dezember 2026
  - Letzter Wert: 4.00
  - Konsens: 4.00
  - Quelle: Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: Name nicht aufgelöst (LIVE_VERIFIZIERT) — offizieller Fed-Kalender live geprüft
*   **FOMC-Sitzung** | 2026-12-09 | Zinsentscheid | 2026-12-09 | Letzter Wert: 3,88% | Konsens: 4,00%-4,25% | Quelle: FRED/CME
  - Wirkung: Höhere Zinsen belasten zinssensitive Sektoren.

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    assert findings == []
    assert normalized.count("- Ereignis: FOMC-Sitzung") == 1
    assert "Quelle: Federal Reserve official calendar" in normalized
    assert "Wirkung: Höhere Zinsen" in normalized
    assert "LIVE_VERIFIZIERT" in normalized


def test_naked_thesis_json_marker_is_fenced_and_stable_id_is_recovered(tmp_path):
    import copy
    import json

    from trading_radar_neuauswertung import (
        ensure_structured_thesis_block,
        extract_thesis_json,
        validate_radar_report_strict,
        validate_thesis_ids_match_report,
        validate_thesis_register,
    )
    from trading_radar_thesen import update_register

    payload = {
        "data_cutoff": "2026-11-05T04:31:42+00:00",
        "theses": [{
            "thesis_id": "Name nicht aufgelöst (MARKET_REGIME_2026_Q4)",
            "version": 1,
            "updated_at": "2026-11-05T04:31:42+00:00",
            "data_cutoff": "2026-11-05T04:31:42+00:00",
            "thesis": "Restriktive Zinspolitik stützt defensive Cashflow-Generatoren.",
            "horizon": "Q4 2026",
            "alternative": "Kurzfristige Inflationsdaten verhindern eine abschließende Bewertung.",
            "evidence": ["Fed Funds Effective Rate 3.88%", "10Y Treasury 4.98%"],
            "counter_evidence": ["VIX-Anstieg", "Underperformance des Russell 2000"],
            "confidence": "mittel",
            "confirm_triggers": ["Stabile PCE-Daten"],
            "invalidate_triggers": ["Einbruch Rohöl unter 200-Tage-Durchschnitt"],
            "event_dependencies": [],
            "asset_classes": ["Energie", "Rohstoffe", "Gesundheit"],
            "candidates": ["Nucor Corporation (NUE)", "Chevron Corporation (CVX)"],
            "status": "BESTAETIGT",
        }],
    }
    report = (
        "1. Trading-Radar\nMarktregime: Risk-off\n"
        "2. Thesen-Update\n\nRegister-Vergleich nicht möglich.\n\n"
        "TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\nTHESENREGISTER-VERGLEICH: NOT_UPDATED\nVORHER-SHA256: NICHT VERFÜGBAR\n"
        + "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
    )

    normalized, changed = ensure_structured_thesis_block(report)
    assert changed is True
    assert normalized.count("```TRADING_RADAR_THESEN_JSON") == 1
    parsed = extract_thesis_json(normalized)
    assert parsed["theses"][0]["thesis_id"] == "MARKTREGIME_2026_11"
    assert validate_thesis_register(parsed) == []
    assert validate_thesis_ids_match_report(normalized, parsed) == []
    strict_findings = validate_radar_report_strict(normalized)
    assert not [item for item in strict_findings if "Thesen" in item or "JSON" in item], strict_findings

    # The actual production register update must now be able to compare against
    # a present baseline and produce deterministic before/after evidence.
    baseline_record = copy.deepcopy(parsed["theses"][0])
    baseline_record.update({
        "thesis_id": "MARKTREGIME_2026_11",
        "version": 3,
        "updated_at": "2026-11-04T14:44:07+00:00",
        "data_cutoff": "2026-11-03T00:00:00+00:00",
        "thesis": "Anhaltend straffes monetäres Umfeld bei robuster konjunktureller Basis und moderater Volatilität.",
        "horizon": "Mittel- bis langfristig",
        "alternative": "Rasche Liquiditätslockerung durch unerwartete Notenbank-Zinsschritte.",
        "evidence": ["Fed Funds Effective Rate bei 3,88%", "10Y US-Treasury-Rendite bei 4,98%"],
        "counter_evidence": ["Schwankungen in spezifischen Industriebereichen und Sektoren"],
        "confidence": "mittel",
        "confirm_triggers": ["Stabilität der Renditen und anhaltend moderater VIX"],
        "invalidate_triggers": ["Rascher Anstieg der Arbeitslosenquote über 4,5% oder abrupter Renditeeinbruch"],
        "asset_classes": ["Aktien/ETFs", "Zinsen/Dollar"],
        "candidates": [],
        "status": "BESTÄTIGT",
        "active": True,
    })
    baseline_path = tmp_path / ".trading_radar_thesen.json"
    baseline_path.write_text(
        json.dumps({
            "schema_version": 1,
            "updated_at": "2026-11-04T14:44:07+00:00",
            "theses": [baseline_record],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    result = update_register(
        normalized,
        str(baseline_path),
        data_cutoff="2026-11-05T04:31:42+00:00",
    )
    assert result["updated"] is True
    assert result["comparison_status"] == "PASS"
    assert result["previous_sha256"]
    assert result["current_sha256"]
    assert result["new_theses"] == []
    assert result["updated_theses"] == ["MARKTREGIME_2026_11"]
    assert result["archived_theses"] == []
    assert result["thesis_versions"][0]["version"] == 4


def test_same_day_distinct_fomc_decision_and_minutes_remain_separate():
    report = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  - Datum: 2026-12-09
  - Uhrzeit: 14:00
  - Zeitzone: America/New_York
  - Kennzahl: FOMC-Zinsentscheid
  - Berichtszeitraum: Dezember 2026
  - Quelle: Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: LIVE_VERIFIZIERT — offizieller Fed-Kalender live geprüft
* FOMC-Zinsentscheid | 2026-12-09 | Zinsentscheid | 2026-12-09
  - Wirkung: Zinspfad beeinflusst Bewertungsmultiplikatoren.
* FOMC-Sitzungsprotokoll (Minutes) | 2026-12-09 | Protokollveröffentlichung
  - Datum: 2026-12-09
  - Uhrzeit: 14:00
  - Zeitzone: America/New_York
  - Kennzahl: FOMC-Sitzungsprotokoll
  - Berichtszeitraum: Dezember 2026
  - Letzter Wert: NICHT VERFÜGBAR (Protokollveröffentlichung ohne Messwert)
  - Quelle: Federal Reserve official minutes https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: LIVE_VERIFIZIERT — offizieller Fed-Kalender live geprüft

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    assert findings == [], findings
    assert normalized.count("FOMC-Zinsentscheid") >= 2  # header plus continuation summary
    assert "FOMC-Sitzungsprotokoll (Minutes)" in normalized
    assert normalized.count("Datum: 2026-12-09") == 2


def _load_production_event_normalizer():
    import ast
    import re

    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_trading_radar_berichtsfelder_normalisieren"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "re": re,
        "_trading_radar_nicht_handhabbare_kandidaten_normalisieren": lambda value: value,
    }
    exec(compile(module, "Trading_Radar_Alleinauswertung.py", "exec"), namespace)
    return namespace["_trading_radar_berichtsfelder_normalisieren"]


def test_full_production_event_normalization_order_keeps_wirkung_and_fomc_identity():
    report = """1. Trading-Radar
Marktregime: restriktiv.
2. Thesen-Update
Thesenregister geprüft.
3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  - Datum: 2026-12-09
  - Uhrzeit: 14:00
  - Zeitzone: America/New_York
  - Kennzahl: FOMC-Zinsentscheid
  - Berichtszeitraum: Dezember 2026
  - Letzter Wert: 4.00%
  - Konsens: 4.00%-4.25%
  - Quelle: Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: LIVE_VERIFIZIERT — offizieller Fed-Kalender live geprüft
* FOMC-Zinsentscheid | 2026-12-09 | Zinsentscheid | 2026-12-09
  * Wirkung: Höhere Zinsen belasten zinssensitive Sektoren.
* FOMC-Sitzungsprotokoll (Minutes) | 2026-12-09 | Protokollveröffentlichung
  - Datum: 2026-12-09
  - Uhrzeit: 14:00
  - Zeitzone: America/New_York
  - Kennzahl: FOMC-Sitzungsprotokoll
  - Berichtszeitraum: Dezember 2026
  - Letzter Wert: NICHT VERFÜGBAR (Protokollveröffentlichung ohne Messwert)
  - Quelle: Federal Reserve official minutes https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: LIVE_VERIFIZIERT — offizieller Fed-Kalender live geprüft
4. Assetklassenvergleich
Relative Stärke und Kapitalrotation.
5. Technische Kandidaten
JETZT TECHNISCH PRÜFEN: Keine Kandidaten
6. Portfolio & Risiko
Risiko bleibt datenabhängig.
7. MSCI-Vergleich
Portfolio-Gesamtperformance: NICHT VERFÜGBAR
Ø Positionsperformance: NICHT VERFÜGBAR
8. Daten-/Prüfanhang
Quellen und Datenstände dokumentiert.
"""
    # This is the exact relevant production order in Trading_Radar_Alleinauswertung.py.
    event_normalized, findings = normalize_event_section(report)
    assert findings == [], findings
    production_normalizer = _load_production_event_normalizer()
    production_normalized = production_normalizer(event_normalized)

    assert production_normalized.count("Wirkung:") == 1
    assert "Wirkung: Höhere Zinsen belasten zinssensitive Sektoren." in production_normalized
    assert production_normalized.count("- Datum: 2026-12-09") == 2
    assert "Datum: NICHT VERFÜGBAR" not in production_normalized
    assert "Uhrzeit: NICHT VERFÜGBAR" not in production_normalized
    assert "FOMC-Sitzungsprotokoll (Minutes)" in production_normalized
    assert "Wirkung: Höhere Zinsen belasten zinssensitive Sektoren.\nUhrzeit:" not in production_normalized

    # Complete the relevant release path with the actual strict report gate.
    gate_report = _FIXTURE_MODULE.report_fixture(ohlcv_warning=False)
    event_start = gate_report.index("3. Ereignisradar")
    event_end = gate_report.index("4. Assetklassenvergleich", event_start)
    production_section = production_normalized[
        production_normalized.index("3. Ereignisradar"):
        production_normalized.index("4. Assetklassenvergleich")
    ]
    gate_report = gate_report[:event_start] + production_section + gate_report[event_end:]
    gate_errors = validate_report(gate_report)
    event_gate_errors = [
        error for error in gate_errors
        if "Ereignis" in error or error.startswith("EVENT")
    ]
    assert event_gate_errors == [], gate_errors



def test_report_gate_keeps_same_day_fomc_decision_and_minutes_as_separate_records():
    """Exercise Market-First -> production field normalization -> strict Report Gate."""
    import re
    report = _base_report()
    event_start = report.index("3. Ereignisradar")
    event_end = report.index("4. Assetklassenvergleich", event_start)
    event_section = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  - Datum: 2026-12-09
  - Uhrzeit: 14:00
  - Zeitzone: America/New_York
  - Kennzahl: FOMC-Zinsentscheid
  - Berichtszeitraum: Dezember 2026
  - Letzter Wert: 4.00%
  - Konsens: 4.00%-4.25%
  - Quelle: Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: LIVE_VERIFIZIERT — offizieller Fed-Kalender live geprüft
* FOMC-Zinsentscheid | 2026-12-09 | Zinsentscheid | 2026-12-09
  - Wirkung: Höhere Zinsen belasten zinssensitive Sektoren.
* FOMC-Sitzungsprotokoll (Minutes) | 2026-12-09 | Protokollveröffentlichung
  - Datum: 2026-12-09
  - Kennzahl: FOMC-Sitzungsprotokoll
  - Quelle: Federal Reserve official minutes https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Quellenprüfung: LIVE_VERIFIZIERT — offizieller Fed-Kalender live geprüft
"""
    report = report[:event_start] + event_section + report[event_end:]
    market_first_text, findings = normalize_event_section(report)
    assert findings == [], findings
    production_normalizer = _load_production_event_normalizer()
    normalized = production_normalizer(market_first_text)
    event_section = normalized[
        normalized.index("3. Ereignisradar"):
        normalized.index("4. Assetklassenvergleich")
    ]
    # The continuation summary belongs to the decision; the Minutes record is
    # a separate event even though both occur on the same calendar date.
    assert "FOMC-Sitzungsprotokoll (Minutes)" in event_section
    minutes_start = normalized.index("* FOMC-Sitzungsprotokoll (Minutes)")
    minutes_end = normalized.index("4. Assetklassenvergleich", minutes_start)
    minutes_record = normalized[minutes_start:minutes_end]
    minutes_record = re.sub(
        r"(?im)^\s*(?:[-*]\s*)?(?:Uhrzeit|Zeitzone|Berichtszeitraum|Letzter Wert|Konsens)\s*[:=].*\n",
        "",
        minutes_record,
    )
    gate_probe = normalized[:minutes_start] + minutes_record + normalized[minutes_end:]
    gate_errors = validate_report(gate_probe)
    m08_errors = [error for error in gate_errors if error.startswith("M-08:")]
    assert len(m08_errors) == 1, gate_errors
    assert "Ereignisdatensatz 2" in m08_errors[0], gate_errors


def test_thesis_id_migration_is_identical_for_naked_canonical_and_generic_json_formats():
    import copy
    import json

    from trading_radar_neuauswertung import (
        ensure_structured_thesis_block,
        extract_thesis_json,
        validate_thesis_ids_match_report,
    )

    payload = {
        "data_cutoff": "2026-11-05T04:31:42+00:00",
        "theses": [{
            "thesis_id": "Name nicht aufgelöst (MARKET_REGIME_2026_Q4)",
            "version": 3,
            "updated_at": "2026-11-05T04:31:42+00:00",
            "data_cutoff": "2026-11-05T04:31:42+00:00",
            "thesis": "Restriktive Zinspolitik stützt defensive Cashflow-Generatoren.",
            "horizon": "Q4 2026",
            "alternative": "Kurzfristige Inflationsdaten verhindern eine abschließende Bewertung.",
            "evidence": ["Fed Funds Effective Rate 3.88%"],
            "counter_evidence": ["VIX-Anstieg"],
            "confidence": "mittel",
            "confirm_triggers": ["Stabile PCE-Daten"],
            "invalidate_triggers": ["Deutliche geldpolitische Lockerung"],
            "event_dependencies": [],
            "asset_classes": ["Energie", "Rohstoffe"],
            "candidates": ["Nucor Corporation (NUE)"],
            "status": "BESTÄTIGT",
        }],
    }

    reports = []
    for fmt in ("naked", "canonical", "generic"):
        # Identical readable thesis fields across all supported JSON formats.
        report = (
            "1. Trading-Radar\nMarktregime: Risk-off\n"
            "2. Thesen-Update\n"
            "Thesen-ID: Name nicht aufgelöst (MARKET_REGIME_2026_Q4)\n"
            "Version: 3\nStatus: BESTÄTIGT\n"
            "Alternative: Kurzfristige Inflationsdaten verhindern eine abschließende Bewertung.\n"
        )
        serialized = json.dumps(copy.deepcopy(payload), ensure_ascii=False, indent=2)
        if fmt == "naked":
            report += "TRADING_RADAR_THESEN_JSON\n" + serialized + "\n"
        elif fmt == "canonical":
            report += "```TRADING_RADAR_THESEN_JSON\n" + serialized + "\n```\n"
        else:
            report += "```json\n" + serialized + "\n```\n"
        report += "3. Ereignisradar\nKeine relevanten Ereignisse.\n"

        normalized, changed = ensure_structured_thesis_block(report)
        assert changed is True, fmt
        parsed = extract_thesis_json(normalized)
        assert parsed["theses"][0]["thesis_id"] == "MARKTREGIME_2026_11", fmt
        assert "Thesen-ID: MARKTREGIME_2026_11" in normalized, fmt
        assert "MARKET_REGIME_2026_Q4" not in normalized, fmt
        assert validate_thesis_ids_match_report(normalized, parsed) == [], fmt
        reports.append(normalized)

    assert extract_thesis_json(reports[0]) == extract_thesis_json(reports[1])
    assert extract_thesis_json(reports[0]) == extract_thesis_json(reports[2])

def test_naked_thesis_json_recovery_rejects_two_consecutive_json_objects():
    import json
    from trading_radar_neuauswertung import ensure_structured_thesis_block

    payload = {
        "data_cutoff": "2026-11-05T04:31:42+00:00",
        "theses": [{
            "thesis_id": "MARKET_REGIME_2026_Q4", "version": 1,
            "updated_at": "2026-11-05T04:31:42+00:00",
            "data_cutoff": "2026-11-05T04:31:42+00:00",
            "thesis": "Restriktives monetäres Regime stützt defensive Cashflows.",
            "horizon": "Q4 2026", "alternative": "Unerwartete Lockerung.",
            "evidence": ["Fed Funds Effective Rate 3.88%"],
            "counter_evidence": ["Robuste Aktienkurse"], "confidence": "mittel",
            "confirm_triggers": ["Renditen bleiben hoch"],
            "invalidate_triggers": ["Renditen fallen deutlich"],
            "event_dependencies": [], "asset_classes": ["Aktien", "Zinsen"],
            "candidates": [], "status": "BESTAETIGT",
        }],
    }
    raw = json.dumps(payload, ensure_ascii=False)
    report = (
        "1. Trading-Radar\nMarktregime: Risk-off\n"
        "2. Thesen-Update\nTRADING_RADAR_THESEN_JSON\n"
        + raw + "\n" + raw + "\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
    )
    normalized, changed = ensure_structured_thesis_block(report)
    assert changed is False
    assert normalized == report
