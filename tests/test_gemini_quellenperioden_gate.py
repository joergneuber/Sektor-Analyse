"""Isolierte Regressionstests für das Quellen-/Perioden-Abschluss-Gate.

Die Tests laden ausschließlich die geprüften Funktionen per AST; dadurch wird
kein Gemini-API-Zugriff und kein Projekt-Import ausgeführt.
"""
import ast
import datetime
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("gemini_auswertung_repariert_final_nachgebessert.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NEEDED = {
    "_normalisiere_quellenbezeichnung", "_quellenwert_toleranz",
    "_quellenwerte_gleich", "_quellenwert_ist_mehrdeutig", "_quellenwert_float", "_normalisiere_datenstand",
    "_datenstand_ist_aktuell", "_pruefe_quellenperioden_und_positionsduplikate",
}
NODES = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NEEDED]


def _portfolio_positionsduplikate(text):
    return []


def _extrahiere_makro_referenzwerte(text):
    return {}

_METAL_VALUES = {"Gold": ("-5,70", -5.70), "Silber": ("-15,60", -15.60),
                 "Platin": ("-9,80", -9.80), "Palladium": ("-15,10", -15.10)}


def _edelmetall_4w_value(text, metal):
    return _METAL_VALUES.get(metal)

def _edelmetall_lage_block(text):
    match = re.search(r"(?ms)^LAGE JE METALL\b.*?^(?=TRENDFOLGE-DIAGNOSE JE METALL\b)", text or "")
    return match.group(0) if match else ""

ENV = {
    "re": re, "datetime": datetime,
    "_portfolio_positionsduplikate": _portfolio_positionsduplikate,
    "_extrahiere_makro_referenzwerte": _extrahiere_makro_referenzwerte,
    "_edelmetall_4w_value": _edelmetall_4w_value,
    "_edelmetall_lage_block": _edelmetall_lage_block,
}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), ENV)

LABELS = ["S&P 500", "Nasdaq Composite", "DAX", "EuroStoxx 50", "Nikkei 225", "Shanghai Composite", "Hang Seng"]
VALUES = [("+0,50", "5.123,45", "+12,30"), ("+0,40", "16.870,00", "+8,20"),
          ("+0,30", "19.123,40", "+11,10"), ("+0,20", "4.987,60", "+9,40"),
          ("-0,10", "38.123,40", "+13,20"), ("+0,60", "3.456,70", "+5,60"),
          ("+0,70", "18.123,40", "+7,80")]


def index_line(label, vals, date="14.10.2026"):
    return f"- {label}: letzter abgeschlossener Handelstag {vals[0]}% ({vals[1]}) | YTD {vals[2]}% | Datenstand {date}"


def fixture():
    indices = "\n".join(index_line(label, vals) for label, vals in zip(LABELS, VALUES))
    output = indices + "\n" + "\n".join([
        "- WTI: Kurs 72,50 | +1,20% 5T | -2,30% 4W | Datenstand 14.10.2026",
        "- Brent: Kurs 76,40 | +1,10% 5T | -1,90% 4W | Datenstand 14.10.2026",
        "- 8.1 Gold: Quelle: Edelmetalle-Briefing | Kurs (Futures GC=F): 4171.40 USD | 4W: -5.70% | EMA200: 4321.96 USD",
        "- 8.2 Silber: Quelle: Edelmetalle-Briefing | Kurs (Futures SI=F): 56.18 USD | 4W: -15.60% | EMA200: 64.62 USD",
        "- 8.3 Platin: Quelle: Edelmetalle-Briefing | Kurs (Futures PL=F): 1615.60 USD | 4W: -9.80% | EMA200: 1700.00 USD",
        "- 8.4 Palladium: Quelle: Edelmetalle-Briefing | Kurs (Futures PA=F): 1109.00 USD | 4W: -15.10% | EMA200: 1200.00 USD",
        "10. 💼 BESTEHENDES PORTFOLIO", "10.1 Offene Positionen", "11. METHODIK / DATENQUALITÄT",
    ])
    briefing = indices + "\n" + "\n".join([
        "- WTI: Kurs 72,50 | +1,20% 5T | -2,30% 4W | Datenstand 14.10.2026",
        "- Brent: Kurs 76,40 | +1,10% 5T | -1,90% 4W | Datenstand 14.10.2026",
    ])
    edel = "\n".join([
        "LAGE JE METALL (52-Wochen-Fenster nach Datum, Basis aller Schwellen):",
        "  Gold: Kurs 4171.40 | -5.7% in den letzten 4 Wochen",
        "  Silber: Kurs 56.18 | -15.6% in den letzten 4 Wochen",
        "  Platin: Kurs 1615.60 | -9.8% in den letzten 4 Wochen",
        "  Palladium: Kurs 1109.00 | -15.1% in den letzten 4 Wochen",
        "TRENDFOLGE-DIAGNOSE JE METALL (GC=F etc. = operative Datenbasis):",
    ])
    return output, briefing, edel


class SourceGateTests(unittest.TestCase):
    def assert_gate_fails(self, text, briefing, phrase, edel=""):
        with self.assertRaisesRegex(RuntimeError, re.escape(phrase)):
            ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel)

    def test_valid_fixture_and_eurostoxx_normalization(self):
        text, briefing, edel = fixture()
        self.assertEqual(ENV["_normalisiere_quellenbezeichnung"]("EuroStoxx 50"), "eurostoxx50")
        self.assertEqual(ENV["_normalisiere_quellenbezeichnung"]("EURO STOXX-50"), "eurostoxx50")
        self.assertTrue(ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel))

    def test_missing_mandatory_index_source_fails(self):
        text, briefing, edel = fixture()
        briefing = "\n".join(line for line in briefing.splitlines() if not line.startswith("- DAX:"))
        self.assert_gate_fails(text, briefing, "7.1 DAX: Pflichtquellwert fehlt", edel)

    def test_missing_output_row_fails(self):
        text, briefing, edel = fixture()
        text = "\n".join(line for line in text.splitlines() if not line.startswith("- Nasdaq Composite:"))
        self.assert_gate_fails(text, briefing, "7.1 Nasdaq Composite: Pflicht-Ausgabezeile fehlt", edel)

    def test_numeric_formatting_is_tolerated(self):
        text, briefing, edel = fixture()
        text = text.replace("(5.123,45)", "(5123.450001)", 1)
        self.assertTrue(ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel))

    def test_real_numeric_mismatch_fails(self):
        text, briefing, edel = fixture()
        text = text.replace("+0,50%", "+0,80%", 1)
        self.assert_gate_fails(text, briefing, "7.1 S&P 500: Tagesrendite weicht", edel)

    def test_stale_output_date_fails(self):
        text, briefing, edel = fixture()
        text = text.replace("Datenstand 14.10.2026", "Datenstand 13.10.2026", 1)
        self.assert_gate_fails(text, briefing, "Datenstand fehlt, ist veraltet", edel)

    def test_invalid_or_empty_source_field_fails(self):
        text, briefing, edel = fixture()
        briefing = briefing.replace("YTD +12,30%", "YTD %", 1)
        self.assert_gate_fails(text, briefing, "Pflichtquellwert fehlt oder ist nicht vollständig parsebar", edel)

    def test_missing_oil_source_fails(self):
        text, briefing, edel = fixture()
        briefing = "\n".join(line for line in briefing.splitlines() if not line.startswith("- WTI:"))
        self.assert_gate_fails(text, briefing, "7.5 WTI: Pflichtquelle fehlt", edel)

    def test_metal_course_mismatch_fails(self):
        text, briefing, edel = fixture()
        text = text.replace("Kurs (Futures GC=F): 4171.40 USD", "Kurs (Futures GC=F): 4172.40 USD")
        self.assert_gate_fails(text, briefing, "8.1 Gold: Kurs stimmt nicht", edel)

    def test_metal_four_week_mismatch_fails(self):
        text, briefing, edel = fixture()
        text = text.replace("4W: -5.70%", "4W: -4.90%", 1)
        self.assert_gate_fails(text, briefing, "8.1 Gold: 4W-Wert stimmt nicht", edel)

    def test_missing_metal_source_course_fails(self):
        text, briefing, edel = fixture()
        edel = edel.replace("Gold: Kurs 4171.40", "Gold: Preis nicht verfügbar")
        self.assert_gate_fails(text, briefing, "8.1 Gold: Kurs-Pflichtwert fehlt", edel)

    def test_precision_tolerance_is_not_global_point_zero_one(self):
        self.assertTrue(ENV["_quellenwerte_gleich"]("16.87", "16.870001"))
        self.assertFalse(ENV["_quellenwerte_gleich"]("16.87", "16.88"))

    def test_invalid_calendar_date_is_rejected(self):
        self.assertIsNone(ENV["_normalisiere_datenstand"]("31.02.2026"))
        self.assertEqual(ENV["_normalisiere_datenstand"]("14.10.2026"), "2026-10-14")


    def test_eurostoxx_alias_is_parsed_before_normalization(self):
        text, briefing, edel = fixture()
        text = text.replace("EuroStoxx 50:", "EURO STOXX-50:")
        briefing = briefing.replace("EuroStoxx 50:", "EURO STOXX-50:")
        self.assertTrue(ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel))

    def test_stale_source_date_fails_even_when_output_matches(self):
        text, briefing, edel = fixture()
        text = text.replace("Datenstand 14.10.2026", "Datenstand 01.10.2026")
        briefing = briefing.replace("Datenstand 14.10.2026", "Datenstand 01.10.2026")
        with self.assertRaisesRegex(RuntimeError, "Quelldatenstand ist veraltet"):
            ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel)

    def test_future_source_date_fails(self):
        text, briefing, edel = fixture()
        text = text.replace("Datenstand 14.10.2026", "Datenstand 16.10.2026")
        briefing = briefing.replace("Datenstand 14.10.2026", "Datenstand 16.10.2026")
        with self.assertRaisesRegex(RuntimeError, "Quelldatenstand ist veraltet oder liegt in der Zukunft"):
            ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel)

    def test_freshness_helper_has_explicit_weekend_holiday_buffer(self):
        today = datetime.date(2026, 10, 15)
        self.assertTrue(ENV["_datenstand_ist_aktuell"]("14.10.2026", heute=today))
        self.assertTrue(ENV["_datenstand_ist_aktuell"]("10.10.2026", heute=today))
        self.assertFalse(ENV["_datenstand_ist_aktuell"]("09.10.2026", heute=today))
        self.assertFalse(ENV["_datenstand_ist_aktuell"]("16.10.2026", heute=today))


    def test_ambiguous_single_separator_three_digits_is_rejected(self):
        self.assertTrue(ENV["_quellenwert_ist_mehrdeutig"]("1.234"))
        self.assertTrue(ENV["_quellenwert_ist_mehrdeutig"]("1,234"))
        self.assertFalse(ENV["_quellenwert_ist_mehrdeutig"]("1.234,56"))
        self.assertFalse(ENV["_quellenwerte_gleich"]("1.234", "1234"))
        self.assertFalse(ENV["_quellenwerte_gleich"]("1,234", "1.234"))


    def test_stale_oil_source_date_fails_even_when_output_matches(self):
        text, briefing, edel = fixture()
        text = text.replace("Datenstand 14.10.2026", "Datenstand 01.10.2026")
        briefing = briefing.replace("Datenstand 14.10.2026", "Datenstand 01.10.2026")
        with self.assertRaisesRegex(RuntimeError, "7.5 WTI: Quelldatenstand ist veraltet"):
            ENV["_pruefe_quellenperioden_und_positionsduplikate"](text, briefing, "", edel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
