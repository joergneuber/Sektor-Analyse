"""Isolierte Regressionstests für das Quellen-/Perioden-Abschluss-Gate.

Die Tests laden ausschließlich die geprüften Funktionen per AST; dadurch wird
kein Gemini-API-Zugriff und kein Projekt-Import ausgeführt.
"""
import ast
import datetime
import re
import unittest
from pathlib import Path

# Im Repository liegt der Produktivcode im Projektstamm; beim isolierten
# lokalen Testen darf alternativ die separat benannte Reparaturdatei daneben liegen.
SOURCE = Path(__file__).resolve().parent.parent / "gemini_auswertung.py"
if not SOURCE.exists():
    SOURCE = Path(__file__).with_name("gemini_auswertung_strukturfix.py")
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NEEDED = {
    "_normalisiere_quellenbezeichnung", "_quellenwert_toleranz",
    "_quellenwerte_gleich", "_quellenwert_ist_mehrdeutig", "_quellenwert_float", "_normalisiere_datenstand",
    "_datenstand_ist_aktuell", "_pruefe_quellenperioden_und_positionsduplikate",
    "_repariere_quellenperioden_und_positionsduplikate",
    "_repariere_doppelte_pflichtunterabschnitte", "_kanonische_unterabschnittstitel",
    "_echte_unterabschnittsueberschrift", "_ist_fallback_pflichttext",
    "_ausgabe_heading_key", "_bereinige_doppelte_ausgabestruktur",
    "_repariere_doppelte_3_3", "_pruefe_finale_struktur_und_konsistenz",
}
NODES = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NEEDED]


def _portfolio_positionsduplikate(text):
    return []


def _dedupliziere_portfolio_positionsdatensaetze(text):
    return text, []


def _edelmetall_lage_zeile(text, metal):
    return None


def _edelmetall_quellenblock(text, metal):
    return ""


def _extrahiere_200er_trend(text):
    return None


def _extrahiere_makro_referenzwerte(text):
    return {}

_METAL_VALUES = {"Gold": ("-5,70", -5.70), "Silber": ("-15,60", -15.60),
                 "Platin": ("-9,80", -9.80), "Palladium": ("-15,10", -15.10)}


def _edelmetall_4w_value(text, metal):
    return _METAL_VALUES.get(metal)

def _edelmetall_lage_block(text):
    match = re.search(r"(?ms)^LAGE JE METALL\b.*?^(?=TRENDFOLGE-DIAGNOSE JE METALL\b)", text or "")
    return match.group(0) if match else ""

def _abschnitt_text(text, start, ends):
    return ""

ENV = {
    "re": re, "datetime": datetime,
    "_portfolio_positionsduplikate": _portfolio_positionsduplikate,
    "_dedupliziere_portfolio_positionsdatensaetze": _dedupliziere_portfolio_positionsdatensaetze,
    "_edelmetall_lage_zeile": _edelmetall_lage_zeile,
    "_edelmetall_quellenblock": _edelmetall_quellenblock,
    "_extrahiere_200er_trend": _extrahiere_200er_trend,
    "EDELMETALL_FAKTENQUELLE": "Edelmetalle-Briefing",
    "_extrahiere_makro_referenzwerte": _extrahiere_makro_referenzwerte,
    "_edelmetall_4w_value": _edelmetall_4w_value,
    "_edelmetall_lage_block": _edelmetall_lage_block,
    "_abschnitt_text": _abschnitt_text,
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


def section_block(text, start_number, end_number):
    pattern = (
        r"(?ims)^(?:\s*#{1,6}\s*)?" + re.escape(start_number) +
        r"\b[^\n]*\n.*?(?=^(?:\s*#{1,6}\s*)?" + re.escape(end_number) + r"\b[^\n]*$)"
    )
    match = re.search(pattern, text)
    return match.group(0) if match else None


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


    def test_repair_rebuilds_indices_under_markdown_headings_and_iso_dates(self):
        _, briefing, edel = fixture()
        briefing = briefing.replace("14.10.2026", "2026-10-14")
        index_rows = "\n".join(
            line for line in briefing.splitlines()
            if any(line.startswith("- " + label + ":") for label in LABELS)
        )
        text = (
            "### 7.1 Aktienmärkte / Indizes\n- alte, nicht kanonische Indexübersicht\n"
            "### 7.2 Zinsen: Überblick\n- Zinsdaten\n"
            "### 7.5 Rohstoffe\n- alte Ölzeile\n"
            "### 7.6 Krypto\n- Kryptozeile\n"
            + "\n".join([
                "- 8.1 Gold: Quelle: Edelmetalle-Briefing | Kurs (Futures GC=F): 4171.40 USD | 4W: -5.70% | EMA200: 4321.96 USD",
                "- 8.2 Silber: Quelle: Edelmetalle-Briefing | Kurs (Futures SI=F): 56.18 USD | 4W: -15.60% | EMA200: 64.62 USD",
                "- 8.3 Platin: Quelle: Edelmetalle-Briefing | Kurs (Futures PL=F): 1615.60 USD | 4W: -9.80% | EMA200: 1700.00 USD",
                "- 8.4 Palladium: Quelle: Edelmetalle-Briefing | Kurs (Futures PA=F): 1109.00 USD | 4W: -15.10% | EMA200: 1200.00 USD",
                "10. 💼 BESTEHENDES PORTFOLIO", "10.1 Offene Positionen", "11. METHODIK / DATENQUALITÄT",
            ])
        )
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](text, briefing, "", edel)
        self.assertIn("7.1 Kurzüberblick aus vollständigen Tagesrendite-/YTD-Quellpaaren neu aufgebaut", changes)
        self.assertIn("- EuroStoxx 50: letzter abgeschlossener Handelstag", fixed)
        self.assertIn("- WTI: Kurs 72,50 | +1,20% 5T | -2,30% 4W | Datenstand 2026-10-14", fixed)
        self.assertIn("- BRENT: Kurs 76,40 | +1,10% 5T | -1,90% 4W | Datenstand 2026-10-14", fixed)
        self.assertTrue(ENV["_pruefe_quellenperioden_und_positionsduplikate"](fixed, briefing, "", edel))

    def test_repair_handles_nasdaq_composite_and_eurostoxx_aliases(self):
        _, briefing, edel = fixture()
        briefing = briefing.replace("Nasdaq Composite:", "Nasdaq:").replace("EuroStoxx 50:", "EURO STOXX-50:")
        text = (
            "7.1 Aktienmärkte / Indizes\n- alte Zeilen\n"
            "7.2 Zinsen\n- Zinsen\n"
            "7.5 Rohstoffe\n- alte Ölzeilen\n"
            "7.6 Krypto\n- Krypto\n"
            + "\n".join([
                "- 8.1 Gold: Quelle: Edelmetalle-Briefing | Kurs (Futures GC=F): 4171.40 USD | 4W: -5.70% | EMA200: 4321.96 USD",
                "- 8.2 Silber: Quelle: Edelmetalle-Briefing | Kurs (Futures SI=F): 56.18 USD | 4W: -15.60% | EMA200: 1700.00 USD",
                "- 8.3 Platin: Quelle: Edelmetalle-Briefing | Kurs (Futures PL=F): 1615.60 USD | 4W: -9.80% | EMA200: 1700.00 USD",
                "- 8.4 Palladium: Quelle: Edelmetalle-Briefing | Kurs (Futures PA=F): 1109.00 USD | 4W: -15.10% | EMA200: 1200.00 USD",
                "10. 💼 BESTEHENDES PORTFOLIO", "10.1 Offene Positionen", "11. METHODIK / DATENQUALITÄT",
            ])
        )
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](text, briefing, "", edel)
        self.assertIn("- Nasdaq Composite: letzter abgeschlossener Handelstag", fixed)
        self.assertIn("- EuroStoxx 50: letzter abgeschlossener Handelstag", fixed)
        self.assertIn("- WTI: Kurs 72,50 | +1,20% 5T | -2,30% 4W | Datenstand 14.10.2026", fixed)


    def test_incomplete_index_source_preserves_existing_index_section(self):
        _, briefing, _ = fixture()
        briefing = "\n".join(line for line in briefing.splitlines() if not line.startswith("- DAX:"))
        original_rows = "\n".join(index_line(label, vals) for label, vals in zip(LABELS, VALUES))
        text = (
            "7.1 Aktienmärkte / Indizes\n" + original_rows + "\n"
            "7.2 Zinsen\n- Zinsdaten\n"
            "7.5 Rohstoffe\n- bestehende WTI-Zeile bleibt\n- bestehende Brent-Zeile bleibt\n"
            "7.6 Krypto\n- Krypto\n"
        )
        before_section = section_block(text, "7.1", "7.2")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.1", "7.2"), before_section)
        self.assertTrue(any("7.1 Index-Reparatur übersprungen; bestehende Ausgabe unverändert gelassen" in item for item in changes))
        self.assertIn("- DAX: letzter abgeschlossener Handelstag", fixed)
        self.assertIn("- EuroStoxx 50: letzter abgeschlossener Handelstag", fixed)
        self.assertNotIn("Kurzüberblick aus vollständigen", " ".join(changes))

    def test_unrecognized_index_source_line_preserves_existing_section(self):
        _, briefing, _ = fixture()
        briefing = briefing.replace("- DAX:", "- Dax-Index:", 1)
        original_rows = "\n".join(index_line(label, vals) for label, vals in zip(LABELS, VALUES))
        text = (
            "7.1 Aktienmärkte / Indizes\n" + original_rows + "\n"
            "7.2 Zinsen\n- Zinsdaten\n7.5 Rohstoffe\n- Öl bleibt\n7.6 Krypto\n"
        )
        before_section = section_block(text, "7.1", "7.2")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.1", "7.2"), before_section)
        self.assertIn("- DAX: letzter abgeschlossener Handelstag", fixed)
        self.assertTrue(any("7.1 Index-Reparatur übersprungen" in item for item in changes))

    def test_incomplete_index_source_field_preserves_existing_section(self):
        _, briefing, _ = fixture()
        briefing = briefing.replace("YTD +11,10%", "YTD %", 1)
        original_rows = "\n".join(index_line(label, vals) for label, vals in zip(LABELS, VALUES))
        text = (
            "7.1 Aktienmärkte / Indizes\n" + original_rows + "\n"
            "7.2 Zinsen\n- Zinsdaten\n7.5 Rohstoffe\n- Öl bleibt\n7.6 Krypto\n"
        )
        before_section = section_block(text, "7.1", "7.2")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.1", "7.2"), before_section)
        self.assertIn("- DAX: letzter abgeschlossener Handelstag", fixed)
        self.assertTrue(any("7.1 Index-Reparatur übersprungen" in item for item in changes))

    def test_missing_brent_source_preserves_both_existing_oil_rows(self):
        _, briefing, _ = fixture()
        briefing = "\n".join(line for line in briefing.splitlines() if not line.startswith("- Brent:"))
        text = (
            "7.1 Aktienmärkte / Indizes\n- Indextext\n7.2 Zinsen\n- Zinsdaten\n"
            "7.5 Rohstoffe\n- WTI: bestehende WTI-Zeile\n- Brent: bestehende Brent-Zeile\n"
            "7.6 Krypto\n- Krypto\n"
        )
        before_section = section_block(text, "7.5", "7.6")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.5", "7.6"), before_section)
        self.assertIn("- WTI: bestehende WTI-Zeile", fixed)
        self.assertIn("- Brent: bestehende Brent-Zeile", fixed)
        self.assertTrue(any("7.5 Öl-Reparatur übersprungen" in item for item in changes))

    def test_duplicate_wti_source_preserves_existing_oil_rows(self):
        _, briefing, _ = fixture()
        wti = next(line for line in briefing.splitlines() if line.startswith("- WTI:"))
        briefing = briefing + "\n" + wti
        text = (
            "7.1 Aktienmärkte / Indizes\n- Indextext\n7.2 Zinsen\n- Zinsdaten\n"
            "7.5 Rohstoffe\n- WTI: bestehende WTI-Zeile\n- Brent: bestehende Brent-Zeile\n"
            "7.6 Krypto\n- Krypto\n"
        )
        before_section = section_block(text, "7.5", "7.6")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.5", "7.6"), before_section)
        self.assertIn("- WTI: bestehende WTI-Zeile", fixed)
        self.assertIn("- Brent: bestehende Brent-Zeile", fixed)
        self.assertTrue(any("7.5 Öl-Reparatur übersprungen" in item for item in changes))


    def test_duplicate_index_source_preserves_existing_index_section(self):
        _, briefing, _ = fixture()
        sp500 = next(line for line in briefing.splitlines() if line.startswith("- S&P 500:"))
        briefing = briefing + "\n" + sp500
        original_rows = "\n".join(index_line(label, vals) for label, vals in zip(LABELS, VALUES))
        text = (
            "7.1 Aktienmärkte / Indizes\n" + original_rows + "\n"
            "7.2 Zinsen\n- Zinsdaten\n7.5 Rohstoffe\n- Öl bleibt\n7.6 Krypto\n"
        )
        before_section = section_block(text, "7.1", "7.2")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.1", "7.2"), before_section)
        self.assertIn("- S&P 500: letzter abgeschlossener Handelstag", fixed)
        self.assertTrue(any("7.1 Index-Reparatur übersprungen" in item for item in changes))

    def test_malformed_duplicate_wti_source_preserves_existing_oil_rows(self):
        _, briefing, _ = fixture()
        briefing = briefing + "\n- WTI: Kurs unbekannt | 5T unbekannt | 4W unbekannt"
        text = (
            "7.1 Aktienmärkte / Indizes\n- Indextext\n7.2 Zinsen\n- Zinsdaten\n"
            "7.5 Rohstoffe\n- WTI: bestehende WTI-Zeile\n- Brent: bestehende Brent-Zeile\n"
            "7.6 Krypto\n- Krypto\n"
        )
        before_section = section_block(text, "7.5", "7.6")
        fixed, changes = ENV["_repariere_quellenperioden_und_positionsduplikate"](
            text, briefing, "", ""
        )
        self.assertEqual(section_block(fixed, "7.5", "7.6"), before_section)
        self.assertIn("- WTI: bestehende WTI-Zeile", fixed)
        self.assertIn("- Brent: bestehende Brent-Zeile", fixed)
        self.assertTrue(any("7.5 Öl-Reparatur übersprungen" in item for item in changes))


class FinalStructureDuplicateTests(unittest.TestCase):
    def test_real_section_is_preserved_and_fallback_duplicate_removed(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6. ⚠️ WIDERSPRÜCHE & RISIKEN\n"
            "6.1 Makro vs. Technik\n"
            "Originalanalyse: Makrotrend und technische Lage widersprechen sich konkret.\n"
            "Prüfpunkt: Bestätigung durch den nächsten Datenstand.\n"
            "6.2 Technik gegen Fundamentaldaten\n"
            "Vorhandener Abschnitt.\n"
            "6.1 Makro gegen Technik\n"
            "Keine belastbare Gegenüberstellung von Makro- und Techniksignalen möglich, "
            "weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren "
            "Angaben in den vorliegenden Projektquellen vorliegen.\n"
            "6.2 Technik gegen Fundamentaldaten\n"
            "Vorhandener Abschnitt.\n"
        )
        result = repair(text)
        self.assertIn("Originalanalyse: Makrotrend", result)
        self.assertEqual(len(re.findall(r"(?m)^\s*6\.1\s+", result)), 1)
        self.assertIn("6.1 Makro vs. Technik", result)
        self.assertNotIn("Keine belastbare Gegenüberstellung", result)
        self.assertEqual(repair(result), result)

    def test_duplicate_fallbacks_collapse_to_one(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        fallback = "Keine verifizierten Makrotermine aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine, Daten oder Ereignisse erfunden."
        text = (
            "9.1 Makrotermine\n" + fallback + "\n"
            "9.1 Makrotermine (Kalender)\n" + fallback + "\n"
            "9.2 Unternehmen\nKeine verifizierten Unternehmensveranstaltungen verfügbar.\n"
        )
        result = repair(text)
        self.assertEqual(len(re.findall(r"(?m)^\s*9\.1\s+", result)), 1)
        self.assertIn("9.1 Makrotermine", result)
        self.assertEqual(repair(result), result)

    def test_two_different_real_sections_fail_closed(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6.1 Makro gegen Technik\n"
            "Erste substanzielle Analyse mit eigenen Prüfpunkten.\n"
            "6.2 Technik gegen Fundamentaldaten\n"
            "Zweite substanzielle Analyse mit anderen Prüfpunkten.\n"
            "6.1 Makro vs Technik\n"
            "Eine andere substanzielle Analyse, die nicht überschrieben werden darf.\n"
            "6.3 Sektor gegen Aktie\n"
            "Sektorprüfung.\n"
        )
        with self.assertRaisesRegex(RuntimeError, "DOPPELTE_PFLICHTUNTERABSCHNITTE_ECHTE_INHALTE: 6.1"):
            repair(text)

    def test_duplicate_cleanup_keeps_all_distinct_subsections(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6.1 Makro gegen Technik\nAnalyse A.\n"
            "6.2 Technik gegen Fundamentaldaten\nAnalyse B.\n"
            "6.3 Sektor gegen Aktie\nAnalyse C.\n"
            "9.1 Makrotermine\nKeine verifizierten Makrotermine aus den vorliegenden Projektquellen verfügbar.\n"
            "9.2 Unternehmen\nKeine verifizierten Unternehmensveranstaltungen oder Unternehmensmeldungen aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine erfunden.\n"
        )
        result = repair(text)
        for key in ("6.1", "6.2", "6.3", "9.1", "9.2"):
            self.assertEqual(len(re.findall(r"(?m)^\s*" + re.escape(key) + r"\s+", result)), 1)
        self.assertEqual(repair(result), result)


    def test_heading_variants_are_recognized_before_fallback_insertion(self):
        heading_key = ENV["_ausgabe_heading_key"]
        required = [
            "6.1 Makro gegen Technik",
            "6.2 Technik gegen Fundamentaldaten",
            "6.3 Sektor gegen Aktie",
            "6.4 Rohstoff gegen Aktie",
            "6.5 Investmentthese gegen aktuelle Marktdaten",
            "6.6 Risiken bestehender Ideen",
            "9.1 Makrotermine",
            "9.2 Unternehmen",
            "9.3 Branchenereignisse",
            "9.4 Technische Trigger",
            "9.5 Mögliche Aktivierung / Invalidierung",
        ]
        variants = [
            ("6.1 Makro vs. Technik — zusätzlicher Kontext", "6.1"),
            ("6.2 Technik vs Fundamentaldaten", "6.2"),
            ("6.3 Sektor vs. Aktie", "6.3"),
            ("6.4 Rohstoff gegen Aktie", "6.4"),
            ("6.5 Investmentthese gegen aktuelle Daten", "6.5"),
            ("6.6 Risiken der bestehenden Ideen", "6.6"),
            ("9.1 Makrotermine (Fed/EZB)", "9.1"),
            ("9.2 Unternehmen / Earnings", "9.2"),
            ("9.3 Branchenereignisse (Konferenzen)", "9.3"),
            ("9.4 Technische Trigger und Auslöser", "9.4"),
            ("9.5 Aktivierung / Invalidierung", "9.5"),
        ]
        for line, expected_key in variants:
            with self.subTest(line=line):
                match = heading_key(line, required)
                self.assertIsNotNone(match)
                self.assertEqual(match[0], expected_key)

    def test_all_eleven_logged_duplicate_numbers_are_repaired_before_final_gate(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        final_gate = ENV["_pruefe_finale_struktur_und_konsistenz"]
        canonical = {
            "6.1": ("6.1 Makro gegen Technik", "Keine belastbare Gegenüberstellung von Makro- und Techniksignalen möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen."),
            "6.2": ("6.2 Technik gegen Fundamentaldaten", "Keine belastbare Gegenüberstellung von Technik und Fundamentaldaten möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen."),
            "6.3": ("6.3 Sektor gegen Aktie", "Keine belastbare Gegenüberstellung von Sektor und einzelner Aktie möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen."),
            "6.4": ("6.4 Rohstoff gegen Aktie", "Keine belastbare Gegenüberstellung von Rohstofftreibern und einzelner Aktie möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen."),
            "6.5": ("6.5 Investmentthese gegen aktuelle Marktdaten", "Keine belastbare Prüfung der Investmentthese gegen aktuelle Marktdaten möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen."),
            "6.6": ("6.6 Risiken bestehender Ideen", "Keine belastbare Risikobewertung bestehender Ideen möglich, weil für diesen Abschnitt keine verifizierbaren, konkret zuordenbaren Angaben in den vorliegenden Projektquellen vorliegen."),
             "9.1": ("9.1 Makrotermine", "Keine verifizierten Makrotermine aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine, Daten oder Ereignisse erfunden."),
            "9.2": ("9.2 Unternehmen", "Keine verifizierten Unternehmensveranstaltungen oder Unternehmensmeldungen aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine erfunden."),
            "9.3": ("9.3 Branchenereignisse", "Keine verifizierten Branchenereignisse aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Termine oder Ereignisse erfunden."),
            "9.4": ("9.4 Technische Trigger", "Keine verifizierbaren technischen Trigger aus den vorliegenden Projektquellen verfügbar; es werden deshalb keine Kursmarken oder Signale erfunden."),
            "9.5": ("9.5 Mögliche Aktivierung / Invalidierung", "Keine belastbaren Aktivierungs- oder Invalidierungsbedingungen aus den vorliegenden Projektquellen ableitbar; es werden deshalb keine Trigger erfunden."),
        }
        chunks = []
        for key, (heading, _) in canonical.items():
            chunks.append(f"{heading}\nEchte Analyse für {key}: nachvollziehbare Aussage und Prüfpunkt.\n")
        for key, (heading, fallback) in canonical.items():
            # Typische Schreibvariante plus deterministischer Fallback.
            variant = heading + " (Pflichtabschnitt)"
            chunks.append(f"{variant}\n{fallback}\n")
        result = repair("\n".join(chunks))
        for key in canonical:
            self.assertEqual(len(re.findall(r"(?m)^\s*" + re.escape(key) + r"\s+", result)), 1)
        self.assertIn("Echte Analyse für 6.1", result)
        self.assertIn("Echte Analyse für 9.5", result)
        final_gate(result)

    def test_known_fallback_duplicate_is_removed_by_regular_structure_cleanup(self):
        cleanup = ENV["_bereinige_doppelte_ausgabestruktur"]
        required = [
            "6.1 Makro gegen Technik",
            "6.2 Technik gegen Fundamentaldaten",
            "6.3 Sektor gegen Aktie",
            "6.4 Rohstoff gegen Aktie",
            "6.5 Investmentthese gegen aktuelle Marktdaten",
            "6.6 Risiken bestehender Ideen",
            "9.1 Makrotermine",
            "9.2 Unternehmen",
            "9.3 Branchenereignisse",
            "9.4 Technische Trigger",
            "9.5 Mögliche Aktivierung / Invalidierung",
        ]
        text = (
            "6.1 Makro vs. Technik — aktuelle Lage\n"
            "Substanzielle Originalanalyse bleibt erhalten.\n"
            "6.1 Makro gegen Technik\n"
            "Keine belastbare Gegenüberstellung von Makro- und Techniksignalen möglich, weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben in den vorliegenden Projektquellen vorliegen.\n"
        )
        result = cleanup(text, required)
        self.assertEqual(len(re.findall(r"(?m)^\s*6\.1\s+", result)), 1)
        self.assertIn("Substanzielle Originalanalyse bleibt erhalten.", result)
        self.assertNotIn("Keine belastbare Gegenüberstellung", result)



class StructureIdempotencyAndSafetyTests(unittest.TestCase):
    def test_numbered_prose_is_not_treated_as_heading_or_boundary(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        is_heading = ENV["_echte_unterabschnittsueberschrift"]
        prose = "6.1 Das bedeutet im konkreten Fall, dass die Kennzahl weiter beobachtet werden sollte"
        self.assertIsNone(is_heading(prose))
        text = (
            "6.1 Makro gegen Technik\n"
            "Substanzielle Originalanalyse bleibt bestehen.\n"
            + prose + "\n"
            "Die Fließtextzeile gehört zum Analyseblock.\n"
            "6.1 Makro vs. Technik — aktuelle Lage\n"
            "Keine belastbare Gegenüberstellung von Makro- und Techniksignalen ist möglich, "
            "da aktuelle Quellenwerte für diesen Abgleich fehlen.\n"
        )
        result = repair(text)
        self.assertEqual(len(re.findall(r"(?m)^6\.1\s+(?:Makro gegen Technik|Makro vs\.)", result)), 1)
        self.assertIn(prose, result)
        self.assertIn("Die Fließtextzeile gehört zum Analyseblock.", result)

    def test_formulation_variant_of_known_fallback_is_removed(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6.1 Makro gegen Technik\n"
            "Substanzielle Analyse: Der Makrotrend und die Technik widersprechen sich.\n"
            "6.1 Makro vs. Technik – zusätzliche Erläuterung\n"
            "Eine belastbare Gegenüberstellung von Makro- und Techniksignalen ist nicht möglich, "
            "weil die vorliegenden Projektquellen keine gemeinsam auswertbaren Angaben enthalten.\n"
        )
        result = repair(text)
        self.assertIn("Substanzielle Analyse:", result)
        self.assertNotIn("Eine belastbare Gegenüberstellung", result)
        self.assertEqual(len(re.findall(r"(?m)^6\.1\s+", result)), 1)

    def test_existing_professional_heading_is_never_rewritten(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6.1 Makro vs. Technik — aktueller Datenabgleich\n"
            "Analyseinhalt mit überprüfbaren Details.\n"
            "6.2 Technik gegen Fundamentaldaten\n"
            "Anderer Inhalt.\n"
        )
        result = repair(text)
        self.assertIn("6.1 Makro vs. Technik — aktueller Datenabgleich", result)
        self.assertEqual(result, text)

    def test_repair_is_idempotent_over_repeated_runs(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6.1 Makro gegen Technik\n"
            "Substanzielle Originalanalyse bleibt bestehen.\n"
            "6.1 Makro gegen Technik (Pflichtabschnitt)\n"
            "Keine belastbare Gegenüberstellung von Makro- und Techniksignalen möglich, "
            "weil für diesen Abschnitt keine verifizierbaren, gemeinsam auswertbaren Angaben "
            "in den vorliegenden Projektquellen vorliegen.\n"
            "9.1 Makrotermine\n"
            "Keine verifizierten Makrotermine aus den vorliegenden Projektquellen verfügbar; "
            "es werden deshalb keine Termine, Daten oder Ereignisse erfunden.\n"
            "9.1 Makrotermine (Kalender)\n"
            "Es liegen derzeit keine bestätigten Makrotermine aus den verfügbaren Quellen vor.\n"
        )
        once = repair(text)
        twice = repair(once)
        thrice = repair(twice)
        self.assertEqual(once, twice)
        self.assertEqual(twice, thrice)
        self.assertIn("Substanzielle Originalanalyse bleibt bestehen.", thrice)
        self.assertEqual(len(re.findall(r"(?m)^6\.1\s+", thrice)), 1)
        self.assertEqual(len(re.findall(r"(?m)^9\.1\s+", thrice)), 1)

    def test_title_shaped_numbered_prose_is_not_misclassified_as_heading(self):
        is_heading = ENV["_echte_unterabschnittsueberschrift"]
        heading_key = ENV["_ausgabe_heading_key"]
        final_gate = ENV["_pruefe_finale_struktur_und_konsistenz"]
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        required = [
            "6.1 Makro gegen Technik",
            "6.2 Technik gegen Fundamentaldaten",
            "6.3 Sektor gegen Aktie",
            "6.4 Rohstoff gegen Aktie",
            "6.5 Investmentthese gegen aktuelle Marktdaten",
            "6.6 Risiken bestehender Ideen",
            "9.1 Makrotermine",
            "9.2 Unternehmen",
            "9.3 Branchenereignisse",
            "9.4 Technische Trigger",
            "9.5 Mögliche Aktivierung / Invalidierung",
        ]
        prose_lines = [
            "6.1 Makro gegen Technik ist derzeit uneindeutig",
            "9.1 Makrotermine wurden bisher nicht bestätigt",
            "6.2 Technik gegen Fundamentaldaten bleiben widersprüchlich",
            "6.1 Makro gegen Technik zeigt im aktuellen Lauf keine klare Richtung",
        ]
        for line in prose_lines:
            with self.subTest(line=line):
                self.assertIsNone(is_heading(line))
                self.assertIsNone(heading_key(line, required))
        text = (
            "6.1 Makro gegen Technik\\n"
            "Originalanalyse bleibt bestehen.\\n"
            + prose_lines[0] + "\\n"
            + prose_lines[1] + "\\n"
            + "Weiterer Fließtext bleibt im selben Abschnitt.\\n"
        )
        result = repair(text)
        self.assertIn(prose_lines[0], result)
        self.assertIn(prose_lines[1], result)
        final_gate(result)

    def test_legitimate_heading_extensions_remain_supported(self):
        is_heading = ENV["_echte_unterabschnittsueberschrift"]
        cases = [
            ("6.1 Makro vs. Technik — zusätzlicher Kontext", "6.1"),
            ("6.5 Investmentthese gegen aktuelle Daten", "6.5"),
            ("6.6 Risiken der bestehenden Ideen", "6.6"),
            ("9.2 Unternehmen / Earnings", "9.2"),
            ("9.4 Technische Trigger und Auslöser", "9.4"),
            ("9.5 Mögliche Aktivierung / Invalidierung", "9.5"),
        ]
        for line, key in cases:
            with self.subTest(line=line):
                self.assertEqual(is_heading(line)[0], key)

    def test_final_gate_ignores_numbered_prose_but_rejects_real_duplicate_headings(self):
        final_gate = ENV["_pruefe_finale_struktur_und_konsistenz"]
        prose = (
            "6.1 Makro gegen Technik\n"
            "Analyseinhalt.\n"
            "6.1 Das bedeutet im konkreten Fall, dass der Wert weiter beobachtet werden muss.\n"
            "Weiterer Fließtext.\n"
        )
        final_gate(prose)
        duplicate = (
            "6.1 Makro gegen Technik\nAnalyse A.\n"
            "6.1 Makro vs. Technik – weiterer Abgleich\nAnalyse B.\n"
        )
        with self.assertRaisesRegex(RuntimeError, "Doppelte Unterabschnittsnummer"):
            final_gate(duplicate)

    def test_real_duplicate_content_fails_closed_without_mutation(self):
        repair = ENV["_repariere_doppelte_pflichtunterabschnitte"]
        text = (
            "6.1 Makro gegen Technik\nAnalyse A mit eigenständiger fachlicher Bewertung.\n"
            "6.1 Makro vs. Technik – zweiter Abgleich\nAnalyse B mit anderer fachlicher Bewertung.\n"
        )
        original = text
        with self.assertRaisesRegex(RuntimeError, "DOPPELTE_PFLICHTUNTERABSCHNITTE_ECHTE_INHALTE"):
            repair(text)
        self.assertEqual(text, original)



if __name__ == "__main__":
    unittest.main(verbosity=2)
