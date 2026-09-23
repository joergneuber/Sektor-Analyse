from __future__ import annotations

import ast
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEMINI = ROOT / "gemini_auswertung.py"


def _load_functions(names):
    source = GEMINI.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = set(names)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns = {"re": re, "csv": csv, "json": json, "os": __import__("os"), "datetime": __import__("datetime")}
    # Normalizer dependencies are included explicitly.
    ordered = [
        "_normalisiere_ticker", "_normalisiere_positionsname", "_parse_technische_zahl",
        "_extrahiere_technische_referenzen", "_technische_ref_fuer_kandidat",
        "_sichere_technische_assetangaben", "_normalisiere_technische_nachsuche_felder",
        "_ergaenze_technische_nachsuche", "_trade_story_bloecke",
        "_extrahiere_makro_referenzwerte", "_sichere_makro_kritische_kompaktangaben",
    ]
    by_name = {n.name: n for n in nodes}
    # Pull all known direct dependencies from the source as well.
    for name in ordered:
        if name not in by_name:
            for n in tree.body:
                if isinstance(n, ast.FunctionDef) and n.name == name:
                    by_name[name] = n
                    break
    selected = [by_name[n] for n in ordered if n in by_name]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(GEMINI), "exec"), ns)
    return ns


def test_structured_discovery_is_not_lost_without_legacy_heading():
    ns = _load_functions({"_trade_story_bloecke"})
    text = """6.1 PERSPEKTIVISCHE TRADE-IDEEN
Thema: Kupfer-/Infrastrukturrotation
Zeithorizont: Monate
Was veraendert sich?: Kupfer steigt deutlich.
Kausalzusammenhang: Infrastrukturinvestitionen -> Kupferbedarf.
Wohin fliesst Kapital?: Rohstoffe / Industrie
Bestehender Kandidat / Bezug: Kein bestehender Kandidat im Datenbestand
Discovery-Status: ENTDECKT
Technischer Status: NICHT VORHANDEN
Naechster bestaetigender Trigger: weiterer Nachfragebeleg
Widerlegender Trigger: Nachfrageeinbruch
Gegentreiber / Risiko: Konjunkturabkuehlung
6.2 MARKTUMFELD
"""
    stories = ns["_trade_story_bloecke"](text)
    assert len(stories) == 1
    assert "Kupfer-/Infrastrukturrotation" in stories[0]
    assert "Discovery-Status: ENTDECKT" in stories[0]


def test_technical_asset_values_cannot_inherit_gold_price(tmp_path):
    ns = _load_functions({"_sichere_technische_assetangaben"})
    setup = tmp_path / "Setups(2026-09-23).csv"
    with setup.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Ticker", "Name", "Kurs", "Einstieg", "Stop", "TP1", "TP2", "CRV1", "Status2"], delimiter=";")
        w.writeheader()
        w.writerow({"Ticker": "AU", "Name": "AngloGold Ashanti plc", "Kurs": "104.60", "Einstieg": "104.60", "Stop": "98.20", "TP1": "123.39", "TP2": "130.00", "CRV1": "2.94", "Status2": "VALIDE"})
    history = tmp_path / "einzel_check_historie.jsonl"
    history.write_text(json.dumps({
        "Datum": "2026-09-23", "Ticker": "AU", "Name": "AngloGold Ashanti plc",
        "Status": "KAUFKANDIDAT A",
        "Technik": {"Trendfolge": {"Kurs": 104.60, "Einstieg": 104.60, "Stop": 98.20, "TP1": 123.39, "CRV1": 2.94}}
    }) + "\n", encoding="utf-8")
    files = {"Setups(...).csv": str(setup), "Einzel-Check-Technikhistorie": str(history)}
    text = """6.1 PERSPEKTIVISCHE TRADE-IDEEN
Thema: Goldproduzenten
Zeithorizont: Monate
Bestehender Kandidat / Bezug: AngloGold Ashanti plc (AU)
Kurs: 4353,00
Einstieg: 4353,00
Stop: 98,20
TP1: 123,39
CRV1: 2,94
Discovery-Status: BEOBACHTUNG
Technischer Status: VALIDER SETUP
"""
    out, changes = ns["_sichere_technische_assetangaben"](text, files)
    assert "AngloGold Ashanti plc (AU)" in out
    assert "Kurs: 104.6" in out or "Kurs: 104,6" in out
    assert "Einstieg: 104.6" in out or "Einstieg: 104,6" in out
    assert "Kurs: 4353,00" not in out
    assert changes


def test_tips_and_reverse_spread_phrasing_are_bound_to_correct_metrics():
    ns = _load_functions({"_extrahiere_makro_referenzwerte", "_sichere_makro_kritische_kompaktangaben"})
    makro = """US 2Y Treasury: 4.7600 | STATUS=REAL
US 10Y Treasury: 4.9600 | STATUS=REAL
Realzins 10Y TIPS: 2.4600 | STATUS=REAL
2Y-10Y Spread: 0.2000 | STATUS=CALCULATED
"""
    text = "10J-2J-Spread ist mit 4,76%-Pkt. positiv. Realzinsen (10Y TIPS bei 4,96%) sind hoch."
    out, changes = ns["_sichere_makro_kritische_kompaktangaben"](text, makro)
    assert "0,20 Prozentpunkte" in out
    assert "Realzinsen (10Y TIPS bei 2,46%)" in out
    assert "4,76%-Pkt." not in out
    assert "4,96%)" not in out
    assert len(changes) >= 2


def test_corrupted_gemini_nachsuche_label_is_normalized_without_changing_assets():
    ns = _load_functions({"_normalisiere_technische_nachsuche_felder"})
    text = """6.1 PERSPEKTIVISCHE TRADE-IDEEN
Bestehender Kandidat / Bezug: Amazon.com, Inc. (AMZN)
PotenzielAmazon.com, Inc. (AMZN)he Nachsuche: Amazon.com, Inc. (AMZN)
PoteMicrosoft Corporation (MSFT)he Nachsuche: Microsoft Corporation (MSFT)
Discovery-Status: BEOBACHTUNG
Technischer Status: VALIDER SETUP
6.2 TRENDFOLGE
"""
    out, changes = ns["_normalisiere_technische_nachsuche_felder"](text)
    assert "PotenzielAmazon.com, Inc. (AMZN)he Nachsuche" not in out
    assert "PoteMicrosoft Corporation (MSFT)he Nachsuche" not in out
    assert out.count("Potenzielle Assets fuer technische Nachsuche:") == 2
    assert "Amazon.com, Inc. (AMZN)" in out
    assert "Microsoft Corporation (MSFT)" in out
    assert len(changes) == 2


def test_corrupted_nachsuche_label_is_normalized_and_then_searched(tmp_path):
    ns = _load_functions({"_ergaenze_technische_nachsuche"})
    setup = tmp_path / "Setups(2026-09-23).csv"
    with setup.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Ticker", "Name", "Kurs", "Status2"], delimiter=";")
        w.writeheader()
        w.writerow({"Ticker": "AMZN", "Name": "Amazon.com, Inc.", "Kurs": "254.98", "Status2": "VALIDE"})
    files = {"Setups(...).csv": str(setup)}
    text = """6.1 PERSPEKTIVISCHE TRADE-IDEEN
PotenzielAmazon.com, Inc. (AMZN)he Nachsuche: Amazon.com, Inc. (AMZN)
Bestehender Kandidat / Bezug: Amazon.com, Inc. (AMZN)
Discovery-Status: BEOBACHTUNG
Technischer Status: VALIDER SETUP
"""
    out, notes = ns["_ergaenze_technische_nachsuche"](text, files)
    assert "PotenzielAmazon.com, Inc. (AMZN)he Nachsuche" not in out
    assert "Amazon.com, Inc. (AMZN) | technisches Tagesuniversum: VALIDE" in out
    assert notes


def test_potential_assets_are_searched_without_creating_a_setup_status(tmp_path):
    ns = _load_functions({"_ergaenze_technische_nachsuche"})
    setup = tmp_path / "Setups(2026-09-23).csv"
    with setup.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Ticker", "Name", "Kurs", "Status2"], delimiter=";")
        w.writeheader()
        w.writerow({"Ticker": "FCX", "Name": "Freeport-McMoRan Inc.", "Kurs": "45.10", "Status2": "VALIDE"})
    files = {"Setups(...).csv": str(setup)}
    text = """6.1 PERSPEKTIVISCHE TRADE-IDEEN
Thema: Kupfer-/Infrastrukturrotation
Potenzielle Assets fuer technische Nachsuche: Freeport-McMoRan Inc. (FCX) | Unbekannter Kupferwert (XYZ)
Bestehender Kandidat / Bezug: Kein bestehender Kandidat im Datenbestand
Discovery-Status: ENTDECKT
Technischer Status: NICHT VORHANDEN
"""
    out, notes = ns["_ergaenze_technische_nachsuche"](text, files)
    assert "Freeport-McMoRan Inc. (FCX) | technisches Tagesuniversum: VALIDE" in out
    assert "Unbekannter Kupferwert (XYZ) | noch nicht im technischen Tagesuniversum verifiziert" in out
    assert "Technischer Status: NICHT VORHANDEN" in out
    assert notes
