"""Regression tests fuer die deterministische 6.5-Darstellung."""
from __future__ import annotations

import ast
import datetime
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEMINI = ROOT / "gemini_auswertung.py"


def main():
    source = GEMINI.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(GEMINI))
    assert "Name (Ticker)" in source and "gesamten fertigen Auswertung" in source
    wanted = {"_lade_6_5_statusverlauf", "_kurzstatus", "_lade_6_5_namen", "_normalisiere_ticker", "_normalisiere_positionsname", "erstelle_6_5_autoritative_liste"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns = {"os": os, "json": json, "datetime": datetime, "re": __import__("re"), "csv": __import__("csv"), "Path": Path}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(GEMINI), "exec"), ns)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        obs = td / "einzel_check_beobachtung.json"
        obs.write_text(json.dumps({
            "AMD": {"status": "KAUFKANDIDAT A", "quelle": "-"},
            "CVX": {"status": "KAUFKANDIDAT B", "quelle": "HEBELTRADER 164/26"},
            "TSM": {"status": "KAUFKANDIDAT C", "quelle": "-"},
            "XOM": {"status": "KEIN KANDIDAT", "quelle": "-"},
            "ZZZ": {"status": "KAUFKANDIDAT B", "quelle": "-"},
        }), encoding="utf-8")
        history = td / "einzel_check_historie.jsonl"
        today = datetime.date.today().isoformat()
        history.write_text("\n".join([
            json.dumps({"Datum": today, "Ticker": "AMD", "Name": "Advanced Micro Devices, Inc.", "Status": "KAUFKANDIDAT A", "Vorheriger_Status": "KAUFKANDIDAT A"}),
            json.dumps({"Datum": today, "Ticker": "CVX", "Name": "Chevron Corporation", "Status": "KAUFKANDIDAT B", "Vorheriger_Status": "KAUFKANDIDAT C"}),
            json.dumps({"Datum": today, "Ticker": "TSM", "Name": "Taiwan Semiconductor Manufacturing Company Limited", "Status": "KAUFKANDIDAT C", "Vorheriger_Status": "KAUFKANDIDAT B"}),
            json.dumps({"Datum": today, "Ticker": "XOM", "Name": "Exxon Mobil Corporation", "Status": "KEIN KANDIDAT", "Vorheriger_Status": "KAUFKANDIDAT B"}),
            json.dumps({"Datum": today, "Ticker": "ZZZ", "Name": "ZZZ Holdings", "Status": "KAUFKANDIDAT B", "Vorheriger_Status": None}),
        ]) + "\n", encoding="utf-8")

        out = ns["erstelle_6_5_autoritative_liste"](str(obs), str(history))
        assert "6.5.1 AKTUELLE KAUFKANDIDATEN A (1 Titel):" in out
        assert "- Advanced Micro Devices, Inc. (AMD) | A -> A | aktueller Status: KAUFKANDIDAT A" in out
        assert "- AMD | A -> A" not in out
        assert "6.5.2 AKTUELLE NICHT-A-KANDIDATEN (3 Titel):" in out
        assert "B:" in out and "C:" in out and "Kein Kandidat:" not in out
        assert "- Chevron Corporation (CVX) | C -> B | Quelle: HEBELTRADER 164/26" in out
        assert "- Taiwan Semiconductor Manufacturing Company Limited (TSM) | B -> C | Quelle: -" in out
        assert "- ZZZ Holdings (ZZZ) | NICHT BEKANNT -> B | Quelle: -" in out
        assert "Exxon Mobil Corporation (XOM)" not in out
        # No artificial five-title cap exists; all current B/C entries remain present.
        assert all(ticker in out for ticker in ("(CVX)", "(TSM)", "(ZZZ)"))

    print("6.5_PRESENTATION_TEST: PASS")


if __name__ == "__main__":
    main()
