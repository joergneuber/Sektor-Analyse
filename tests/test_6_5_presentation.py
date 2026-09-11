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
    tree = ast.parse(GEMINI.read_text(encoding="utf-8"), filename=str(GEMINI))
    wanted = {"_lade_6_5_statusverlauf", "_kurzstatus", "erstelle_6_5_autoritative_liste"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    ns = {"os": os, "json": json, "datetime": datetime, "re": __import__("re")}
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
            json.dumps({"Datum": today, "Ticker": "AMD", "Status": "KAUFKANDIDAT A", "Vorheriger_Status": "KAUFKANDIDAT A"}),
            json.dumps({"Datum": today, "Ticker": "CVX", "Status": "KAUFKANDIDAT B", "Vorheriger_Status": "KAUFKANDIDAT C"}),
            json.dumps({"Datum": today, "Ticker": "TSM", "Status": "KAUFKANDIDAT C", "Vorheriger_Status": "KAUFKANDIDAT B"}),
            json.dumps({"Datum": today, "Ticker": "XOM", "Status": "KEIN KANDIDAT", "Vorheriger_Status": "KAUFKANDIDAT B"}),
            json.dumps({"Datum": today, "Ticker": "ZZZ", "Status": "KAUFKANDIDAT B", "Vorheriger_Status": None}),
        ]) + "\n", encoding="utf-8")

        out = ns["erstelle_6_5_autoritative_liste"](str(obs), str(history))
        assert "6.5.1 AKTUELLE KAUFKANDIDATEN A (1 Titel):" in out
        assert "- AMD | A -> A | aktueller Status: KAUFKANDIDAT A" in out
        assert "6.5.2 AKTUELLE NICHT-A-KANDIDATEN (4 Titel):" in out
        assert "B:" in out and "C:" in out and "Kein Kandidat:" in out
        assert "- CVX | C -> B | Quelle: HEBELTRADER 164/26" in out
        assert "- TSM | B -> C | Quelle: -" in out
        assert "- XOM | B -> Kein Kandidat | Quelle: -" in out
        assert "- ZZZ | NICHT BEKANNT -> B | Quelle: -" in out
        # All non-A entries remain present; no artificial five-title cap exists.
        assert sum(out.count(f"- {ticker} |") for ticker in ("CVX", "TSM", "XOM", "ZZZ")) == 4

    print("6.5_PRESENTATION_TEST: PASS")


if __name__ == "__main__":
    main()
