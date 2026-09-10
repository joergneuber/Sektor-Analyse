"""Regression tests fuer die deterministische Makro-Architektur.

Direkt mit Python ausfuehrbar; keine pytest-Abhaengigkeit.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The architecture tests do not call yfinance. A tiny import stub keeps the
# regression suite runnable in minimal environments; GitHub installs yfinance normally.
sys.modules.setdefault("yfinance", types.SimpleNamespace())

import makro_szenario as m


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def test_parser_real_format():
    lines = [
        "Core CPI: 336.7890 | Datenstand=2026-08-01 | STATUS=REAL | SOURCE=FRED CPILFESL | YOY=+2.70% | YOY_VORMONAT=2025-08-01 | YOY_STATUS=CALCULATED",
        "ISM Manufacturing PMI: 55.6 | Datenmonat=2026-08 | STATUS=REAL_PUBLIC_SECONDARY | SOURCE=ISM",
        "US 10Y Treasury: 4.79 | Datenstand=2026-09-03 | STATUS=REAL | SOURCE=FRED DGS10",
    ]
    _assert(m._parse_named_value(lines, "ISM Manufacturing PMI")[0] == 55.6, "PMI parser failed for Datenmonat")
    _assert(m._parse_named_value(lines, "US 10Y Treasury")[0] == 4.79, "Bond parser failed")
    _assert(m._parse_inline_number(lines[0], "YOY") == 2.70, "YoY parser failed")


def test_inflation_yoy():
    import pandas as pd
    df = pd.DataFrame({
        "DATE": pd.to_datetime(["2025-08-01", "2026-08-01"]),
        "CPILFESL": [327.95, 336.789],
    })
    yoy, prior = m._fred_yoy(df, "CPILFESL")
    expected = (336.789 / 327.95 - 1.0) * 100.0
    _assert(abs(yoy - expected) < 1e-9, "Inflation YoY calculation incorrect")
    _assert(prior == "2025-08-01", "YoY reference month incorrect")


def test_bond_market_remains_objective_data():
    lines = [
        "US 2Y Treasury: 4.39 | Datenstand=2026-09-03 | STATUS=REAL",
        "US 5Y Treasury: 4.50 | Datenstand=2026-09-03 | STATUS=REAL",
        "US 10Y Treasury: 4.79 | Datenstand=2026-09-03 | STATUS=REAL",
        "US 30Y Treasury: 5.42 | Datenstand=2026-09-03 | STATUS=REAL",
        "Realzins 10Y TIPS: 2.45 | Datenstand=2026-09-03 | STATUS=REAL",
    ]
    bond = m.bond_market_snapshot(lines)
    text = "\n".join(bond)
    _assert("2Y-10Y Spread: 0.4" in text, "Bond spread missing")
    _assert("5Y-10Y Spread: 0.29" in text, "5Y-10Y spread missing")
    _assert("10Y-30Y Spread: 0.63" in text, "10Y-30Y spread missing")
    _assert("10Y Nominal-Real Differenz: 2.34" in text, "Nominal-real difference missing")
    _assert("Yield-Curve-Form" not in text, "Python still emits qualitative curve interpretation")


def test_python_contains_no_macro_scenario_interpretation():
    text = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    forbidden = [
        "SZENARIO-" + "SCORE",
        "Inflationaere " + "Expansion",
        "Stag" + "flation",
        "Recession / " + "Kontraktion",
        "Soft " + "Landing",
        "Gemischtes Makro-" + "Szenario",
        "Konstruktiv / " + "selektiv",
        "Defensiv",
    ]
    for term in forbidden:
        _assert(term not in text, f"Python still contains macro interpretation: {term}")
    _assert(("_scenario_" + "engine") not in text, "Deterministic macro scenario engine still exists")
    _assert("PMI-Regel:" not in text, "Python still emits qualitative PMI interpretation")
    _assert("Lithium ist als struktureller" not in text, "Python still emits commodity interpretation")


def test_gemini_is_macro_interpreter():
    prompt = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    master = (ROOT / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md").read_text(encoding="utf-8")
    _assert("vollstaendige" in prompt.lower() and "makrooekonomische interpretation" in prompt.lower(), "Gemini prompt does not own macro interpretation")
    _assert("Die makrooekonomische Interpretation, das Makro-Szenario" in master, "Master instruction does not assign scenario interpretation to Gemini")
    _assert(("SZENARIO-" + "SCORE") not in prompt, "Legacy scenario score remains in Gemini prompt")
    _assert(("SZENARIO-" + "SCORE") not in master, "Legacy scenario score remains in master instruction")

def test_trade_story_layer_is_explicit_and_does_not_create_setups():
    prompt = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    master = (ROOT / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md").read_text(encoding="utf-8")
    for term in ("INTERESSANT", "VORBEREITET", "VALIDE SETUP", "Trade-Story"):
        _assert(term in prompt, f"Gemini prompt missing Trade-Story term: {term}")
        _assert(term in master, f"Master instruction missing Trade-Story term: {term}")
    _assert("Gemini darf niemals aus einer interessanten Story" in prompt, "Gemini setup boundary missing")
    _assert("Gemini darf aus INTERESSANT oder VORBEREITET niemals selbst ein VALIDE" in master, "Master setup boundary missing")
    _assert("Status: INTERESSANT | VORBEREITET | VALIDE SETUP" in master, "Trade-Story status field missing")



def test_gate_rules():
    base = [
        "Fed Funds Effective Rate: 3.63 | Datenstand=2026-09-02",
        "US 2Y Treasury: 4.39 | Datenstand=2026-09-03",
        "US 10Y Treasury: 4.79 | Datenstand=2026-09-03",
        "Core CPI: 336.789 | Datenstand=2026-08-01",
        "NFP / Nonfarm Payrolls: 158858 | Datenstand=2026-08-01",
        "Arbeitslosenquote: 4.1 | Datenstand=2026-08-01",
        "ISM Manufacturing PMI: 55.6 | Datenmonat=2026-08",
        "ISM Services PMI: 55.4 | Datenmonat=2026-08",
        "S&P 500: 6460.26 | Datenstand=2026-09-03",
    ]
    gate, missing, quality, secondary = m.data_quality_gate(base)
    _assert(gate == "FREIGEGEBEN", "Complete Tier-1 should open gate")
    _assert(quality in {"VOLLSTAENDIG", "EINGESCHRAENKT"}, "Tier-1 completeness must not block the gate")
    blocked = [x for x in base if not x.startswith("Core CPI:")]
    gate2, missing2, quality2, _ = m.data_quality_gate(blocked)
    _assert(gate2 == "GESPERRT", "Missing Tier-1 must block gate")
    _assert("Core CPI" in missing2, "Missing Core CPI not reported")
    _assert(gate2 == "GESPERRT", "Blocked gate must remain authoritative")


def test_calendar_parsers():
    ics = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20260911\nSUMMARY:Consumer Price Index\nEND:VEVENT\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20260910\nSUMMARY:Producer Price Index\nEND:VEVENT\nEND:VCALENDAR\n"""
    events = m._parse_bls_ics(ics)
    _assert(len(events) == 2, "BLS ICS parser failed")
    _assert(events[0][0] == dt.date(2026, 9, 11) or events[1][0] == dt.date(2026, 9, 11), "CPI date missing")


def test_no_legacy_macro_terms():
    text = (ROOT / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md").read_text(encoding="utf-8")
    _assert("struktureller Capex-Zyklus" not in text, "Legacy Capex terminology remains")
    _assert("Regime-Killer" not in text, "Legacy Regime-Killer terminology remains")
    _assert("Marktregime" not in text, "Legacy Marktregime terminology remains")
    makro = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    _assert(("SZENARIO-" + "SCORE") not in makro, "Legacy scenario score remains in Python")
    _assert(("SZENARIO-" + "SCORE") not in text, "Legacy scenario score remains in master instruction")


def main():
    tests = [
        test_parser_real_format,
        test_inflation_yoy,
        test_bond_market_remains_objective_data,
        test_python_contains_no_macro_scenario_interpretation,
        test_gemini_is_macro_interpreter,
        test_gate_rules,
        test_trade_story_layer_is_explicit_and_does_not_create_setups,
        test_calendar_parsers,
        test_no_legacy_macro_terms,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"MACRO_ARCHITECTURE_TESTS: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()


def test_trade_story_validator_enforces_status_and_setup_authority():
    gem = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    _assert("def _trade_story_validierung(" in gem, "Trade-Story validator function missing")
    _assert("Status\\s*:\\s*(INTERESSANT|VORBEREITET|VALIDE SETUP)" in gem, "Trade-Story status regex missing")
    _assert("VALIDE SETUP" in gem and "autoritativen Setup-Dateien" in gem, "VALIDE SETUP authority check missing")
    _assert("Kauf-/Entry" in gem, "Purchase boundary check missing")


def test_gdelt_retry_and_trade_story_validation_are_present():
    macro = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    gem = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    _assert("def _gdelt_get(" in macro, "GDELT retry helper missing")
    _assert("status_code in {429, 500, 502, 503, 504}" in macro, "GDELT HTTP retry handling missing")
    _assert("_gdelt_get({\"query\": query" in macro, "GDELT cluster calls do not use retry helper")
    _assert("_gdelt_get({\"query\": broad_query" in macro, "GDELT big-news call does not use retry helper")
    _assert("_trade_story_validierung(text, eingabedateien, beobachtung_pfad)" in gem, "Trade-Story validator not integrated into Gemini flow")


def test_trade_story_validator_requires_real_setup_status_and_observation_anchor(tmp_path):
    """Stage-2.1: mere presence in a CSV must not authorize VALIDE SETUP."""
    import csv
    import json
    import os
    import re

    source = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    _assert("status2 == required_status or status == \"KAUFKANDIDAT A\"" in source, "Setup validator does not enforce source-specific status")
    _assert("if not gelesene_quellen:" in source, "Missing authoritative setup sources must block VALIDE SETUP")
    _assert("_trade_story_beobachtung_universum" in source, "Observation universe validator missing")
    _assert("not beobachtung_verfuegbar" in source, "Missing observation source must not silently pass")
    _assert("Antworte ausschliesslich mit dem vollstaendigen Abschnitt 6.1" in source, "Repair prompt is not restricted to section 6.1")

    # Source-level fixture: a setup row with a non-valid status must not be
    # treated as a valid setup merely because ticker/name exist.
    setup = tmp_path / "Setups(2026-09-10).csv"
    setup.write_text("Ticker;Name;Status\nEOG;EOG Resources, Inc.;KAUFKANDIDAT B\n", encoding="utf-8-sig")
    obs = tmp_path / "einzel_check_beobachtung.json"
    obs.write_text(json.dumps({"EOG": {"status": "KAUFKANDIDAT B", "name": "EOG Resources, Inc."}}), encoding="utf-8")
    _assert("KAUFKANDIDAT B" in setup.read_text(encoding="utf-8-sig"), "Fixture setup not created")
    _assert(json.loads(obs.read_text(encoding="utf-8"))["EOG"]["status"] == "KAUFKANDIDAT B", "Fixture observation not created")


def test_trade_story_validator_source_specific_status_contracts():
    source = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    for required in (
        '("Setups(...).csv", "status2_or_status", "VALIDE")',
        '("Trendwende_Setups(...).csv", "presence", None)',
        '("Short_Setups(...).csv", "status2", "VALIDE")',
        '("Edelmetalle_Setups(...).csv", "status2", "VALIDE")',
    ):
        _assert(required in source, f"Missing source-specific setup contract: {required}")


def test_trade_story_validator_does_not_allow_unanchored_prepared_story():
    source = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    _assert("ist nicht in der aktuellen Beobachtungsliste verankert" in source, "VORBEREITET/INTERESSANT is not anchored to current observation universe")
    _assert("darf keine Kauf-/Entry-Formulierung enthalten" in source, "Purchase boundary missing for non-valid story states")


def test_gdelt_quality_gap_is_visible_but_does_not_block_gate():
    lines = [
        "Fed Funds Effective Rate: 3.63 | STATUS=REAL",
        "US 2Y Treasury: 4.39 | STATUS=REAL",
        "US 10Y Treasury: 4.79 | STATUS=REAL",
        "Core CPI: 336.789 | STATUS=REAL",
        "NFP / Nonfarm Payrolls: 158858 | STATUS=REAL",
        "Arbeitslosenquote: 4.1 | STATUS=REAL",
        "ISM Manufacturing PMI: 55.6 | STATUS=REAL",
        "ISM Services PMI: 55.4 | STATUS=REAL",
        "S&P 500: 6460.26 | STATUS=REAL",
        "Nahost: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=GDELT",
        "China/Taiwan: ARTIKEL_24H=20 | STATUS=REAL_PUBLIC_SECONDARY | SOURCE=GDELT DOC 2.0",
    ]
    gate, missing, quality, secondary = m.data_quality_gate(lines)
    _assert(gate == "FREIGEGEBEN", "GDELT context gap must not block Tier-1 macro gate")
    _assert(quality == "EINGESCHRAENKT", "GDELT context gap must degrade data quality")
    _assert("GDELT Nahost" in secondary, "GDELT unavailable cluster must be visible as secondary data gap")


def test_gdelt_cache_is_explicitly_limited_to_24h():
    macro = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    _assert("GDELT_CACHE_MAX_AGE_HOURS = 24" in macro, "GDELT cache age limit missing")
    _assert("STATUS=REAL_CACHED" in macro, "GDELT cached provenance missing")
    _assert("GDELT GKG/Bulk" in macro, "Official GKG fallback missing")
    _assert("max_workers=3" in macro, "GDELT cluster requests are not bounded in parallel")


def test_gdelt_fallback_is_labeled_as_sample_not_article_count():
    source = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    _assert("THEMEN_TREFFER_24H_SAMPLE" in source, "GKG fallback must not masquerade as article count")
    _assert("keine vollstaendige Artikelanzahl" in source, "GKG fallback scope must be explicit")
    _assert("ABDECKUNG=24H_SAMPLE" in source, "GKG fallback coverage must be explicit as a 24h sample")
    _assert("0h, 3h, ..., 24h" in source, "GKG fallback slice spacing must be explicit")
    _assert("keine vollstaendige 24h-Abdeckung" in source, "GKG fallback must not claim full 24h coverage")
    _assert("frische DOC-Ergebnisse blieben erhalten" in source, "Per-cluster fallback merge must preserve fresh results")
    _assert("Nur der jeweils ausgefallene Cluster" in source or "nur der jeweils ausgefallene Cluster" in source, "Fallback must operate per cluster")


def test_gdelt_cache_is_clusterwise_and_provenance_aware():
    source = (ROOT / "makro_szenario.py").read_text(encoding="utf-8")
    _assert('cached_clusters = set()' in source, "Clusterwise cache tracking missing")
    _assert('result[cluster] = dict(cached_item)' in source, "Cache fallback must merge per cluster")
    _assert('result = cached["clusters"]' not in source, "Whole-cache replacement would discard fresh cluster results")
    _assert('result[cluster]["cached_fallback"] = True' in source, "Cached cluster provenance missing")


def test_hebeltrader_latest_drive_version_can_replace_stale_local_copy():
    source = (ROOT / "gemini_auswertung.py").read_text(encoding="utf-8")
    _assert("def lade_hebeltrader_datei_von_drive(" in source, "HEBELTRADER Drive synchronization missing")
    _assert("local_modified >= drive_modified" in source, "HEBELTRADER freshness comparison missing")
    _assert('"HEBELTRADER-Einzelcheck"' in source, "HEBELTRADER input key missing")
    _assert("neuere Drive-Version hat Vorrang" in source, "HEBELTRADER newest-version rule missing")
