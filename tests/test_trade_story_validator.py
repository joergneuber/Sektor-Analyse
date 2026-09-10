from __future__ import annotations

import ast
import csv
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEMINI = ROOT / "gemini_auswertung.py"


def load_validator_namespace():
    tree = ast.parse(GEMINI.read_text(encoding="utf-8"), filename=str(GEMINI))
    wanted = {
        "_normalisiere_positionsname",
        "_normalisiere_ticker",
        "_trade_story_setup_universum",
        "_trade_story_beobachtung_universum",
        "_trade_story_bloecke",
        "_trade_story_kandidaten_schluessel",
        "_trade_story_keys_treffen",
        "_trade_story_validierung",
        "_trade_story_deterministische_reparatur",
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns = {"re": re, "os": os, "json": json, "csv": csv}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(GEMINI), "exec"), ns)
    return ns


def write_csv(path: Path, header, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


def main():
    ns = load_validator_namespace()

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        setups = td / "Setups(2026-09-10).csv"
        write_csv(
            setups,
            ["Name", "Status2", "Ticker"],
            [
                ["Nebius Group N.V.", "VALIDE", "NBIS"],
                ["The Williams Companies, Inc.", "VALIDE", "WMB"],
                ["EOG Resources, Inc.", "ACHTUNG", "EOG"],
            ],
        )
        trendwende = td / "Trendwende_Setups(2026-09-10).csv"
        write_csv(trendwende, ["Ticker", "Name"], [])
        short = td / "Short_Setups(2026-09-10).csv"
        write_csv(short, ["Ticker", "Name", "Status2"], [])
        metals = td / "Edelmetalle_Setups(2026-09-10).csv"
        write_csv(metals, ["Ticker", "Name", "Status2"], [])
        obs = td / "einzel_check_beobachtung.json"
        obs.write_text(json.dumps({
            "EOG": {"status": "KAUFKANDIDAT B", "name": "EOG Resources, Inc."},
            "NBIS": {"status": "KAUFKANDIDAT A", "name": "Nebius Group N.V."},
            "TSM": {"status": "KAUFKANDIDAT A", "name": "Taiwan Semiconductor Manufacturing Company Limited"},
        }), encoding="utf-8")
        files = {
            "Setups(...).csv": str(setups),
            "Trendwende_Setups(...).csv": str(trendwende),
            "Short_Setups(...).csv": str(short),
            "Edelmetalle_Setups(...).csv": str(metals),
        }

        # Real-format regression: Status2=VALIDE in normal Setups must count.
        universe, read, missing = ns["_trade_story_setup_universum"](files)
        assert ns["_normalisiere_ticker"]("NBIS") in universe
        assert ns["_normalisiere_ticker"]("WMB") in universe
        assert ns["_normalisiere_ticker"]("EOG") not in universe
        assert not missing

        def validate(story):
            return ns["_trade_story_validierung"](story, files, str(obs))

        valid = """6.1 PERSPEKTIVISCHE TRADE-IDEEN\nNebius Story\nZeithorizont: mittelfristig\nBestehender Kandidat / Bezug: Nebius Group N.V. (NBIS)\nStatus: VALIDE SETUP\nNächster technischer Trigger: halten\nRisiko: Makro\n"""
        ok, errors = validate(valid)
        assert ok, errors

        # A valid setup mentioned as multiple titles must be recognized.
        multi = """6.1 PERSPEKTIVISCHE TRADE-IDEEN\nTechnologie-Story\nZeithorizont: mittelfristig\nBestehender Kandidat / Bezug: F5, Inc. (FFIV)\nStatus: VALIDE SETUP\nNächster technischer Trigger: Trigger\nRisiko: Risiko\n"""
        ok, errors = validate(multi)
        assert not ok, "FFIV is not authoritative in this fixture and must fail"

        # Prepared must come from the current observation list.
        prepared = """6.1 PERSPEKTIVISCHE TRADE-IDEEN\nEnergy Story\nZeithorizont: kurzfristig\nBestehender Kandidat / Bezug: EOG Resources, Inc. (EOG)\nStatus: VORBEREITET\nNächster technischer Trigger: Breakout\nRisiko: Risiko\n"""
        ok, errors = validate(prepared)
        assert ok, errors

        # Prepared must reject a name that is only a valid setup, not a current observer candidate.
        prepared_invalid = """6.1 PERSPEKTIVISCHE TRADE-IDEEN\nWMB Story\nZeithorizont: kurzfristig\nBestehender Kandidat / Bezug: The Williams Companies, Inc. (WMB)\nStatus: VORBEREITET\nNächster technischer Trigger: Breakout\nRisiko: Risiko\n"""
        ok, errors = validate(prepared_invalid)
        assert not ok and any("VORBEREITET" in e for e in errors)

        # Interesting is intentionally allowed outside the observation list.
        interesting = """6.1 PERSPEKTIVISCHE TRADE-IDEEN\nSilver structural theme\nZeithorizont: langfristig\nBestehender Kandidat / Bezug: iShares Physical Silver ETC (PPFD.SG)\nStatus: INTERESSANT\nNächster technischer Trigger: erst später\nRisiko: Nachfrage\n"""
        ok, errors = validate(interesting)
        assert ok, errors

        # No authoritative setup source => VALIDE SETUP must be rejected.
        ok, errors = ns["_trade_story_validierung"](valid, {}, str(obs))
        assert not ok and any("nicht verifizierbar" in e for e in errors)

        # Quota regression: invalid Trade-Story must be repairable locally,
        # without a second Gemini API call. Invalid VALIDE SETUP is downgraded
        # conservatively to INTERESSANT; buy language is neutralized.
        invalid = """6.1 PERSPEKTIVISCHE TRADE-IDEEN

Unknown Story
Zeithorizont: kurzfristig
Bestehender Kandidat / Bezug: F5, Inc. (FFIV)
Status: VALIDE SETUP
Naechster technischer Trigger: jetzt kaufen
Risiko: Risiko
"""
        repaired = ns["_trade_story_deterministische_reparatur"](invalid, files, str(obs))
        assert "Status: INTERESSANT" in repaired
        assert "jetzt kaufen" not in repaired.lower()
        ok, errors = ns["_trade_story_validierung"](repaired, files, str(obs))
        assert ok, errors

    source = GEMINI.read_text(encoding="utf-8")
    assert "Langfrist_Bewertung(...).csv" in source
    assert "Langfrist_Briefing(...).txt" in source
    assert "TRADE_STORY_DETERMINISTISCHE_REPARATUR_TERMINAL" in source
    assert "ohne Gemini-API-Call" in source
    assert "PORTFOLIO-MAKRO-ABGLEICH / WARNER" in source
    print("TRADE_STORY_VALIDATOR_TESTS: 7 PASS")


if __name__ == "__main__":
    main()
