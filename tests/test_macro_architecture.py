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


def test_bond_market_and_scenario_connection():
    lines = [
        "ISM Manufacturing PMI: 55.6 | Datenmonat=2026-08 | STATUS=REAL",
        "ISM Services PMI: 55.4 | Datenmonat=2026-08 | STATUS=REAL",
        "Core CPI: 336.789 | Datenstand=2026-08-01 | YOY=+2.70% | YOY_STATUS=CALCULATED | STATUS=REAL",
        "Core PCE: 130.658 | Datenstand=2026-07-01 | YOY=+2.90% | YOY_STATUS=CALCULATED | STATUS=REAL",
        "Arbeitslosenquote: 4.1 | Datenstand=2026-08-01 | STATUS=REAL",
        "NFP / Nonfarm Payrolls: 158858 | Datenstand=2026-08-01 | STATUS=REAL",
        "Fed Funds Effective Rate: 3.63 | Datenstand=2026-09-02 | STATUS=REAL",
        "US 2Y Treasury: 4.39 | Datenstand=2026-09-03 | STATUS=REAL",
        "US 10Y Treasury: 4.79 | Datenstand=2026-09-03 | STATUS=REAL",
        "Realzins 10Y TIPS: 2.45 | Datenstand=2026-09-03 | STATUS=REAL",
        "US High Yield OAS: 2.66 | Datenstand=2026-09-03 | STATUS=REAL",
        "Chicago Fed NFCI: -0.566 | Datenstand=2026-09-03 | STATUS=REAL",
        "VIX: 14.32 | Datenstand=2026-09-03 | STATUS=REAL",
        "S&P 500: 6460.26 | Datenstand=2026-09-03 | STATUS=REAL",
        "2Y-10Y Spread: 0.40 | STATUS=CALCULATED",
        "Nahost: ARTIKEL_24H=25 | STATUS=REAL_PUBLIC_SECONDARY",
        "China/Taiwan: ARTIKEL_24H=10 | STATUS=REAL_PUBLIC_SECONDARY",
        "Russland/Ukraine: ARTIKEL_24H=12 | STATUS=REAL_PUBLIC_SECONDARY",
    ]
    bond = m.bond_market_snapshot(lines)
    _assert(any(x.startswith("2Y-10Y Spread:") for x in bond), "Bond spread missing")
    gate, missing, quality, secondary = m.data_quality_gate(lines)
    # S&P/ISM etc are present; data_quality_gate only needs exact critical lines.
    _assert(gate == "FREIGEGEBEN", f"Unexpected gate: {gate} / {missing}")
    out = m._scenario_engine(lines, gate, quality, secondary)
    text = "\n".join(out)
    _assert("MAKRO-SZENARIO:" in text, "Scenario missing")
    _assert("SZENARIO-SCORE:" in text, "Scenario score missing")
    _assert("MARKTUMFELD:" in text, "Marktumfeld missing")
    _assert("Anleihenmarkt:" in text, "Bond axis not connected to scenario engine")
    _assert("Core CPI YoY=2.7" in text, "Scenario engine did not use YoY inflation")


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
    blocked_out = m._scenario_engine(blocked, gate2, quality2, [])
    _assert("SZENARIO-SCORE: NICHT VERFUEGBAR" in "\n".join(blocked_out), "Blocked gate leaked scenario score")


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
    _assert("MAKRO-SZENARIO -> SZENARIO-SCORE -> MARKTUMFELD" in (ROOT / "makro_szenario.py").read_text(encoding="utf-8"), "Scenario architecture missing")


def main():
    tests = [
        test_parser_real_format,
        test_inflation_yoy,
        test_bond_market_and_scenario_connection,
        test_gate_rules,
        test_calendar_parsers,
        test_no_legacy_macro_terms,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"MACRO_ARCHITECTURE_TESTS: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()
