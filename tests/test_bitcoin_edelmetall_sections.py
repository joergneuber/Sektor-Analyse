"""Gezielte Regressionstests fuer Bitcoin-Root-Mapping, Edelmetall-Normalisierung
und identische Abschnittsbereiche von Normalizer und Gate.

Ausfuehrung:
  python test_bitcoin_edelmetall_sections.py <gemini_auswertung.py>

Die Produktionsfunktionen werden per AST geladen; kein Gemini/API-Aufruf.
"""
from __future__ import annotations
import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

_source_override = next(
    (Path(arg) for arg in sys.argv[1:]
     if not arg.startswith("-") and Path(arg).is_file() and Path(arg).suffix.lower() == ".py"),
    None,
)
# unittest.main() interprets leftover positional arguments as test names.
# Remove only the recognized source-file override before the test runner starts.
if _source_override is not None:
    _source_arg = str(_source_override)
    sys.argv[:] = [sys.argv[0]] + [arg for arg in sys.argv[1:] if arg != _source_arg]
SOURCE = _source_override.resolve() if _source_override else Path(__file__).resolve().parents[1] / "gemini_auswertung.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
FUNCTIONS = {n.name: n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

def load(names, ns):
    mod = ast.Module(body=[FUNCTIONS[n] for n in names], type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), str(SOURCE), "exec"), ns)

class TargetedRegression(unittest.TestCase):
    def test_bitcoin_root_json_asset_ticker_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "Trade_Story_Bitcoin.json"
            p.write_text(json.dumps({"asset": "Bitcoin", "ticker": "BTC-USD"}), encoding="utf-8")
            ns = {"re": re, "json": json, "Path": Path, "os": __import__("os"),
                  "csv": __import__("csv"), "DATEIMUSTER": {"Trade_Story_Universum(...).json": "universe.json",
                  "Trade_Story_Bitcoin(...).json": "Trade_Story_Bitcoin.json",
                  "Setups(...).csv": "none", "Trade_Story_Setup_Rohuniversum(...).csv": "none",
                  "Trendwende_Setups(...).csv": "none", "Short_Setups(...).csv": "none",
                  "Edelmetalle_Setups(...).csv": "none", "Langfrist_Bewertung(...).csv": "none",
                  "Trade_Story_Aktienuniversum(...).csv": "none", "Einzel_Check_A_Meldungen(...).txt": "none"},
                  "finde_datei": lambda pattern: str(Path(tmp) / pattern) if pattern == "Trade_Story_Bitcoin.json" else str(Path(tmp)/"universe.json")}
            (Path(tmp)/"universe.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")
            load(["_lade_name_ticker_quellenmapping"], ns)
            names, tickers = ns["_lade_name_ticker_quellenmapping"]()
            self.assertEqual(names.get("bitcoin"), {"BTC-USD"})
            self.assertEqual(tickers.get("BTC-USD"), {"Bitcoin"})

    def test_gold_and_silver_are_normalized_in_analysis_sections(self):
        ns = {"re": re, "_lade_name_ticker_quellenmapping": lambda: (
            {"gold": {"GC=F"}, "silber": {"SI=F"}},
            {"GC=F": {"Gold"}, "SI=F": {"Silber"}})}
        load(["_name_ticker_pruefbereiche", "_normalisiere_name_ticker_ausgabe"], ns)
        source = "8. Edelmetalle\nGold steigt; Silber bleibt stabil.\n11. Methodik\nGold und Silber sind nur Beispiele.\n"
        result = ns["_normalisiere_name_ticker_ausgabe"](source)
        self.assertIn("Gold (GC=F) steigt; Silber (SI=F) bleibt stabil.", result)
        self.assertIn("11. Methodik\nGold und Silber sind nur Beispiele.", result)

    def test_normalizer_and_gate_use_same_section_definition(self):
        ns = {"re": re, "_lade_name_ticker_quellenmapping": lambda: (
            {"gold": {"GC=F"}}, {"GC=F": {"Gold"}})}
        load(["_name_ticker_pruefbereiche", "_normalisiere_name_ticker_ausgabe", "_pruefe_name_ticker_gate"], ns)
        report = "7. Portfolio\nGold ohne Ticker\n8. Edelmetalle\nGold ohne Ticker\n11. Methodik\nGold ohne Ticker\n"
        spans = ns["_name_ticker_pruefbereiche"](report)
        checked_blocks = [report[start:end] for start, end in spans]
        self.assertEqual(len(checked_blocks), 2)
        self.assertTrue(all("11. Methodik" not in block for block in checked_blocks))
        self.assertIn("7. Portfolio", checked_blocks[0])
        self.assertIn("8. Edelmetalle", checked_blocks[1])
        with self.assertRaisesRegex(RuntimeError, "NAME_TICKER_GATE_UNGUELTIG"):
            ns["_pruefe_name_ticker_gate"](report)
        normalized = ns["_normalisiere_name_ticker_ausgabe"](report)
        self.assertIn("Gold (GC=F)", normalized)
        # Punkt 11 wird weder normalisiert noch vom Gate beanstandet.
        only_methodology = "11. Methodik\nGold ohne Ticker\n"
        self.assertEqual(ns["_normalisiere_name_ticker_ausgabe"](only_methodology), only_methodology)
        ns["_pruefe_name_ticker_gate"](only_methodology)

if __name__ == "__main__":
    unittest.main(verbosity=2)
