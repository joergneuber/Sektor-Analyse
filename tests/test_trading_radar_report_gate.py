"""Focused regression coverage for the M-05..M-10 report contract."""
import importlib.util
import hashlib
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "trading_radar_report_gate.py"
spec = importlib.util.spec_from_file_location("radar_report_gate", MODULE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def report_fixture(*, quantity_line="", tab2_count=2, ohlcv_warning=True):
    tab2_lines = [
        f"TAB-2-ANZAHL={tab2_count}",
        "TAB-2-QUELLE=Geschlossene Positionen",
        "Ticker: AAA | Status: Gestoppt",
        "Ticker: BBB | Status: Verkauft",
    ]
    tab2_digest = hashlib.sha256("\n".join(tab2_lines).encode("utf-8")).hexdigest()
    sections = [
        "1. Trading-Radar\nBasis und Lagebild mit ausführlicher Markteinordnung und nachvollziehbarem Kontext.",
        "2. Thesen-Update\nThesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion unverändert.",
        "3. Ereignisradar\nUhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
        "4. Assetklassenvergleich\nVergleich von Assetklassen und Marktregimen.",
        "5. Technische Kandidaten\nTicker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; Regelstatus: valide.",
        "6. Portfolio & Risiko\nGeschlossene Positionen: Tab 2 | TAB-2-ANZAHL=2 | Stop-/TP-Bestand: dokumentiert | realisierte Ergebnisse: Performance vorhanden | Konzentration und Portfolio-Risiko bewertet. " + quantity_line,
        "7. MSCI-Vergleich\nPortfolio-Gesamtperformance nicht verfügbar; Ø Positionsperformance nicht verfügbar (getrennte Kennzahlen).",
        "8. Daten-/Prüfanhang\n" + ("WARNUNG-HIST-OHLCV: missing Daten; OHLCV fehlend: Scanner/Kandidaten technisch eingeschränkt." if ohlcv_warning else "Daten geprüft und dokumentiert, keine besonderen Datenqualitätsprobleme erkannt."),
        "10.5 Geschlossene Positionen (Quelle: Tab 2 / Geschlossene Positionen)\n" + "\n".join(tab2_lines).replace(f"TAB-2-ANZAHL={tab2_count}", f"TAB-2-ANZAHL={tab2_count}") + f"\nTAB-2-EXPORT-SHA256={tab2_digest}",
        "TRADING_RADAR_FACHLICH_FINAL=FINAL",
    ]
    return "\n\n".join(sections) + "\n"


class ReportGateTests(unittest.TestCase):
    def test_m05_accepts_report_without_quantities_or_fees(self):
        self.assertEqual(gate.validate_report(report_fixture()), [])

    def test_m05_rejects_tab2_count_mismatch(self):
        errors = gate.validate_report(report_fixture(tab2_count=3))
        self.assertTrue(any("Tab-2-Zählung widerspricht" in e for e in errors), errors)

    def test_m06_requires_separate_portfolio_performance_and_position_average(self):
        text = report_fixture().replace("Portfolio-Gesamtperformance nicht verfügbar; Ø Positionsperformance nicht verfügbar (getrennte Kennzahlen).", "Keine Performancewerte vorhanden.")
        errors = gate.validate_report(text)
        self.assertTrue(any("M-06" in e for e in errors), errors)

    def test_m07_requires_versioned_thesis(self):
        text = report_fixture().replace("Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion unverändert.", "Keine Theseninformationen dokumentiert.")
        errors = gate.validate_report(text)
        self.assertTrue(any("M-07" in e for e in errors), errors)

    def test_m07_accepts_explicit_no_thesis_update_without_fabricated_id(self):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion unverändert.",
            "Keine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.",
        )
        self.assertFalse(any("M-07" in e for e in gate.validate_report(text)))

    def test_m08_requires_event_detail(self):
        text = report_fixture().replace("Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.", "Ein wichtiges makroökonomisches Ereignis wurde angekündigt und wird beobachtet.")
        errors = gate.validate_report(text)
        self.assertTrue(any("M-08" in e for e in errors), errors)


    def test_m08_validates_each_event_record_independently(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "- CPI Veröffentlichung | Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.\n"
            "- Arbeitsmarktdaten | Uhrzeit: TBD | Zeitzone: ET | Kennzahl: NFP | Berichtszeitraum: Oktober 2026 | Konsens: 150000.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-08: Ereignisdatensatz 2" in e for e in errors), errors)

    def test_m08_accepts_separate_complete_event_records(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "- CPI Veröffentlichung | Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.\n"
            "- Arbeitsmarktdaten | Uhrzeit: 08:30 | Zeitzone: ET | Kennzahl: NFP | Berichtszeitraum: Oktober 2026 | letzter Wert: 140000 | Konsens: 150000.",
        )
        self.assertFalse(any("M-08" in e for e in gate.validate_report(text)))

    def test_m09_requires_actionable_candidate_fields(self):
        text = report_fixture().replace("JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; Regelstatus: valide.", "Ein technischer Kandidat ist vorhanden, aber ohne konkrete Handlungsfelder.")
        errors = gate.validate_report(text)
        self.assertTrue(any("M-09" in e for e in errors), errors)

    def test_m10_requires_ohlcv_warning_and_candidate_impact(self):
        errors = gate.validate_report(report_fixture(ohlcv_warning=False))
        self.assertTrue(any("M-10" in e for e in errors), errors)


    def test_m06_rejects_placeholder_performance_values(self):
        text = report_fixture().replace(
            "Portfolio-Gesamtperformance nicht verfügbar; Ø Positionsperformance nicht verfügbar (getrennte Kennzahlen).",
            "Portfolio-Gesamtperformance: TBD; Ø Positionsperformance: Platzhalter.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-06" in e for e in errors), errors)

    def test_m07_rejects_empty_identity_and_invalid_status(self):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion unverändert.",
            "Thesen-ID: | Version: 0 | Status: vielleicht.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-07" in e for e in errors), errors)

    def test_m08_rejects_placeholder_event_values(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "Uhrzeit: TBD | Zeitzone: ET | Kennzahl: | Berichtszeitraum: N/A | letzter Wert: TBD.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-08" in e for e in errors), errors)

    def test_m09_rejects_non_numeric_levels_and_crv(self):
        text = report_fixture().replace(
            "Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Einstieg: bald | Stop: tief | TP1: später | TP2: offen | CRV: gut",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-09" in e for e in errors), errors)


    def test_m09_rejects_number_embedded_in_prose(self):
        text = report_fixture().replace(
            "Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Einstieg: bald 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-09" in e for e in errors), errors)

    def test_m09_accepts_complete_numeric_values_with_units(self):
        text = report_fixture().replace(
            "Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Einstieg: 100,50 EUR | Stop: 95,00 EUR | TP1: 110,00 EUR | TP2: 120,00 EUR | CRV: 2,0x",
        )
        self.assertEqual(gate.validate_report(text), [])

    def test_m08_accepts_labeled_event_values_on_following_lines(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "Uhrzeit:\n14:30\nZeitzone:\nAmerica/New_York\nKennzahl:\nCPI\nBerichtszeitraum:\nQ3 2026\nletzter Wert:\n2.8",
        )
        self.assertEqual(gate.validate_report(text), [])

    def test_m08_accepts_explicit_no_relevant_events(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "Keine relevanten Ereignisse im Berichtszeitraum.",
        )
        self.assertEqual(gate.validate_report(text), [])

    def test_m09_accepts_explicit_no_actionable_candidates(self):
        text = report_fixture().replace(
            "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; Regelstatus: valide.",
            "Keine belastbaren technischen Kandidaten; fehlende Voraussetzungen werden nicht als Setup ausgegeben.",
        )
        self.assertEqual(gate.validate_report(text), [])

    def test_m10_accepts_explicit_complete_ohlcv_and_unrestricted_candidates(self):
        text = report_fixture().replace(
            "WARNUNG-HIST-OHLCV: missing Daten; OHLCV fehlend: Scanner/Kandidaten technisch eingeschränkt.",
            "OHLCV vollständig und keine Datenlücken; Kandidaten nicht eingeschränkt und unverändert valide.",
        )
        self.assertEqual(gate.validate_report(text), [])

    def test_non_final_report_blocked(self):
        errors = gate.validate_report(report_fixture().replace("TRADING_RADAR_FACHLICH_FINAL=FINAL", "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"))
        self.assertTrue(any("NICHT_FINAL" in e for e in errors), errors)


    def test_m05_rejects_identical_duplicate_position_rows(self):
        text = report_fixture(tab2_count=2)
        text = text.replace(
            "Ticker: BBB | Status: Verkauft",
            "Ticker: AAA | Status: Gestoppt",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("identische Positionszeilen" in e for e in errors), errors)

    def test_m12_rejects_tab2_export_fingerprint_mismatch(self):
        text = report_fixture().replace("Ticker: BBB | Status: Verkauft", "Ticker: BBB | Status: Geaendert")
        errors = gate.validate_report(text)
        self.assertTrue(any("Fingerabdruck stimmt nicht" in e for e in errors), errors)

    def test_m05_fingerprint_excludes_following_chapter_11(self):
        text = report_fixture().replace(
            "10.5 Geschlossene Positionen (Quelle: Tab 2 / Geschlossene Positionen)",
            "10.5 Geschlossene Positionen (Quelle: Tab 2 / Geschlossene Positionen)\n",
        ).replace(
            "TRADING_RADAR_FACHLICH_FINAL=FINAL",
            "11. Methodische Hinweise und Datenqualität\nMethodik ist dokumentiert und gehört nicht zum Tab-2-Export.\n\nTRADING_RADAR_FACHLICH_FINAL=FINAL",
        )
        errors = gate.validate_report(text)
        self.assertFalse(any("Fingerabdruck" in e for e in errors), errors)

    def test_m06_rejects_placeholder_even_if_unavailable_prose_also_exists(self):
        text = report_fixture().replace(
            "Portfolio-Gesamtperformance nicht verfügbar; Ø Positionsperformance nicht verfügbar (getrennte Kennzahlen).",
            "Portfolio-Gesamtperformance: TBD; Ø Positionsperformance: Platzhalter.\n"
            "Portfolio-Gesamtperformance nicht verfügbar; Ø Positionsperformance nicht verfügbar.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-06" in e for e in errors), errors)

    def test_m08_accepts_explicit_unavailable_status_for_each_event_field(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "Uhrzeit: NICHT VERFÜGBAR | Zeitzone: NICHT VERFÜGBAR | "
            "Kennzahl: NICHT VERFÜGBAR | Berichtszeitraum: NICHT VERFÜGBAR | "
            "Letzter Wert: NICHT VERFÜGBAR | Konsens: NICHT VERFÜGBAR",
        )
        self.assertFalse(any("M-08" in e for e in gate.validate_report(text)))


if __name__ == "__main__":
    unittest.main()
