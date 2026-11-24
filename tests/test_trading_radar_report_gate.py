"""Focused regression coverage for the M-05..M-10 report contract."""
import importlib.util
import hashlib
import json
import pytest
import re
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
        "Quellzeile: 3 | Ticker: AAA | Einstiegsdatum: 01.08.2026 | Status: Gestoppt",
        "Quellzeile: 4 | Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Verkauft",
    ]
    tab2_digest = hashlib.sha256("\n".join(tab2_lines).encode("utf-8")).hexdigest()
    before_hash, after_hash = "a" * 64, "b" * 64
    comparison_payload = {
        "previous_sha256": before_hash,
        "current_sha256": after_hash,
        "schema": "trading-radar-thesis-register-comparison-v1",
    }
    comparison_id = hashlib.sha256(
        json.dumps(comparison_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    sections = [
        "1. Trading-Radar\nBasis und Lagebild mit ausführlicher Markteinordnung und nachvollziehbarem Kontext.\n"
        "- Marktregime: Selektives Risk-on mit defensiver Rotation.\n"
        "Marktregime-Datenstichtag: 2026-10-30\n"
        "- Datenstand UTC: 2026-10-30T21:30:00Z\n"
        "Indizes/Marktbreite: S&P 500 +1,0%; Marktbreite 8/11.\n"
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.\n"
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00; Liquiditätslage stabil.\n"
        "- Volatilität VIX: 18.00 | Quelle: Yahoo Finance (^VIX); Abrufzeit: 2026-10-30T21:20:00Z; Datenstichtag: 2026-10-30; Einheit: Indexpunkte; Verzögerung: Tages-Schlusskurs.\n"
        "- Dollar-Proxy DXY (DX-Y.NYB): 100.00 | Quelle: Yahoo Finance (DX-Y.NYB); Abrufzeit: 2026-10-30T21:20:00Z; Datenstichtag: 2026-10-30; Einheit: Indexpunkte; Verzögerung: Tages-Schlusskurs.\n"
        "Index S&P 500: 1M +1,0%; 3M +2,0%.\nIndex Nasdaq: 1M +0,5%; 3M +1,5%.\n"
        "Basisszenario: Seitwärts bis aufwärts. Eintrittsbedingung: Marktbreite mindestens 3/11. Bestätigungstrigger: Ausbruch. Status: ERFÜLLT. Invalidierungstrigger: Bruch 200-Tage-Linie. Zeithorizont: 4 Wochen. Indikatoren: Marktbreite 8/11. Konsequenz: selektive Longs.\n"
        "Positives Alternativszenario: Beschleunigter Aufwärtstrend. Eintrittsbedingung: Marktbreite mindestens 9/11. Bestätigungstrigger: neue Hochs. Status: NICHT ERFÜLLT. Invalidierungstrigger: Rückfall unter Ausbruchsniveau. Zeithorizont: 4 Wochen. Indikatoren: Marktbreite 8/11. Konsequenz: Risiko erhöhen.\n"
        "Negatives Alternativszenario: Korrektur. Eintrittsbedingung: Marktbreite unter 3/11. Bestätigungstrigger: Marktbreite bricht. Status: NICHT ERFÜLLT. Invalidierungstrigger: Rückeroberung 50-Tage-Linie. Zeithorizont: 4 Wochen. Indikatoren: Marktbreite 8/11. Konsequenz: Risiko reduzieren.",
        "2. Thesen-Update\nThesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.\n"
        "THESENREGISTER-VERGLEICH: PASS\nVORHER-SHA256: " + before_hash + "\nNACHHER-SHA256: " + after_hash + "\n"
        "THESENREGISTER-VERGLEICH-ID: " + comparison_id + "\n"
        "VORHER_THESEN_COUNT: 1\nVORHER_AKTIVE_THESEN: 1\nNACHHER_THESEN_COUNT: 1\nNACHHER_AKTIVE_THESEN: 1\n"
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "THESEN_NEU: KEINE\nTHESEN_AKTUALISIERT: T-1\nTHESEN_UNVERAENDERT: KEINE\nTHESEN_ARCHIVIERT: KEINE\n"
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: YES\n"
        "THESIS_STATUS: BESTÄTIGT\nTHESIS_REPLACED_BY: KEINE\nVERSION: 2",
        "3. Ereignisradar\nUhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
        "4. Assetklassenvergleich\nRelative-Stärke-Matrix / Kapitalrotationsvergleich\nDatenstichtag: 2026-10-30. Fenster: 1M und 3M.\n"
        "Gewinner: NICHT VERFÜGBAR (Datenlücke: keine vergleichbaren Reihen). Verlierer: NICHT VERFÜGBAR. Divergenzen: keine belastbaren Daten.\n"
        "Indizes: 1M/3M NICHT VERFÜGBAR (Datenlücke: keine vergleichbaren Reihen).\n"
        "Regionen: 1M/3M NICHT VERFÜGBAR (Datenlücke: keine vergleichbaren Reihen).\n"
        "Sektoren: 1M/3M NICHT VERFÜGBAR (Datenlücke: keine vergleichbaren Reihen).\n"
        "Anlageklassen: 1M/3M NICHT VERFÜGBAR (Datenlücke: keine vergleichbaren Reihen).\n"
        "Vergleich von Assetklassen und Marktregimen.",
        "5. Technische Kandidaten\nTicker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; Regelstatus: valide.\n"
        "Kandidatenquellen: Scanner-Quellen geprüft (2 Quellen).\n"
        "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup.\n"
        "WATCHLIST: Keine Watchlist-Einträge. Begründung: Keine belastbaren Kurs-/Strukturtrigger aus den verfügbaren Daten ableitbar.\n"
        "VERWORFENE KANDIDATEN: Keine verworfenen Kandidaten. Begründung: Es lagen keine formal geprüften Kandidaten vor.",
        "6. Portfolio & Risiko\nGeschlossene Positionen: Tab 2 | TAB-2-ANZAHL=2 | Stop-/TP-Bestand: dokumentiert | realisierte Ergebnisse: Performance vorhanden | Konzentration und Portfolio-Risiko bewertet. " + quantity_line,
         "7. MSCI-Vergleich\nPortfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        "8. Daten-/Prüfanhang\n" + ("OHLCV-Abdeckung: 10/10 Symbole abgedeckt; Status=gaps.\nOHLCV-Qualitätsstatus: VALID=8, MISSING=1, STALE=1, INVALID=0; Summe=10/10.\nOHLCV-Symbole-VALID: A,B,C,D,E,F,G,H\nOHLCV-Symbole-MISSING: I\nOHLCV-Symbole-STALE: J\nOHLCV-Symbole-INVALID: KEINE\nKandidaten-Mapping: vollständig.\nWARNUNG-HIST-OHLCV: missing Daten; OHLCV fehlend: Scanner/Kandidaten technisch eingeschränkt." if ohlcv_warning else "OHLCV-Abdeckung: 10/10 Symbole abgedeckt; Status=complete.\nOHLCV-Qualitätsstatus: VALID=10, MISSING=0, STALE=0, INVALID=0; Summe=10/10.\nOHLCV-Symbole-VALID: A,B,C,D,E,F,G,H,I,J\nOHLCV-Symbole-MISSING: KEINE\nOHLCV-Symbole-STALE: KEINE\nOHLCV-Symbole-INVALID: KEINE\nKandidaten-Mapping: vollständig.\nDaten geprüft und dokumentiert, keine besonderen Datenqualitätsprobleme erkannt."),
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

    def test_m05_rejects_nonempty_but_invalid_tab2_entry_date(self):
        text = report_fixture().replace(
            "Einstiegsdatum: 01.08.2026",
            "Einstiegsdatum: 31.02.2026",
            1,
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("Ticker+Einstiegsdatum nicht verifizierbar" in e for e in errors), errors)

    def test_m06_requires_separate_portfolio_performance_and_position_average(self):
        text = report_fixture().replace("Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.", "Keine Performancewerte vorhanden.")
        errors = gate.validate_report(text)
        self.assertTrue(any("M-01" in e for e in errors), errors)

    def test_m07_requires_versioned_thesis(self):
        text = report_fixture().replace("Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.", "Keine Theseninformationen dokumentiert.")
        errors = gate.validate_report(text)
        self.assertTrue(any("M-07" in e for e in errors), errors)

    def test_m07_accepts_explicit_unmodified_register_variant(self):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
            "Das Thesenregister wurde geprüft und ist unverändert; es gab keine Änderung.",
        )
        self.assertFalse(any("M-07" in e for e in gate.validate_report(text)))

    def test_m07_accepts_markdown_bold_thesis_fields(self):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
            "**Thesen-ID:** T-1 | **Version:** 2 | **Status:** BESTÄTIGT | gegenüber Vorversion aktualisiert.",
        )
        self.assertFalse(any("M-07" in e for e in gate.validate_report(text)))

    def test_m07_accepts_explicit_no_thesis_update_without_fabricated_id(self):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
            "Keine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.",
        )
        self.assertFalse(any("M-07" in e for e in gate.validate_report(text)))

    def test_m07_rejects_noncanonical_change_descriptor_as_lifecycle_status(self):
        text = report_fixture().replace(
            "Status: BESTÄTIGT",
            "Status: Aktualisiert",
        )
        self.assertTrue(any("M-07" in e for e in gate.validate_report(text)))

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

    def test_m08_accepts_markdown_bold_field_labels(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "**Uhrzeit:** 14:30 | **Zeitzone:** ET | **Kennzahl:** CPI | "
            "**Berichtszeitraum:** Q3 2026 | **letzter Wert:** 2.8 | **Konsens:** 2.9.",
        )
        self.assertFalse(any("M-08" in e for e in gate.validate_report(text)))

    def test_m08_scenario_bullets_are_continuations_of_the_fomc_record(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "- Ereignis: FOMC-Zinsentscheid\n"
            "  Datum: 2026-10-28\n"
            "  Quelle: Federal Reserve, offizieller Kalender\n"
            "  Uhrzeit: 14:00 | Zeitzone: America/New_York | Kennzahl: Fed Funds Rate | "
            "Zeitraum: Sitzung 2026-10-28 | letzter Wert: 3,75%-4,00% | Konsens: 3,75%-4,00%.\n"
            "- Szenarien unter Konsens: unerwartete Zinssenkung\n"
            "- Szenarien entsprechend Konsens: Korridor bleibt unverändert\n"
            "- Szenarien über Konsens: unerwartete Zinserhöhung\n"
            "- Reaktionskanäle: Anleihen, Devisen, Aktien\n"
            "- Auswirkung: Volatilität bei Zins-Assets; keine zweite Ereignisidentität.",
        )
        errors = gate.validate_report(text)
        self.assertFalse(any("M-08" in e for e in errors), errors)

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
            "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
            "Portfolio-Gesamtperformance: TBD; Ø Positionsperformance: Platzhalter.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-01" in e for e in errors), errors)

    def test_m07_rejects_empty_identity_and_invalid_status(self):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
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

    def test_m10_rejects_no_gap_claim_when_status_counts_include_non_valid_symbols(self):
        text = report_fixture().replace(
            "WARNUNG-HIST-OHLCV: missing Daten; OHLCV fehlend: Scanner/Kandidaten technisch eingeschränkt.",
            "OHLCV vollständig und keine Datenlücken; Kandidaten nicht eingeschränkt und unverändert valide.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("Freitext behauptet Datenlückenfreiheit" in error for error in errors), errors)

    def test_m10_accepts_existing_complete_coverage_wording_without_analysis_restriction(self):
        text = report_fixture().replace(
            "WARNUNG-HIST-OHLCV: missing Daten; OHLCV fehlend: Scanner/Kandidaten technisch eingeschränkt.",
            "OHLCV vollständig für die definierten Scanner- und Benchmark-Universen; "
            "keine technischen Datenlücken, welche die Sektor-Analyse einschränken.",
        )
        self.assertFalse(any("M-10" in error for error in gate.validate_report(text)), gate.validate_report(text))

    def test_non_final_report_blocked(self):
        errors = gate.validate_report(report_fixture().replace("TRADING_RADAR_FACHLICH_FINAL=FINAL", "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"))
        self.assertTrue(any("NICHT_FINAL" in e for e in errors), errors)


    def _recompute_tab2_digest(self, text):
        start = text.index("10.5 Geschlossene Positionen")
        status_start = text.index("TRADING_RADAR_FACHLICH_FINAL=")
        block_lines = text[start:status_start].splitlines()[1:]
        digest = hashlib.sha256("\n".join(
            line.rstrip() for line in block_lines
            if not line.startswith("TAB-2-EXPORT-SHA256=")
        ).strip().encode("utf-8")).hexdigest()
        return re.sub(
            r"(?m)^TAB-2-EXPORT-SHA256=[0-9a-f]{64}$",
            f"TAB-2-EXPORT-SHA256={digest}",
            text,
        )

    def test_m05_allows_same_ticker_with_distinct_entry_dates(self):
        text = report_fixture(tab2_count=2).replace(
            "Quellzeile: 4 | Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Verkauft",
            "Quellzeile: 5 | Ticker: AAA | Einstiegsdatum: 02.08.2026 | Status: Gestoppt",
        )
        text = self._recompute_tab2_digest(text)
        errors = gate.validate_report(text)
        self.assertFalse(any("Positionsschluessel" in e for e in errors), errors)
        self.assertFalse(any("Fingerabdruck" in e for e in errors), errors)

    def test_m05_rejects_same_ticker_and_same_entry_date(self):
        text = report_fixture(tab2_count=2).replace(
            "Quellzeile: 4 | Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Verkauft",
                        "Quellzeile: 5 | Ticker: AAA | Einstiegsdatum: 2026-08-01 | Status: Gestoppt",
        )
        text = self._recompute_tab2_digest(text)
        errors = gate.validate_report(text)
        self.assertTrue(any("Positionsschluessel Ticker+Einstiegsdatum doppelt" in e for e in errors), errors)

    def test_m05_rejects_nonempty_but_invalid_entry_dates(self):
        for invalid_date in ("32.13.2026", "2026-02-30", "Datum unbekannt", "TBD"):
            with self.subTest(invalid_date=invalid_date):
                text = report_fixture().replace(
                    "Einstiegsdatum: 02.08.2026", f"Einstiegsdatum: {invalid_date}"
                )
                text = self._recompute_tab2_digest(text)
                errors = gate.validate_report(text)
                self.assertTrue(
                    any("Positionsidentitaet fehlt" in e for e in errors), errors
                )

    def test_m05_accepts_all_contract_entry_date_formats(self):
        for valid_date in ("02.08.2026", "2026-08-02", "02/08/2026"):
            with self.subTest(valid_date=valid_date):
                text = report_fixture().replace(
                    "Einstiegsdatum: 02.08.2026", f"Einstiegsdatum: {valid_date}"
                )
                text = self._recompute_tab2_digest(text)
                errors = gate.validate_report(text)
                self.assertFalse(any("Positionsidentitaet fehlt" in e for e in errors), errors)

    def test_m05_rejects_missing_entry_date(self):
        text = report_fixture(tab2_count=2).replace(
            "Einstiegsdatum: 02.08.2026 | ", ""
        )
        text = self._recompute_tab2_digest(text)
        errors = gate.validate_report(text)
        self.assertTrue(any("Positionsidentitaet fehlt" in e for e in errors), errors)

    def test_m05_rejects_duplicate_source_row_identity_even_with_distinct_trade_key(self):
        text = report_fixture(tab2_count=2).replace(
            "Quellzeile: 4 | Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Verkauft",
            "Quellzeile: 3 | Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Verkauft",
        )
        text = self._recompute_tab2_digest(text)
        errors = gate.validate_report(text)
        self.assertTrue(any("Quellzeilennummer ist doppelt" in e for e in errors), errors)

    def test_m12_rejects_tab2_export_fingerprint_mismatch(self):
        text = report_fixture().replace("Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Verkauft", "Ticker: BBB | Einstiegsdatum: 02.08.2026 | Status: Geaendert")
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
            "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
            "Portfolio-Gesamtperformance: TBD; Ø Positionsperformance: Platzhalter.\n"
            "Portfolio-Gesamtperformance nicht verfügbar; Ø Positionsperformance nicht verfügbar.",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("M-01" in e for e in errors), errors)

    def test_m08_accepts_explicit_unavailable_status_for_each_event_field(self):
        text = report_fixture().replace(
            "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
            "Uhrzeit: NICHT VERFÜGBAR | Zeitzone: NICHT VERFÜGBAR | "
            "Kennzahl: NICHT VERFÜGBAR | Berichtszeitraum: NICHT VERFÜGBAR | "
            "Letzter Wert: NICHT VERFÜGBAR | Konsens: NICHT VERFÜGBAR",
        )
        self.assertFalse(any("M-08" in e for e in gate.validate_report(text)))


    def test_m09_rejects_implausible_long_stop_above_entry(self):
        text = report_fixture().replace(
            "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Ticker: AAA | Richtung: LONG | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 105 | TP1: 110 | TP2: 120 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("unplausible Stop-/TP1-Lage" in error for error in errors), errors)

    def test_m09_accepts_explicit_short_direction_and_short_crv_geometry(self):
        text = report_fixture().replace(
            "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Ticker: AAA | Richtung: SHORT | JETZT TECHNISCH PRÜFEN | Trigger: Breakdown | Einstieg: 100 | Stop: 105 | TP1: 90 | TP2: 80 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertFalse(any("M-09" in error for error in errors), errors)

    def test_m09_accepts_explicit_long_direction_and_long_crv_geometry(self):
        text = report_fixture().replace(
            "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Ticker: AAA | Richtung: LONG | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertFalse(any("M-09" in error for error in errors), errors)

    def test_m09_infers_short_direction_when_direction_is_missing(self):
        # Regression for the former bug: the valid SHORT geometry used the
        # LONG risk formula and produced a negative risk.
        text = report_fixture().replace(
            "Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Einstieg: 100 | Stop: 105 | TP1: 90 | TP2: 80 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertFalse(any("M-09" in error for error in errors), errors)

    def test_m09_infers_long_direction_when_direction_is_missing(self):
        errors = gate.validate_report(report_fixture())
        self.assertFalse(any("M-09" in error for error in errors), errors)

    def test_m09_rejects_missing_direction_with_ambiguous_geometry(self):
        baseline = "Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2"
        invalid_geometries = (
            "Einstieg: 100 | Stop: 105 | TP1: 110 | TP2: 120 | CRV: 2",
            "Einstieg: 100 | Stop: 95 | TP1: 90 | TP2: 80 | CRV: 2",
        )
        for geometry in invalid_geometries:
            with self.subTest(geometry=geometry):
                text = report_fixture().replace(baseline, geometry)
                errors = gate.validate_report(text)
                self.assertTrue(
                    any("unplausible Stop-/TP1-Lage" in error for error in errors),
                    errors,
                )

    def test_m09_rejects_unknown_explicit_direction_instead_of_inferring(self):
        text = report_fixture().replace(
            "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Ticker: AAA | Richtung: NEUTRAL | JETZT TECHNISCH PRÜFEN | Trigger: Breakdown | Einstieg: 100 | Stop: 105 | TP1: 90 | TP2: 80 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("unplausible Stop-/TP1-Lage" in error for error in errors), errors)

    def test_m09_rejects_explicit_direction_contradicting_tp1_geometry(self):
        text = report_fixture().replace(
            "Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2",
            "Ticker: AAA | Richtung: SHORT | JETZT TECHNISCH PRÜFEN | Trigger: Breakdown | Einstieg: 100 | Stop: 105 | TP1: 110 | TP2: 120 | CRV: 2",
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("unplausible Stop-/TP1-Lage" in error for error in errors), errors)

    def test_m09_rejects_missing_direction_when_stop_or_tp1_equals_entry(self):
        baseline = "Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2"
        invalid_geometries = (
            "Einstieg: 100 | Stop: 100 | TP1: 110 | TP2: 120 | CRV: 2",
            "Einstieg: 100 | Stop: 95 | TP1: 100 | TP2: 120 | CRV: 2",
        )
        for geometry in invalid_geometries:
            with self.subTest(geometry=geometry):
                text = report_fixture().replace(baseline, geometry)
                errors = gate.validate_report(text)
                self.assertTrue(
                    any("unplausible Stop-/TP1-Lage" in error for error in errors),
                    errors,
                )

    def test_m09_candidate_fields_are_not_borrowed_from_another_record(self):
        candidate_block = (
            "5. Technische Kandidaten\n"
            "- Ticker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Risiko vorhanden | Regelstatus: valide\n"
            "- Ticker: BBB | AUF TRIGGER WARTEN | Trigger: fehlt"
        )
        text = report_fixture().replace(
            "5. Technische Kandidaten\nTicker: AAA | JETZT TECHNISCH PRÜFEN | Trigger: Breakout | Einstieg: 100 | Stop: 95 | TP1: 110 | TP2: 120 | CRV: 2 | Gegenargument: Datenlücke geprüft; Regelstatus: valide.",
            candidate_block,
        )
        errors = gate.validate_report(text)
        self.assertTrue(any("Kandidat 2" in error and "M-09" in error for error in errors), errors)


    def test_m09_scenario_data_gaps_block_final_but_are_preserved_for_non_final(self):
        report = report_fixture()
        for label in (
            "Basisszenario: Seitwärts bis aufwärts.",
            "Positives Alternativszenario: Beschleunigter Aufwärtstrend.",
            "Negatives Alternativszenario: Korrektur.",
        ):
            report = report.replace(label, label.split(":", 1)[0] + ": NICHT ABLEITBAR – Datenlücke: Zeithorizont.")
        errors_final = gate.validate_report(report)
        scenario_errors = [error for error in errors_final if "M-09-SZENARIO" in error and "darf nicht FINAL sein" in error]
        self.assertEqual(len(scenario_errors), 3, errors_final)
        non_final = report.replace("TRADING_RADAR_FACHLICH_FINAL=FINAL", "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL")
        errors_non_final = gate.validate_report(non_final)
        self.assertFalse(any("darf nicht FINAL sein" in error for error in errors_non_final), errors_non_final)

    def test_m06_accepts_explicit_zero_confirmed_setups(self):
        report = report_fixture().replace(
            "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup.",
            "BESTÄTIGTE SETUPS: 0 bestätigte Setups – Begründung: kein bestätigter Trigger.",
        )
        errors = gate.validate_report(report)
        self.assertFalse(any("M-06:" in error for error in errors), errors)

    def test_m06_rejects_zero_confirmed_setups_with_setup_record(self):
        report = report_fixture().replace(
            "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup.",
            "BESTÄTIGTE SETUPS: 0 bestätigte Setups – Begründung: kein bestätigter Trigger.\n"
            "- Name: Alpha AG (ABC) | Setup-ID: S-1 | Quelle: Setups.csv | Status: valide | "
            "Begründung: Alles ok | Trigger: Breakout",
        )
        errors = gate.validate_report(report)
        self.assertTrue(
            any("meldet null Setups, enthält aber zugleich Setup-Datensätze" in error for error in errors),
            errors,
        )

    def test_m06_rejects_source_validity_as_substitute_for_confirmed_setup_evidence(self):
        report = report_fixture().replace(
            "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup.",
            "BESTÄTIGTE SETUPS:\n- Name: Alpha AG (ABC) | Setup-ID: S-1 | Quelle: Setups.csv | Status: valide | Begründung: Alles ok | Trigger: Breakout",
        )
        errors = gate.validate_report(report)
        self.assertTrue(any("Bestätigtes Setup 1 ist nicht technisch nachgewiesen" in error for error in errors), errors)

    def test_m06_accepts_complete_multiline_confirmed_setup_summary(self):
        complete = (
            "BESTÄTIGTE SETUPS:\n"
            "- Name: Alpha AG (ABC) | Setup-ID: S-1 | Quelle: Setups.csv\n"
            "  Richtung: LONG | Verknüpfte These: TH-1\n"
            "  Technische Bestätigung: BESTÄTIGT | Datenstichtag: 2026-10-30\n"
            "  Einstieg: 100 | Stop: 95 | CRV: 2.0\n"
            "  Gegenargument: keines | Datenlücken: keine | Regel-/Datenstatus: PASS | Trigger: Breakout"
        )
        report = report_fixture().replace(
            "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup.",
            complete,
        )
        errors = gate.validate_report(report)
        self.assertFalse(any("Bestätigtes Setup 1" in error for error in errors), errors)

    def test_m06_rejects_confirmed_setup_below_crv1_minimum(self):
        complete = (
            "BESTÄTIGTE SETUPS:\n"
            "- Name: Alpha AG (ABC) | Setup-ID: S-1 | Quelle: Setups.csv | Richtung: LONG | "
            "Verknüpfte These: TH-1 | Technische Bestätigung: BESTÄTIGT | Datenstichtag: 2026-10-30 | "
            "Einstieg: 100 | Stop: 95 | CRV: 0.9 | Gegenargument: keines | Datenlücken: keine | "
            "Regel-/Datenstatus: PASS | Trigger: Breakout"
        )
        report = report_fixture().replace(
            "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup.",
            complete,
        )
        errors = gate.validate_report(report)
        self.assertTrue(any("unterschreitet CRV1 >= 1" in error for error in errors), errors)

    def test_m03_rejects_unverifiable_candidate_mapping_claimed_unrestricted(self):
        report = report_fixture().replace(
            "Kandidaten-Mapping: vollständig.",
            "Kandidaten-Mapping: NICHT VERIFIZIERBAR.\nKandidaten nicht eingeschränkt.",
        )
        errors = gate.validate_report(report)
        self.assertTrue(any("Kandidaten-Mapping ist NICHT VERIFIZIERBAR" in error for error in errors), errors)

    def test_m01_rejects_portfolio_underperformance_claim_when_portfolio_metric_unavailable(self):
        report = report_fixture().replace(
            "Basis und Lagebild mit ausführlicher Markteinordnung und nachvollziehbarem Kontext.",
            "Basis und Lagebild mit ausführlicher Markteinordnung und nachvollziehbarem Kontext.\n"
            "Underperformance des Portfolios (-9,62% vs. MSCI World Benchmark seit 07.08.2026).",
        )
        errors = gate.validate_report(report)
        self.assertTrue(any("Numerische System-/Benchmark-Performance außerhalb" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()


def test_nontradable_candidate_with_explicit_unavailable_levels_is_not_a_m09_trade_failure():
    from trading_radar_report_gate import validate_report
    report = """# 1. Trading-Radar
**Hauptthese**: Beispielthese mit Marktbelegen.
Indizes/Marktbreite: Index stabil.
Zinsen/Renditen: Rendite bleibt restriktiv.
Dollar/Liquidität/Volatilität: VIX stabil, Dollar hoch.
Marktregime: Selektiv.
Basisszenario: Defensive Rotation.

# 2. Thesen-Update
**Thesen-ID**: TH-1
**Version**: 1
**Status**: WARTET AUF EREIGNIS
**Gegenbelege**: Marktbreite schwach.
**Alternative**: Rotation setzt sich nicht fort.
**Bestätigungstrigger**: Qualitätsindex bricht über Hoch.
**Invalidierungstrigger**: Rendite steigt über Schwelle.

# 3. Ereignisradar
Keine relevanten Ereignisse im geprüften Zeitraum.

# 4. Assetklassenvergleich
Aktien relativ stärker.

# 5. Technische Kandidaten
NICHT HANDELBAR:
- **Asset/Ticker**: American Electric Power Company, Inc. (AEP)
  - **Statusgruppe**: NICHT HANDELBAR
  - **Trigger**: A-Status gemeldet; unabhängiges technisches Setup nicht bestätigt.
  - **Einstieg**: NICHT VERFÜGBAR
  - **Stop**: NICHT VERFÜGBAR
  - **TP1**: NICHT VERFÜGBAR
  - **TP2**: NICHT VERFÜGBAR
  - **CRV**: NICHT VERFÜGBAR
  - **Gegenargument**: Handelslevels fehlen.
  - **Regel-/Datenstatus**: NICHT HANDELBAR – Statusmeldung ist kein Kauf.

# 6. Portfolio & Risiko
Keine offenen Positionen.

# 7. MSCI-Vergleich
Kennzahlen nicht verfügbar.

# 8. Daten-/Prüfanhang
OHLCV-Abdeckung: 862/862 Symbole abgedeckt; Status=gaps.
Lückenstatus und Kandidatenauswirkung: Kandidaten-Mapping vollständig; keine direkte Überschneidung mit geprüften Kandidaten.

TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL
"""
    findings = validate_report(report)
    assert not any("M-09:" in finding for finding in findings), findings


def test_m01_reconstructs_equal_weighted_portfolio_from_auditable_trade_rows(tmp_path):
    from trading_radar_portfolio_performance import calculate_portfolio_performance, format_performance_block
    source = tmp_path / "positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegskurs\n"
        "AAA;Long;01.01.2026;100;110;Offen;\n"
        "BBB;Short;02.01.2026;100;105;Gestoppt;105\n",
        encoding="utf-8",
    )
    result = calculate_portfolio_performance([source])
    block = format_performance_block(result)
    text = report_fixture()
    text = text.replace(
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        block,
    )
    assert gate.validate_report(text) == []


def test_m01_rejects_tampered_trade_detail_hash(tmp_path):
    from trading_radar_portfolio_performance import calculate_portfolio_performance, format_performance_block
    source = tmp_path / "positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegskurs\n"
        "AAA;Long;01.01.2026;100;110;Offen;\n",
        encoding="utf-8",
    )
    block = format_performance_block(calculate_portfolio_performance([source]))
    digest_match = re.search(r"(?m)^Performance-Einzelrenditen-SHA256: ([0-9a-f]{64})$", block)
    assert digest_match
    digest = digest_match.group(1)
    tampered_digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    tampered_block = block.replace(digest, tampered_digest)
    text = report_fixture().replace(
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        tampered_block,
    )
    errors = gate.validate_report(text)
    assert any("SHA256 der Einzelrenditen stimmt nicht" in error for error in errors)


def test_m01_rejects_outperformance_not_derived_from_portfolio_and_benchmark(tmp_path):
    from trading_radar_portfolio_performance import calculate_portfolio_performance, format_performance_block
    source = tmp_path / "positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegskurs\n"
        "AAA;Long;01.01.2026;100;110;Offen;\n",
        encoding="utf-8",
    )
    block = format_performance_block(calculate_portfolio_performance([source]))
    replacement = (
        "Benchmark-Performance: +2,00 %\n"
        "Out-/Underperformance: +5,00 Prozentpunkte\n\n" + block
    )
    text = report_fixture().replace(
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        replacement,
    )
    errors = gate.validate_report(text)
    assert any("Out-/Underperformance stimmt nicht" in error for error in errors)


def test_numeric_portfolio_result_requires_explicit_benchmark_exclusion():
    import hashlib

    payload = (
        "AAA|2026-01-01|aaaaaaaaaaaaaaaa|OPEN|LONG|100.0000000000|110.0000000000|10.0000000000;"
        "BBB|2026-01-02|bbbbbbbbbbbbbbbb|CLOSED|LONG|100.0000000000|90.0000000000|-10.0000000000"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    block = (
        "Portfolio-Gesamtperformance: +0,00 %\n"
        "Ø Positionsperformance: +10,00 %\n"
        "Performance-Datenbasis: 2 eindeutige Trades; abgeschlossen=1; offen=1\n"
        "Berechnungsmethode: gleichgewichteten arithmetischen Mittelwert der individuellen prozentualen Renditen aller eindeutigen offenen und abgeschlossenen Trades; jeder Trade zählt genau einmal; genau 1 Stück je Trade; abgeschlossene Trades zum Ausstiegskurs; offene Trades zum aktuellen Kurs; Kosten=0; keine Kapitalgewichtung und keine Währungsumrechnung.\n"
        "Formel je Trade: Long=(Kurs-Einstieg)/Einstieg*100; Short=(Einstieg-Kurs)/Einstieg*100. Portfolio-Gesamtperformance=Summe der Einzelrenditen geteilt durch Anzahl eindeutiger Trades.\n"
        f"Performance-Einzelrenditen: {payload}\n"
        f"Performance-Einzelrenditen-SHA256: {digest}\n"
    )
    text = report_fixture().replace(
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        block,
    )
    errors_without = gate.validate_report(text)
    assert any("Benchmark-Abgrenzung" in error for error in errors_without), errors_without

    text = text.replace(
        "Performance-Einzelrenditen-SHA256: " + digest,
        "Performance-Einzelrenditen-SHA256: " + digest + "\nBenchmarkpositionen ausgeschlossen: EUNL.DE (Anzahl=1); externe Vergleichspositionen zählen nicht als System-Trades.",
    )
    errors_with = gate.validate_report(text)
    assert not any("Benchmark-Abgrenzung" in error for error in errors_with), errors_with


def test_numeric_portfolio_audit_rows_cannot_contain_eunl_benchmark():
    import hashlib

    payload = (
        "EUNL.DE|2026-01-01|aaaaaaaaaaaaaaaa|OPEN|LONG|100.0000000000|110.0000000000|10.0000000000;"
        "BBB|2026-01-02|bbbbbbbbbbbbbbbb|CLOSED|LONG|100.0000000000|90.0000000000|-10.0000000000"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    block = (
        "Portfolio-Gesamtperformance: +0,00 %\n"
        "Ø Positionsperformance: +10,00 %\n"
        "Performance-Datenbasis: 2 eindeutige Trades; abgeschlossen=1; offen=1\n"
        "Berechnungsmethode: gleichgewichteten arithmetischen Mittelwert der individuellen prozentualen Renditen aller eindeutigen offenen und abgeschlossenen Trades; jeder Trade zählt genau einmal; genau 1 Stück je Trade; abgeschlossene Trades zum Ausstiegskurs; offene Trades zum aktuellen Kurs; Kosten=0; keine Kapitalgewichtung und keine Währungsumrechnung.\n"
        "Formel je Trade: Long=(Kurs-Einstieg)/Einstieg*100; Short=(Einstieg-Kurs)/Einstieg*100. Portfolio-Gesamtperformance=Summe der Einzelrenditen geteilt durch Anzahl eindeutiger Trades.\n"
        f"Performance-Einzelrenditen: {payload}\n"
        f"Performance-Einzelrenditen-SHA256: {digest}\n"
        "Benchmarkpositionen ausgeschlossen: EUNL.DE (Anzahl=1); externe Vergleichspositionen zählen nicht als System-Trades.\n"
    )
    text = report_fixture().replace(
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        block,
    )
    errors = gate.validate_report(text)
    assert any("EUNL.DE darf nicht als System-Trade" in error for error in errors), errors


def test_m06_allows_distinct_setup_ids_for_same_ticker_in_same_group():
    base = report_fixture()
    old = (
        "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup."
    )
    new = (
        "BESTÄTIGTE SETUPS:\n"
        "- Name: Alpha AG (ABC) | Setup-ID: trendfolge|pullback|hammer | Quelle: Setups.csv | Richtung: LONG | Verknüpfte These: TH-1 | Technische Bestätigung: BESTÄTIGT | Datenstichtag: 2026-10-30 | Einstieg: 100 | Stop: 95 | CRV: 2.0 | Gegenargument: keines | Datenlücken: keine | Regel-/Datenstatus: PASS | Trigger: 100\n"
        "- Name: Alpha AG (ABC) | Setup-ID: trendwende|bodenbildung|doppelboden | Quelle: Trendwende_Setups.csv | Richtung: LONG | Verknüpfte These: TH-2 | Technische Bestätigung: BESTÄTIGT | Datenstichtag: 2026-10-30 | Einstieg: 100 | Stop: 95 | CRV: 2.0 | Gegenargument: keines | Datenlücken: keine | Regel-/Datenstatus: PASS | Trigger: 95"
    )
    assert old in base
    report = base.replace(old, new)
    errors = gate.validate_report(report)
    assert not any("Doppelte Kandidatenidentität" in error for error in errors), errors


def test_m06_rejects_duplicate_same_setup_identity_for_same_ticker():
    base = report_fixture()
    old = (
        "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: Im geprüften Datenstand kein vollständig bestätigtes Setup."
    )
    duplicate = (
        "BESTÄTIGTE SETUPS:\n"
        "- Name: Alpha AG (ABC) | Setup-ID: trendfolge|pullback|hammer | Quelle: Setups.csv | Richtung: LONG | Verknüpfte These: TH-1 | Technische Bestätigung: BESTÄTIGT | Datenstichtag: 2026-10-30 | Einstieg: 100 | Stop: 95 | CRV: 2.0 | Gegenargument: keines | Datenlücken: keine | Regel-/Datenstatus: PASS | Trigger: 100\n"
        "- Name: Alpha AG (ABC) | Setup-ID: trendfolge|pullback|hammer | Quelle: Setups_backup.csv | Richtung: LONG | Verknüpfte These: TH-1 | Technische Bestätigung: BESTÄTIGT | Datenstichtag: 2026-10-30 | Einstieg: 100 | Stop: 95 | CRV: 2.0 | Gegenargument: keines | Datenlücken: keine | Regel-/Datenstatus: PASS | Trigger: 100"
    )
    assert old in base
    errors = gate.validate_report(base.replace(old, duplicate))
    assert any("Doppelte Kandidatenidentität" in error for error in errors), errors


def test_m01_malformed_numeric_block_without_trade_counts_fails_closed_without_crashing():
    import hashlib
    from trading_radar_report_gate import _validate_m05_m10
    detail = "AAA|2026-01-01|0123456789abcdef|OPEN|LONG|100.0000000000|110.0000000000|10.0000000000"
    digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
    report = """### 7. MSCI-Vergleich
Portfolio-Gesamtperformance: +10.00 %
Ø Positionsperformance: +10.00 %
Berechnungsmethode: gleichgewichteten arithmetischen Mittelwert der individuellen prozentualen Renditen; jeder Trade zählt genau einmal; genau 1 Stück je Trade; abgeschlossene Trades zum Ausstiegskurs; offene Trades zum aktuellen Kurs; Kosten=0; keine Kapitalgewichtung und keine Währungsumrechnung.
Benchmarkpositionen ausgeschlossen: KEINE (Anzahl=0)
Formel je Trade: Long=(Kurs-Einstieg)/Einstieg*100; Short=(Einstieg-Kurs)/Einstieg*100. Portfolio-Gesamtperformance=Summe der Einzelrenditen geteilt durch Anzahl eindeutiger Trades.
Performance-Einzelrenditen: AAA|2026-01-01|0123456789abcdef|OPEN|LONG|100.0000000000|110.0000000000|10.0000000000
Performance-Einzelrenditen-SHA256: DIGEST
""".replace("DIGEST", digest)
    errors = []
    _validate_m05_m10(report, errors)
    assert any("Performance-Datenbasis mit Trade-/Statusanzahl fehlt" in item for item in errors)



def test_m07_allows_repeated_narrative_value_when_each_distinct_treasury_observation_is_sourced():
    report = report_fixture()
    report = report.replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | "
        "Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | "
        "Einheit: % | Verzögerung: 1 Börsentag.\n"
        "US-10J-Rendite (^TNX): 4,95% | Quelle: Yahoo Finance (^TNX) | "
        "Abrufzeit: 2026-10-30T20:00:00Z | Datenstichtag: 2026-10-30 | "
        "Einheit: % | Verzögerung: Tages-Schlusskurs."
    )
    report = report.replace(
        "Vergleich von Assetklassen und Marktregimen.",
        "Vergleich von Assetklassen und Marktregimen. "
        "Interpretation: US-10Y-Treasury bleibt mit 5,0000% erhöht."
    )
    errors = gate.validate_report(report)
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors)


def test_m05_rejects_tampered_register_comparison_digest():
    text = report_fixture().replace(
        "THESENREGISTER-VERGLEICH-ID: 03ad73fb6916f8c1d10706e17fd4e1a260ead1c02fb21797d8316756cc4f0c96",
        "THESENREGISTER-VERGLEICH-ID: " + "0" * 64,
    )
    errors = gate.validate_report(text)
    assert any("M-05" in error for error in errors), errors


def test_m05_requires_complete_previous_register_ids_and_versions():
    text = report_fixture().replace(
        "VORHER-REGISTER-VERSION: 1\n",
        "",
    )
    errors = gate.validate_report(text)
    assert any("M-05" in error for error in errors), errors


def test_m05_rejects_change_lists_that_disagree_with_register_versions():
    text = report_fixture().replace(
        "THESEN_AKTUALISIERT: T-1", "THESEN_AKTUALISIERT: KEINE"
    ).replace(
        "THESEN_UNVERAENDERT: KEINE", "THESEN_UNVERAENDERT: T-1"
    )
    errors = gate.validate_report(text)
    assert any("Semantischer Thesenregistervergleich" in error for error in errors), errors


def test_m05_rejects_version_jump_not_matching_single_update():
    text = report_fixture().replace(
        "REGISTER-VERSION: 2", "REGISTER-VERSION: 3"
    )
    errors = gate.validate_report(text)
    assert any("Semantischer Thesenregistervergleich" in error for error in errors), errors


def test_m05_rejects_removed_previous_thesis_without_archive_row():
    text = report_fixture().replace(
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: YES",
        "THESIS_ID: T-2\nREGISTER-VERSION: 1\nTHESIS_ACTIVE: YES",
    )
    errors = gate.validate_report(text)
    assert any("Semantischer Thesenregistervergleich" in error for error in errors), errors


def test_m05_producer_writes_machine_checkable_before_after_comparison():
    import json
    import tempfile
    from trading_radar_thesen import update_register
    import Trading_Radar_Alleinauswertung as production

    thesis_payload = {
        "data_cutoff": "2026-10-30T00:00:00+00:00",
        "theses": [{
            "thesis_id": "T-1",
            "thesis": "Defensive Rotation",
            "horizon": "4 Wochen",
            "alternative": "Wachstum gewinnt relative Stärke zurück.",
            "evidence": ["Sektorrelative Stärke"],
            "counter_evidence": ["Renditeanstieg"],
            "confidence": "mittel",
            "confirm_triggers": ["Defensive Stärke hält."],
            "invalidate_triggers": ["Wachstum outperformt."],
            "event_dependencies": [],
            "asset_classes": ["Aktien"],
            "candidates": [],
            "status": "NEU",
            "replaced_by": None,
        }],
    }
    def thesis_report(data):
        return "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(data) + "\n```"

    with tempfile.TemporaryDirectory() as tmp:
        register_path = str(Path(tmp) / "register.json")
        first = update_register(thesis_report(thesis_payload), register_path)
        assert first["comparison_status"] == "BASELINE_MISSING"
        thesis_payload["theses"][0]["status"] = "BESTAETIGT"
        second = update_register(thesis_report(thesis_payload), register_path)
        assert second["comparison_status"] == "PASS"
        evidence = production._trading_radar_thesenregister_evidence_lines(second)
        report = production._trading_radar_thesenregister_nachweis_text(report_fixture(), evidence)
        errors = gate.validate_report(report)
        assert not any("M-05" in error for error in errors), errors
        assert "VORHER-THESIS-ID: T-1" in report
        assert "VORHER-REGISTER-VERSION: 1" in report
        assert "THESIS_ID: T-1" in report
        assert "REGISTER-VERSION: 2" in report
        assert "THESENREGISTER-VERGLEICH-ID:" in report


def test_m05_bootstrap_snapshot_is_never_misreported_as_comparison_pass():
    import json
    import tempfile
    from trading_radar_thesen import update_register
    import Trading_Radar_Alleinauswertung as production

    data = {
        "data_cutoff": "2026-10-30T00:00:00+00:00",
        "theses": [{
            "thesis_id": "T-BOOTSTRAP", "thesis": "These", "horizon": "4 Wochen",
            "alternative": "Alternative", "evidence": ["Beleg"], "counter_evidence": ["Gegenbeleg"],
            "confidence": "mittel", "confirm_triggers": ["Trigger"], "invalidate_triggers": ["Invalidierung"],
            "event_dependencies": [], "asset_classes": ["Aktien"], "candidates": [], "status": "NEU",
            "replaced_by": None,
        }],
    }
    with tempfile.TemporaryDirectory() as tmp:
        result = update_register(
            "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(data) + "\n```",
            str(Path(tmp) / "register.json"),
        )
        evidence = production._trading_radar_thesenregister_evidence_lines(result)
        report = production._trading_radar_thesenregister_nachweis_text(report_fixture(), evidence)
        errors = gate.validate_report(report)
        assert any("M-05" in error for error in errors), errors

def test_m05_accepts_unicode_thesis_ids_in_register_rows_and_change_lists():
    unicode_id = "makro-zinsen-liquidität-2026-11"
    report = report_fixture()
    report = report.replace("T-1", unicode_id)
    errors = gate.validate_report(report)
    assert not any("M-05" in error for error in errors), errors


def test_m07_accepts_unicode_thesis_id_in_compact_identity_row():
    unicode_id = "makro-zinsen-liquidität-2026-11"
    report = report_fixture()
    report = report.replace(
        "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
        f"- {unicode_id} | Version 1 | Status BESTÄTIGT",
    )
    errors = gate.validate_report(report)
    assert not any("M-07" in error for error in errors), errors



def test_m08_treats_combined_time_zone_label_as_one_field_in_one_event():
    from trading_radar_market_first import normalize_event_section
    report = report_fixture().replace(
        "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
        "- Ereignis: ADP Weekly Employment Change\n"
        "  - Datum: 2026-11-17\n"
        "  - Quelle: ADP official calendar\n"
        "  - Uhrzeit/Zeitzone: NICHT VERFÜGBAR\n"
        "  - Kennzahl: ADP Beschäftigungsänderung\n"
        "  - Berichtszeitraum: November 2026\n"
        "  - Letzter Wert: NICHT VERFÜGBAR\n"
        "  - Konsens: NICHT VERFÜGBAR.",
    )
    normalized, findings = normalize_event_section(report)
    assert findings == []
    errors = gate.validate_report(normalized)
    m08 = [error for error in errors if "M-08:" in error]
    assert len(m08) == 0, errors


def test_m03_generator_and_gate_reject_contradictory_ohlcv_claims():
    import Trading_Radar_Alleinauswertung as production
    audit = {
        "status": "gaps",
        "checked_at": "2026-11-16T12:00:00Z",
        "universe_count": 10,
        "covered_count": 10,
        "missing_tickers": ["AAA"],
        "stale_tickers": [],
        "recent_gap_tickers": [],
        "historical_only_gap_tickers": [],
        "calendar_unverified_tickers": [],
        "calendar_coverage_incomplete_tickers": [],
        "affected_candidates": ["AAA"],
        "quality_status_counts": {"VALID": 9, "MISSING": 1, "STALE": 0, "INVALID": 0, "TOTAL": 10},
        "quality_status_by_ticker": {
            **{f"T{i}": "VALID" for i in range(9)},
            "AAA": "MISSING",
        },
        "candidate_mapping_complete": True,
        "candidate_impact": "affected_candidates_documented",
    }
    previous = getattr(production, "_TRADING_RADAR_OHLCV_AUDIT", None)
    production._TRADING_RADAR_OHLCV_AUDIT = audit
    try:
        source_report = report_fixture(ohlcv_warning=True).replace(
            "Basisszenario: Seitwärts bis aufwärts.",
            "OHLCV vollständig, keine Datenlücken; Kandidaten nicht eingeschränkt.\\n"
            "Basisszenario: Seitwärts bis aufwärts.",
            1,
        )
        assert any(
            "Freitext behauptet Datenlückenfreiheit" in error
            for error in gate.validate_report(source_report)
        )
        generated = production._trading_radar_ohlcv_abschnitt8_aktualisieren(source_report)
    finally:
        if previous is None:
            delattr(production, "_TRADING_RADAR_OHLCV_AUDIT")
        else:
            production._TRADING_RADAR_OHLCV_AUDIT = previous
    assert "OHLCV vollständig, keine Datenlücken" not in generated
    assert "OHLCV-Abdeckung: vollständig" not in generated
    errors = gate.validate_report(generated)
    assert not any("Freitext behauptet Datenlückenfreiheit" in error for error in errors), errors
    contradictory = generated.replace(
        "Kandidaten-Mapping: VOLLSTÄNDIG",
        "OHLCV vollständig, keine Datenlücken; Kandidaten nicht eingeschränkt.\nKandidaten-Mapping: VOLLSTÄNDIG",
        1,
    )
    errors = gate.validate_report(contradictory)
    assert any("Freitext behauptet Datenlückenfreiheit" in error for error in errors), errors


def test_m08_splits_date_led_legacy_events_and_keeps_combined_time_zone_as_field():
    from trading_radar_market_first import normalize_event_section
    event_body = (
        "- Datum: 2026-11-17\n"
        "- Uhrzeit/Zeitzone: NICHT VERFÜGBAR\n"
        "- Kennzahl: ADP Weekly Employment Change\n"
        "- Zeitraum: Wöchentlich\n"
        "- Letzter Wert: NICHT VERFÜGBAR\n"
        "- Konsens: NICHT VERFÜGBAR\n"
        "- Quelle: ADP official calendar\n"
        "- Datum: 2026-11-19\n"
        "- Uhrzeit/Zeitzone: NICHT VERFÜGBAR\n"
        "- Kennzahl: Unemployment Claims\n"
        "- Zeitraum: Wöchentlich\n"
        "- Letzter Wert: 200000\n"
        "- Konsens: NICHT VERFÜGBAR\n"
        "- Quelle: ForexFactory (CACHE)\n"
    )
    report = report_fixture().replace(
        "Uhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
        event_body,
    )
    normalized, findings = normalize_event_section(report)
    assert len([x for x in findings if "Ereignis ohne verifizierte Quelle" in x]) == 1
    assert normalized.count("Datum: 2026-11-17") == 1
    assert normalized.count("Datum: 2026-11-19") == 1
    errors = gate.validate_report(normalized)
    assert not any("M-08:" in error for error in errors), errors


def test_m08_keeps_event_clock_records_and_consensus_scenarios_inside_each_event():
    event_section = (
        "3. Ereignisradar\n"
        "- Ereignis-Uhrzeit: ADP Weekly Employment Change (2026-11-17)\n"
        "  - Uhrzeit: 08:15\n"
        "  - Zeitzone: UTC-05:00\n"
        "  - Kennzahl: ADP Weekly Employment Change\n"
        "  - Zeitraum: Wöchentlich\n"
        "  - Datum: 2026-11-17\n"
        "  - Letzter Wert: NICHT VERFÜGBAR\n"
        "  - Konsens: NICHT VERFÜGBAR\n"
        "  - Quelle: ForexFactory LIVE adapter (https://nfs.faireconomy.media/ff_calendar_thisweek.json)\n"
        "  - Unter Konsens: Nachlassen des Lohndrucks\n"
        "  - Datum: NICHT VERFÜGBAR (Szenariofeld, kein eigener Termin)\n"
        "- Ereignis-Uhrzeit: Unemployment Claims (2026-11-19)\n"
        "  - Uhrzeit: 08:30\n"
        "  - Zeitzone: UTC-05:00\n"
        "  - Kennzahl: Unemployment Claims (Initial Jobless Claims)\n"
        "  - Zeitraum: Wöchentlich\n"
        "  - Datum: 2026-11-19\n"
        "  - Letzter Wert: 200000\n"
        "  - Konsens: 198K\n"
        "  - Quelle: ForexFactory LIVE adapter (https://nfs.faireconomy.media/ff_calendar_thisweek.json)\n"
        "  - Entsprechend Konsens: Keine Überraschung\n"
        "  - Datum: NICHT VERFÜGBAR (Szenariofeld, kein eigener Termin)\n"
    )
    report = report_fixture().replace(
        "3. Ereignisradar\nUhrzeit: 14:30 | Zeitzone: ET | Kennzahl: CPI | Berichtszeitraum: Q3 2026 | letzter Wert: 2.8 | Konsens: 2.9.",
        event_section.rstrip(),
        1,
    )

    errors = gate.validate_report(report)

    assert not any(error.startswith("M-08:") for error in errors), errors


def test_m03_does_not_misclassify_unrelated_macro_gap_statement_as_ohlcv_claim():
    text = report_fixture(ohlcv_warning=True).replace(
        "Basisszenario: Seitwärts bis aufwärts.",
        "Für die Makrozeitreihe liegen keine Datenlücken vor.\n"
        "Basisszenario: Seitwärts bis aufwärts.",
        1,
    )
    errors = gate.validate_report(text)
    assert not any("Freitext behauptet Datenlückenfreiheit" in error for error in errors), errors


def test_m02_rejects_old_observation_with_recent_retrieval_timestamp():
    text = report_fixture().replace("Datenstichtag: 2026-10-30; Einheit: Indexpunkte", "Datenstichtag: 2026-10-20; Einheit: Indexpunkte", 1)
    text = text.replace("Marktregime-Datenstichtag: 2026-10-30", "Marktregime-Datenstichtag: 2026-10-20", 1)
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_rejects_retrieval_before_observation_date():
    text = report_fixture().replace("Quelle: Yahoo Finance (^VIX); Abrufzeit: 2026-10-30T21:20:00Z", "Quelle: Yahoo Finance (^VIX); Abrufzeit: 2026-10-30T20:00:00Z", 1)
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_rejects_missing_timezone_on_retrieval_timestamp():
    text = report_fixture().replace("Quelle: Yahoo Finance (^VIX); Abrufzeit: 2026-10-30T21:20:00Z", "Quelle: Yahoo Finance (^VIX); Abrufzeit: 2026-10-30T04:30:00", 1)
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_rejects_observation_date_in_future_of_report_run():
    text = report_fixture().replace("Marktregime-Datenstichtag: 2026-10-30", "Marktregime-Datenstichtag: 2026-10-31", 1)
    text = text.replace("Datenstichtag: 2026-10-30; Einheit: Indexpunkte", "Datenstichtag: 2026-10-31; Einheit: Indexpunkte")
    text = text.replace("Abrufzeit: 2026-10-30T21:20:00Z", "Abrufzeit: 2026-10-31T04:30:00Z")
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_rejects_duplicate_vix_live_provenance_lines():
    text = report_fixture()
    line = next(line for line in text.splitlines() if "Volatilität VIX:" in line)
    text = text.replace(line + "\n", line + "\n" + line + "\n", 1)
    errors = gate.validate_report(text)
    assert any("genau eine Live-Provenienzzeile für VIX" in item for item in errors), errors


def test_m02_rejects_duplicate_numeric_values_in_canonical_risk_field():
    text = report_fixture().replace(
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00; Liquiditätslage stabil.",
        "Dollar/Liquidität/Volatilität: VIX: 18,00; VIX: 18,10; DXY: 100,00; Liquiditätslage stabil.", 1,
    )
    errors = gate.validate_report(text)
    assert any("genau eine numerische VIX-Beobachtung" in item for item in errors), errors


def test_m02_rejects_same_day_utc_rollover_before_market_close():
    text = report_fixture().replace(
        "Datenstand UTC: 2026-10-30T21:30:00Z",
        "Datenstand UTC: 2026-10-30T04:30:00Z",
    ).replace(
        "Abrufzeit: 2026-10-30T21:20:00Z",
        "Abrufzeit: 2026-10-30T04:20:00Z",
    )
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_accepts_last_completed_friday_close_on_sunday_after_utc_midnight():
    text = report_fixture()
    text = text.replace("2026-10-30", "2026-11-20")
    text = text.replace("2026-11-20T21:30:00Z", "2026-11-22T09:02:00Z")
    text = text.replace("2026-11-20T21:20:00Z", "2026-11-22T09:01:00Z")
    assert not any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in gate.validate_report(text))


def test_m02_requires_both_source_closes_and_handles_dst_and_ice_holidays():
    from datetime import datetime, date
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-10-30T20:30:00+00:00")) == date(2026, 10, 29)
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-10-30T21:01:00+00:00")) == date(2026, 10, 30)
    # After the US DST switch, ICE's 17:00 ET close is 22:00 UTC.
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-11-02T21:30:00+00:00")) == date(2026, 10, 30)
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-11-02T22:01:00+00:00")) == date(2026, 11, 2)
    # Thanksgiving: VIX is closed while ICE currency/index contracts are a session.
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-11-26T22:30:00+00:00")) == date(2026, 11, 25)
    # Cboe's published early close on Nov 27 is 13:00 ET; ICE is not
    # considered complete until its regular 17:00 ET close.
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-11-27T18:01:00+00:00")) == date(2026, 11, 26)
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2026-11-27T22:01:00+00:00")) == date(2026, 11, 27)


def test_m02_fails_closed_outside_verified_calendar_window():
    from datetime import datetime
    assert gate._m02_expected_common_session_date(datetime.fromisoformat("2028-01-04T22:30:00+00:00")) is None



def test_m02_accepts_explicit_uup_dollar_etf_proxy_when_spot_dxy_is_unavailable():
    text = report_fixture().replace(
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00; Liquiditätslage stabil.",
        "Dollar/Liquidität/Volatilität: VIX: 18,00; Dollarproxy: 28,50; Liquiditätslage stabil.",
        1,
    ).replace(
        "- Dollar-Proxy DXY (DX-Y.NYB): 100.00 | Quelle: Yahoo Finance (DX-Y.NYB); Abrufzeit: 2026-10-30T21:20:00Z; Datenstichtag: 2026-10-30; Einheit: Indexpunkte; Verzögerung: Tages-Schlusskurs.",
        "- Dollar-Proxy UUP (US-Dollar-ETF): 28.50 | Quelle: Yahoo Finance (UUP); Abrufzeit: 2026-10-30T21:20:00Z; Datenstichtag: 2026-10-30; Einheit: USD je Anteil; Verzögerung: Tages-Schlusskurs.",
        1,
    )
    errors = gate.validate_report(text)
    assert not any(item.startswith("M-02:") for item in errors), errors



def test_m02_accepts_dx_f_as_explicit_dollar_index_futures_proxy():
    text = report_fixture().replace(
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00; Liquiditätslage stabil.",
        "Dollar/Liquidität/Volatilität: VIX: 18,00; Dollarproxy: 105,50; Liquiditätslage stabil.",
        1,
    ).replace(
        "- Dollar-Proxy DXY (DX-Y.NYB): 100.00 | Quelle: Yahoo Finance (DX-Y.NYB); Abrufzeit: 2026-10-30T21:20:00Z; Datenstichtag: 2026-10-30; Einheit: Indexpunkte; Verzögerung: Tages-Schlusskurs.",
        "- Dollar-Proxy DX-Future (DX=F): 105.50 | Quelle: Yahoo Finance (DX=F); Abrufzeit: 2026-10-30T21:20:00Z; Datenstichtag: 2026-10-30; Einheit: Indexpunkte; Verzögerung: Tages-Schlusskurs.",
        1,
    )
    errors = gate.validate_report(text)
    assert not any(item.startswith("M-02:") for item in errors), errors

def test_m04_accepts_explicit_exact_cutoff_data_gap_in_each_matrix_group():
    text = report_fixture()
    for group in ("Indizes", "Regionen", "Sektoren", "Anlageklassen"):
        text = re.sub(
            rf"(?m)^{group}: 1M/3M NICHT VERFÜGBAR \(Datenlücke: keine vergleichbaren Reihen\)\.$",
            f"{group}: NICHT VERFÜGBAR (Datenlücke: kein Kurs exakt am gemeinsamen Stichtag).",
            text,
            count=1,
        )
    errors = gate.validate_report(text)
    assert not any(item.startswith("M-04:") for item in errors), errors


def test_m08_pipe_delimited_event_header_and_auswirkung_remain_one_record():
    text = report_fixture()
    start = text.index("3. Ereignisradar")
    end = text.index("4. Assetklassenvergleich", start)
    event_section = (
        "3. Ereignisradar\n"
        "* ADP Weekly Employment Change | 2026-11-24 | 14:15 MEZ | "
        "Erwartung: NICHT VERFÜGBAR | Quelle: ForexFactory\n"
        "- Datum: 2026-11-24\n"
        "- Quellenprüfung: LIVE_VERIFIZIERT (ForexFactory; Datum und Ereignisidentität abgeglichen)\n"
        "Uhrzeit: 14:15\n"
        "Zeitzone: Europe/Berlin\n"
        "Kennzahl: NICHT VERFÜGBAR\n"
        "Berichtszeitraum: NICHT VERFÜGBAR\n"
        "Letzter Wert: NICHT VERFÜGBAR\n"
        "Konsens: NICHT VERFÜGBAR\n"
        "* Auswirkung: Volatilität bei Zins-Assets; keine zweite Ereignisidentität.\n\n"
    )
    text = text[:start] + event_section + text[end:]
    errors = gate.validate_report(text)
    assert not any(item.startswith("M-08:") for item in errors), errors

def test_m05_thesis_identity_uses_shared_placeholder_rules_without_changing_m07_parser():
    text = report_fixture().replace("Thesen-ID: T-1", "Thesen-ID: NICHT ANGEGEBEN", 1)
    errors = gate.validate_report(text)
    assert any(item.startswith("M-05") for item in errors), errors
    assert not any(item.startswith("M-07") for item in errors), errors
    # The old M-07 parser still treats this annotated phrase exactly as before;
    # only the separate M-05 parser applies the shared extended placeholder set.
    assert gate._label_value(
        "Thesen-ID: NICHT VERFÜGBAR (keine ID)", (r"Thesen-ID",)
    ) == "NICHT VERFÜGBAR (keine ID)"
    assert gate._m05_label_value(
        "Thesen-ID: NICHT VERFÜGBAR (keine ID)", (r"Thesen-ID",)
    ) is None


def test_m02_rejects_duplicate_canonical_risk_fields():
    text = report_fixture().replace(
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00; Liquiditätslage stabil.",
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00; Liquiditätslage stabil.\n"
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00.", 1,
    )
    errors = gate.validate_report(text)
    assert any("kanonisches Dollar/Liquidität/Volatilität-Feld muss genau einmal" in item for item in errors), errors


def test_m02_rejects_observation_date_different_from_market_regime_cutoff():
    text = report_fixture().replace("Datenstichtag: 2026-10-30; Einheit: Indexpunkte", "Datenstichtag: 2026-10-29; Einheit: Indexpunkte", 1)
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_rejects_missing_retrieval_timestamp():
    text = report_fixture().replace("Abrufzeit: 2026-10-30T21:20:00Z; ", "", 1)
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m02_rejects_missing_utc_run_timestamp():
    text = report_fixture().replace("- Datenstand UTC: 2026-10-30T21:30:00Z\n", "", 1)
    errors = gate.validate_report(text)
    assert any("timezone-behafteter UTC-Report-/Run-Zeitstempel fehlt" in item for item in errors), errors


def test_m02_rejects_live_snapshot_value_that_disagrees_with_canonical_risk_field():
    text = report_fixture().replace("Volatilität VIX: 18.00", "Volatilität VIX: 18.25", 1)
    errors = gate.validate_report(text)
    assert any("M-02: VIX-Beobachtung ist nicht frisch" in item for item in errors), errors


def test_m05_gate_applies_shared_placeholder_rules_to_structured_identity_without_changing_m07():
    import json
    text = report_fixture().replace("T-1", "NICHT ANGEGEBEN")
    payload = {
        "schema_version": 1,
        "theses": [{
            "thesis_id": "NICHT ANGEGEBEN",
            "version": 2,
            "status": "BESTÄTIGT",
            "alternative": "Eine plausible Gegenhypothese",
        }],
    }
    text = text.replace(
        "VERSION: 2",
        "VERSION: 2\n```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```",
        1,
    )
    errors = gate.validate_report(text)
    assert any(item.startswith("M-05") for item in errors), errors
    # M-07 continues to use its original parser and predicate.
    assert not any(item.startswith("M-07") for item in errors), errors


def test_m05_and_m07_accept_multiword_canonical_compact_statuses():
    text = report_fixture().replace(
        "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
        "Thesen-ID: T-1\nStatus: BESTÄTIGT\n"
        "Thesenregister – kompakte Übersicht:\n"
        "- T-1 | Version 2 | Status BESTÄTIGT | Konfidenz mittel\n"
        "- T-2 | Version 1 | Status WARTET AUF EREIGNIS | Konfidenz mittel",
        1,
    )
    text = text.replace("VORHER-REGISTER-VERSION: 1", "VORHER-REGISTER-VERSION: NICHT VERFÜGBAR")
    text = text.replace("REGISTER-VERSION: 2", "REGISTER-VERSION: NICHT VERFÜGBAR")
    text = text.replace("VERSION: 2", "VERSION: NICHT VERFÜGBAR")
    errors = gate.validate_report(text)
    assert not any("Thesen-Update benötigt" in item for item in errors), errors
    # Both validators now use the same canonical multiword lifecycle status.
    assert not any(item.startswith("M-07") for item in errors), errors


def test_m05_compact_register_rejects_placeholder_identity_values():
    for placeholder in (
        "NICHT ANGEGEBEN", "NICHT ERMITTELT", "NICHT VERFÜGBAR (keine ID)",
        "UNBEKANNT", "TBD", "N/A",
    ):
        text = report_fixture().replace(
            "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
            "Thesen-ID: " + placeholder + "\nStatus: BESTÄTIGT\n"
            "Thesenregister – kompakte Übersicht:\n"
            "- " + placeholder + " | Version 2 | Status BESTÄTIGT | Konfidenz mittel",
            1,
        )
        errors = gate.validate_report(text)
        assert any("Thesen-Update benötigt" in item for item in errors), (placeholder, errors)


def test_m05_accepts_fully_proven_archive_only_register_without_active_thesis():
    text = (
        "2. Thesen-Update\n"
        "Thesen-ID: T-1 | Version: 1 | Status: INVALIDIERT. Alle Thesen sind archiviert; keine aktive These verbleibt.\n"
        "THESENREGISTER-VERGLEICH: PASS\n"
        "VORHER-SHA256: " + "a" * 64 + "\n"
        "NACHHER-SHA256: " + "b" * 64 + "\n"
        "THESENREGISTER-VERGLEICH-ID: 03ad73fb6916f8c1d10706e17fd4e1a260ead1c02fb21797d8316756cc4f0c96\n"
        "VORHER_THESEN_COUNT: 1\nVORHER_AKTIVE_THESEN: 1\nNACHHER_THESEN_COUNT: 1\nNACHHER_AKTIVE_THESEN: 0\n"
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "THESEN_NEU: KEINE\n"
        "THESEN_AKTUALISIERT: KEINE\n"
        "THESEN_UNVERAENDERT: KEINE\n"
        "THESEN_ARCHIVIERT: T-1\n"
        "THESIS_ID: T-1\nREGISTER-VERSION: 1\nTHESIS_ACTIVE: NO\n"
        "THESIS_STATUS: INVALIDIERT\nTHESIS_REPLACED_BY: KEINE\n"
        "7. MSCI-Vergleich\n"
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert not any(error.startswith("M-05:") for error in errors), errors


def test_m05_does_not_accept_archive_only_register_with_inconsistent_change_list():
    text = (
        "2. Thesen-Update\n"
        "Thesen-ID: T-1 | Version: 1 | Status: INVALIDIERT. Alle Thesen sind archiviert; keine aktive These verbleibt.\n"
        "THESENREGISTER-VERGLEICH: PASS\n"
        "VORHER-SHA256: " + "a" * 64 + "\n"
        "NACHHER-SHA256: " + "b" * 64 + "\n"
        "THESENREGISTER-VERGLEICH-ID: 03ad73fb6916f8c1d10706e17fd4e1a260ead1c02fb21797d8316756cc4f0c96\n"
        "VORHER_THESEN_COUNT: 1\nVORHER_AKTIVE_THESEN: 1\nNACHHER_THESEN_COUNT: 1\nNACHHER_AKTIVE_THESEN: 0\n"
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "THESEN_NEU: KEINE\n"
        "THESEN_AKTUALISIERT: KEINE\n"
        "THESEN_UNVERAENDERT: KEINE\n"
        "THESEN_ARCHIVIERT: KEINE\n"
        "THESIS_ID: T-1\nREGISTER-VERSION: 1\nTHESIS_ACTIVE: NO\n"
        "7. MSCI-Vergleich\n"
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert any(error.startswith("M-05:") for error in errors), errors


def test_scenario_entry_conditions_are_checked_against_indicator_values():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenarios = (
        "Basisszenario: Makro bleibt robust. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf. "
        "Status: NICHT ERFÜLLT. Bestätigungstrigger: beide Sektoren positiv. "
        "Invalidierungstrigger: Marktbreite unter 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; "
        "Energie-ETF (XLE) 3M -3.55%. Konsequenz: beobachten.\n"
        "Positives Alternativszenario: Rotation setzt sich fort. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und 3M-Relative-Stärke Gesundheit-ETF (XLV) und Energie-ETF (XLE) jeweils über 0,00%. "
        "Status: NICHT ERFÜLLT. Bestätigungstrigger: beide Werte positiv. "
        "Invalidierungstrigger: einer der Werte nicht positiv. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; "
        "Energie-ETF (XLE) 3M -3.55%. Konsequenz: abwarten.\n"
        "Negatives Alternativszenario: Sektorrotation schwächt sich ab. Eintrittsbedingung: Marktbreite fällt unter 3/11 "
        "oder Gesundheit-ETF (XLV) bzw. Energie-ETF (XLE) weist eine nicht-positive 3M-Relative-Stärke auf. "
        "Status: TRIGGER AUSGELÖST. Bestätigungstrigger: nächster gemeinsamer Stichtag. "
        "Invalidierungstrigger: beide Sektoren wieder positiv. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; "
        "Energie-ETF (XLE) 3M -3.55%. Konsequenz: Risiko prüfen."
    )
    report = report[:start] + scenarios + report[end:]
    errors = gate.validate_report(report)
    assert not [item for item in errors if "M-09-SZENARIO" in item], errors


def test_scenario_gate_rejects_positive_baseline_when_one_required_sector_is_negative():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenarios = (
        "Basisszenario: Zyklische Rotation. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf. "
        "Status: ERFÜLLT. Bestätigungstrigger: beide positiv. Invalidierungstrigger: einer negativ. "
        "Zeithorizont: 1 bis 3 Monate. Indikatoren: Marktbreite 3/11; "
        "Gesundheit-ETF (XLV) 3M +1.84%; Energie-ETF (XLE) 3M -3.55%. Konsequenz: aktiv.\n"
        "Positives Alternativszenario: Stärke. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) positiv. Status: NICHT ERFÜLLT. "
        "Bestätigungstrigger: beide positiv. Invalidierungstrigger: Werte negativ. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; Energie-ETF (XLE) 3M -3.55%. Konsequenz: warten.\n"
        "Negatives Alternativszenario: Schwäche. Eintrittsbedingung: Energie-ETF (XLE) weist eine nicht-positive 3M-Relative-Stärke auf. "
        "Status: TRIGGER AUSGELÖST. Bestätigungstrigger: Wert bleibt negativ. Invalidierungstrigger: Wert positiv. "
        "Zeithorizont: 1 bis 3 Monate. Indikatoren: Energie-ETF (XLE) 3M -3.55%. Konsequenz: defensiv."
    )
    report = report[:start] + scenarios + report[end:]
    errors = gate.validate_report(report)
    assert any("Basisszenario widerspricht den Messwerten" in item for item in errors), errors


def test_scenario_gate_requires_status_when_entry_condition_is_measurable():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenarios = (
        "Basisszenario: Zyklische Rotation. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf. "
        "Bestätigungstrigger: beide positiv. Invalidierungstrigger: einer negativ. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; Energie-ETF (XLE) 3M -3.55%. Konsequenz: aktiv."
    )
    report = report[:start] + scenarios + report[end:]
    errors = gate.validate_report(report)
    assert any("benötigt einen expliziten Status" in item for item in errors), errors


def test_m05_rejects_archiving_active_thesis_without_terminal_status():
    text = (
        "2. Thesen-Update\nThesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT.\n"
        "THESENREGISTER-VERGLEICH: PASS\n"
        "VORHER-SHA256: " + "a" * 64 + "\nNACHHER-SHA256: " + "b" * 64 + "\n"
        "THESENREGISTER-VERGLEICH-ID: " + hashlib.sha256(json.dumps({
            "previous_sha256": "a" * 64, "current_sha256": "b" * 64,
            "schema": "trading-radar-thesis-register-comparison-v1"
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest() + "\n"
        "VORHER_THESEN_COUNT: 1\nVORHER_AKTIVE_THESEN: 1\nNACHHER_THESEN_COUNT: 1\nNACHHER_AKTIVE_THESEN: 0\n"
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "THESEN_NEU: KEINE\nTHESEN_AKTUALISIERT: KEINE\nTHESEN_UNVERAENDERT: KEINE\nTHESEN_ARCHIVIERT: T-1\n"
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: NO\n"
        "THESIS_STATUS: BESTÄTIGT\nTHESIS_REPLACED_BY: KEINE\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert any("ohne expliziten Status INVALIDIERT/ERSETZT" in item for item in errors), errors


def test_m05_rejects_new_id_marked_as_already_confirmed():
    text = report_fixture().replace(
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: YES\n"
        "THESIS_STATUS: BESTÄTIGT\nTHESIS_REPLACED_BY: KEINE",
        "THESIS_ID: T-2\nREGISTER-VERSION: 1\nTHESIS_ACTIVE: YES\n"
        "THESIS_STATUS: BESTÄTIGT\nTHESIS_REPLACED_BY: KEINE",
    ).replace(
        "THESEN_AKTUALISIERT: T-1", "THESEN_NEU: T-2"
    ).replace(
        "THESEN_NEU: KEINE\nTHESEN_AKTUALISIERT: T-1", "THESEN_NEU: T-2\nTHESEN_AKTUALISIERT: KEINE"
    )
    errors = gate.validate_report(text)
    assert any("Neue These T-2 muss mit explizitem Status NEU" in item for item in errors), errors


def test_scenario_gate_requires_nicht_ableitbar_when_required_indicator_is_missing():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenario = (
        "Basisszenario: Zyklische Rotation. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf. "
        "Status: ERFÜLLT. Bestätigungstrigger: beide Sektoren positiv. Invalidierungstrigger: einer negativ. "
        "Zeithorizont: 1 bis 3 Monate. Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%. "
        "Konsequenz: aktiv."
    )
    report = report[:start] + scenario + report[end:]
    errors = gate.validate_report(report)
    assert any("Indikatorwerte fehlen" in item for item in errors), errors


def test_scenario_gate_requires_a_valid_common_cutoff_when_explicitly_declared():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenarios = (
        "Basisszenario: Zyklische Rotation. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf. "
        "Status: ERFÜLLT. Bestätigungstrigger: beide Sektoren positiv. Invalidierungstrigger: einer negativ. "
        "Zeithorizont: 1 bis 3 Monate. Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; "
        "Energie-ETF (XLE) 3M -3.55%; Gemeinsamer Datenstichtag: NICHT VERFÜGBAR. "
        "Datenlücke: gemeinsamer Datenstichtag fehlt. Konsequenz: beobachten.\n"
        "Positives Alternativszenario: Stärke. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf. "
        "Status: ERFÜLLT. Bestätigungstrigger: beide positiv. Invalidierungstrigger: einer negativ. "
        "Zeithorizont: 1 bis 3 Monate. Indikatoren: Marktbreite 3/11; Gesundheit-ETF (XLV) 3M +1.84%; "
        "Energie-ETF (XLE) 3M -3.55%; Gemeinsamer Datenstichtag: NICHT VERFÜGBAR. "
        "Datenlücke: gemeinsamer Datenstichtag fehlt. Konsequenz: warten.\n"
        "Negatives Alternativszenario: Schwäche. Eintrittsbedingung: Gesundheit-ETF (XLV) bzw. "
        "Energie-ETF (XLE) weist eine nicht-positive 3M-Relative-Stärke auf. "
        "Status: TRIGGER AUSGELÖST. Bestätigungstrigger: Wert bleibt negativ. Invalidierungstrigger: Wert positiv. "
        "Zeithorizont: 1 bis 3 Monate. Indikatoren: Gesundheit-ETF (XLV) 3M +1.84%; "
        "Energie-ETF (XLE) 3M -3.55%; Gemeinsamer Datenstichtag: NICHT VERFÜGBAR. "
        "Datenlücke: gemeinsamer Datenstichtag fehlt. Konsequenz: defensiv."
    )
    report = report[:start] + scenarios + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert len([item for item in errors if "Datenbasis unvollständig" in item]) == 3, errors

def test_m05_rejects_existing_thesis_marked_neu():
    report = report_fixture().replace(
        "THESIS_STATUS: BESTÄTIGT",
        "THESIS_STATUS: NEU",
    )
    errors = gate.validate_report(report)
    assert any("Bestehende These T-1 darf nicht erneut Status NEU tragen" in item for item in errors), errors


def test_negative_alternative_requires_trigger_ausgeloest_when_condition_is_true():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenarios = (
        "Basisszenario: Wachstum bleibt robust. Eintrittsbedingung: Marktbreite mindestens 3/11. "
        "Status: ERFÜLLT. Bestätigungstrigger: Marktbreite bleibt mindestens 3/11. "
        "Invalidierungstrigger: Marktbreite fällt unter 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten.\n"
        "Positives Alternativszenario: Rotation setzt sich fort. Eintrittsbedingung: Marktbreite mindestens 3/11. "
        "Status: ERFÜLLT. Bestätigungstrigger: Marktbreite bleibt mindestens 3/11. "
        "Invalidierungstrigger: Marktbreite fällt unter 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten.\n"
        "Negatives Alternativszenario: Breite schwächt sich ab. Eintrittsbedingung: Marktbreite fällt unter 4/11. "
        "Status: ERFÜLLT. Bestätigungstrigger: Marktbreite bleibt unter 4/11. "
        "Invalidierungstrigger: Marktbreite erreicht mindestens 4/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: defensiv."
    )
    report = report[:start] + scenarios + report[end:]
    errors = gate.validate_report(report)
    assert any("erwarteter Status=TRIGGER AUSGELÖST" in item for item in errors), errors


def test_breadth_over_threshold_is_strict_not_greater_or_equal():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\n\n2. Thesen-Update", start)
    scenarios = (
        "Basisszenario: Marktbreite verbessert sich. Eintrittsbedingung: Marktbreite über 3/11. "
        "Status: ERFÜLLT. Bestätigungstrigger: Marktbreite bleibt über 3/11. "
        "Invalidierungstrigger: Marktbreite fällt auf 3/11 oder darunter. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten.\n"
        "Positives Alternativszenario: Marktbreite verbessert sich. Eintrittsbedingung: Marktbreite mindestens 3/11. "
        "Status: ERFÜLLT. Bestätigungstrigger: Marktbreite bleibt mindestens 3/11. "
        "Invalidierungstrigger: Marktbreite fällt unter 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten.\n"
        "Negatives Alternativszenario: Marktbreite fällt. Eintrittsbedingung: Marktbreite fällt unter 3/11. "
        "Status: NICHT ERFÜLLT. Bestätigungstrigger: Marktbreite bleibt unter 3/11. "
        "Invalidierungstrigger: Marktbreite erreicht mindestens 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten."
    )
    report = report[:start] + scenarios + report[end:]
    errors = gate.validate_report(report)
    assert any("Basisszenario widerspricht den Messwerten" in item for item in errors), errors


def test_m05_requires_status_and_successor_evidence_for_every_current_thesis():
    report = report_fixture().replace(
        "THESIS_STATUS: BESTÄTIGT\nTHESIS_REPLACED_BY: KEINE\n",
        "",
    )
    errors = gate.validate_report(report)
    assert any("THESIS_STATUS-Nachweis" in item for item in errors), errors
    assert any("THESIS_REPLACED_BY-Nachweis" in item for item in errors), errors


def test_scenario_parser_uses_and_precedence_over_or():
    indicators = "Marktbreite 3/11; XLV 3M +1.84%; XLE 3M -3.55%"
    # A OR B AND C must be A OR (B AND C), not (A OR B) AND C.
    result = gate._evaluate_scenario_entry(
        "Marktbreite mindestens 3/11 oder XLV positive 3M-Relative-Stärke "
        "und XLE positive 3M-Relative-Stärke",
        indicators,
    )
    assert result.value is True

    grouped = gate._evaluate_scenario_entry(
        "(Marktbreite mindestens 3/11 oder XLV positive 3M-Relative-Stärke) "
        "und XLE positive 3M-Relative-Stärke",
        indicators,
    )
    assert grouped.value is False


def test_scenario_parser_respects_explicit_parentheses_and_nested_logic():
    indicators = "Marktbreite 2/11; XLV 3M +1.84%; XLE 3M -3.55%"
    result = gate._evaluate_scenario_entry(
        "Marktbreite mindestens 3/11 oder "
        "(XLV positive 3M-Relative-Stärke und XLE nicht-positive 3M-Relative-Stärke)",
        indicators,
    )
    assert result.value is True


def test_scenario_parser_supports_liegt_oberhalb_von_as_strict_comparison():
    result = gate._evaluate_scenario_entry(
        "Marktbreite liegt oberhalb von 3/11",
        "Marktbreite 4/11",
    )
    assert result.value is True


def test_scenario_parser_recognizes_paired_sector_predicates_and_bzw_or():
    indicators = "Marktbreite 2/11; Gesundheit-ETF (XLV) 3M +1.84%; Energie-ETF (XLE) 3M -3.55%"
    both_positive = gate._evaluate_scenario_entry(
        "Marktbreite mindestens 3/11 oder Gesundheit-ETF (XLV) sowie "
        "Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf",
        indicators,
    )
    assert both_positive.value is False
    either_nonpositive = gate._evaluate_scenario_entry(
        "Gesundheit-ETF (XLV) bzw. Energie-ETF (XLE) weist eine "
        "nicht-positive 3M-Relative-Stärke auf",
        indicators,
    )
    assert either_nonpositive.value is True


def test_scenario_parser_returns_missing_metric_only_for_recognized_condition():
    result = gate._evaluate_scenario_entry(
        "Marktbreite mindestens 3/11 und XLV positive 3M-Relative-Stärke",
        "Marktbreite 3/11",
    )
    assert result.value is None
    assert result.missing_metrics == ("XLV",)


def test_scenario_parser_fails_closed_on_unknown_indicator_and_unknown_quantifier():
    for expression in (
        "Marktbreite mindestens 3/11 und XLF positive 3M-Relative-Stärke",
        "Marktbreite nimmt deutlich gegenüber dem Vormonat zu",
        "Marktbreite mindestens 3/11 ODER",
        "Marktbreite mindestens 3/11 und (XLV positive 3M-Relative-Stärke",
    ):
        try:
            gate._evaluate_scenario_entry(expression, "Marktbreite 3/11; XLV 3M +1.84%")
        except gate._ScenarioConditionError:
            pass
        else:
            raise AssertionError(f"Unbekannte/ungültige Bedingung wurde nicht fail-closed abgelehnt: {expression}")


def test_scenario_gate_rejects_unknown_condition_even_when_status_claims_not_ableitbar():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\nPositives Alternativszenario:", start)
    scenario = (
        "Basisszenario: Marktregime. Eintrittsbedingung: Marktbreite zeigt ein "
        "ungewöhnliches Momentum. Bestätigungstrigger: Bestätigung. Status: NICHT ABLEITBAR. "
        "Invalidierungstrigger: Invalidierung. Zeithorizont: 4 Wochen. "
        "Indikatoren: Marktbreite 8/11. Datenlücke: unbekannte Momentumdefinition. "
        "Konsequenz: beobachten."
    )
    report = report[:start] + scenario + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert any("Fail-Closed" in item for item in errors), errors


def test_m05_rejects_reactivation_of_previously_archived_thesis_id():
    report = report_fixture().replace(
        "VORHER_AKTIVE_THESEN: 1", "VORHER_AKTIVE_THESEN: 0"
    ).replace(
        "VORHER-THESIS-ACTIVE: YES", "VORHER-THESIS-ACTIVE: NO"
    )
    errors = gate.validate_report(report)
    assert any("darf nicht reaktiviert werden" in item for item in errors), errors


def test_scenario_gate_accepts_not_ableitbar_only_for_a_named_missing_metric():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\nPositives Alternativszenario:", start)
    scenario = (
        "Basisszenario: Marktregime. Eintrittsbedingung: Marktbreite mindestens 3/11 "
        "und XLV positive 3M-Relative-Stärke. Bestätigungstrigger: Datenwert nachreichen. "
        "Status: NICHT ABLEITBAR. Invalidierungstrigger: Marktbreite unter 3/11. "
        "Zeithorizont: 4 Wochen. Indikatoren: Marktbreite 8/11. "
        "Datenlücke: XLV 3M-Relative-Stärke fehlt. Konsequenz: beobachten."
    )
    report = report[:start] + scenario + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert not errors, errors


def test_scenario_parser_supports_single_sector_verb_and_article_form():
    result = gate._evaluate_scenario_entry(
        "Energie-ETF (XLE) weist eine nicht-positive 3M-Relative-Stärke auf",
        "Energie-ETF (XLE) 3M -0.25%",
    )
    assert result.value is True


def test_scenario_gate_rejects_duplicate_condition_indicator_or_status_fields():
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\nPositives Alternativszenario:", start)
    scenario = (
        "Basisszenario: Marktregime. Eintrittsbedingung: Marktbreite mindestens 3/11. "
        "Eintrittsbedingung: Marktbreite unter 3/11. Bestätigungstrigger: Bestätigung. "
        "Status: ERFÜLLT. Status: NICHT ERFÜLLT. Invalidierungstrigger: Invalidierung. "
        "Zeithorizont: 4 Wochen. Indikatoren: Marktbreite 8/11. Konsequenz: beobachten."
    )
    report = report[:start] + scenario + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert any("genau eine explizite Eintrittsbedingung" in item for item in errors)


def test_scenario_parser_uses_three_valued_logic_when_a_metric_is_missing():
    assert gate._evaluate_scenario_entry(
        "Marktbreite mindestens 3/11 oder XLV positive 3M-Relative-Stärke",
        "Marktbreite 4/11",
    ).value is True
    assert gate._evaluate_scenario_entry(
        "Marktbreite unter 3/11 und XLV positive 3M-Relative-Stärke",
        "Marktbreite 2/11",
    ).value is None
    assert gate._evaluate_scenario_entry(
        "Marktbreite unter 3/11 und XLV positive 3M-Relative-Stärke",
        "Marktbreite 4/11",
    ).value is False


def test_m07_validator_uses_canonical_lifecycle_status_contract():
    report = report_fixture().replace("Status: BESTÄTIGT", "Status: WIDERLEGT", 1)
    errors = gate.validate_report(report)
    assert any(item.startswith("M-07:") for item in errors), errors
    assert any(item.startswith("M-05:") for item in errors), errors


def test_m07_validator_normalizes_documented_ascii_status_alias():
    report = report_fixture().replace("Status: BESTÄTIGT", "Status: BESTAETIGT", 1)
    errors = gate.validate_report(report)
    assert not any(item.startswith("M-07:") for item in errors), errors


def test_m05_json_validator_requires_replaced_by_for_ersetzt_and_active_target():
    def add_json(report, records):
        start = report.index("3. Ereignisradar")
        block = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps({
            "data_cutoff": "2026-11-02T08:11:03+00:00",
            "theses": records,
        }) + "\n```\n"
        return report[:start] + block + report[start:]

    base = {
        "version": 2,
        "thesis": "Alte These",
        "horizon": "4 Wochen",
        "alternative": "Nachfolgerthese",
        "evidence": ["Beleg"],
        "counter_evidence": ["Gegenbeleg"],
        "confidence": "mittel",
        "confirm_triggers": ["Trigger"],
        "invalidate_triggers": ["Invalidierung"],
        "event_dependencies": [],
        "asset_classes": ["Aktien"],
        "candidates": [],
    }
    old_missing_link = dict(base, thesis_id="T-OLD", status="ERSETZT")
    errors = gate.validate_report(add_json(report_fixture(), [old_missing_link]))
    assert any(item.startswith("M-05:") for item in errors), errors

    old = dict(base, thesis_id="T-OLD", status="ERSETZT", replaced_by="T-NEW")
    successor = dict(base, thesis_id="T-NEW", status="NEU", replaced_by=None)
    errors = gate.validate_report(add_json(report_fixture(), [old, successor]))
    assert not any(item.startswith("M-05:") for item in errors), errors

    inactive_successor = dict(base, thesis_id="T-NEW", status="INVALIDIERT", replaced_by=None)
    errors = gate.validate_report(add_json(report_fixture(), [old, inactive_successor]))
    assert any(item.startswith("M-05:") for item in errors), errors


def test_scenario_parser_rejects_impossible_breadth_fractions():
    for expression, indicators in (
        ("Marktbreite mindestens 12/11", "Marktbreite 8/11"),
        ("Marktbreite mindestens 3/11", "Marktbreite 12/11"),
    ):
        try:
            gate._evaluate_scenario_entry(expression, indicators)
        except gate._ScenarioConditionError:
            pass
        else:
            raise AssertionError("Unmögliche Marktbreitenquote wurde akzeptiert")


@pytest.mark.parametrize(
    "invalid_status",
    [
        "ERFÜLLTBOGUS",
        "NICHT ABLEITBARXYZ",
        "TRIGGER AUSGELÖST_UNBEKANNT",
        "ERFÜLLT unbekannt",
        "ERFÜLLT. BOGUS",
        "ERFÜLLT; BOGUS",
        "ERFÜLLT, BOGUS",
    ],
)
def test_scenario_gate_rejects_status_with_unknown_suffix(invalid_status):
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\nPositives Alternativszenario:", start)
    scenario = (
        "Basisszenario: Wachstum bleibt robust. Eintrittsbedingung: Marktbreite mindestens 3/11. "
        f"Status: {invalid_status}. Bestätigungstrigger: Marktbreite bleibt mindestens 3/11. "
        "Invalidierungstrigger: Marktbreite fällt unter 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten."
    )
    report = report[:start] + scenario + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert any("benötigt einen expliziten Status genau einmal" in item for item in errors), errors


@pytest.mark.parametrize(
    "status_line",
    [
        "Status: ERFÜLLT",
        "Status: ERFÜLLT — Eintrittsbedingung anhand der genannten Messwerte erfüllt",
        "Status: ERFÜLLT (Messwerte stimmen mit der Bedingung überein)",
    ],
)
def test_scenario_gate_accepts_canonical_status_with_explicit_explanation(status_line):
    report = report_fixture()
    start = report.index("Basisszenario:")
    end = report.index("\nPositives Alternativszenario:", start)
    scenario = (
        "Basisszenario: Wachstum bleibt robust. Eintrittsbedingung: Marktbreite mindestens 3/11. "
        f"{status_line}. Bestätigungstrigger: Marktbreite bleibt mindestens 3/11. "
        "Invalidierungstrigger: Marktbreite fällt unter 3/11. Zeithorizont: 1 bis 3 Monate. "
        "Indikatoren: Marktbreite 3/11. Konsequenz: beobachten."
    )
    report = report[:start] + scenario + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert not any("benötigt einen expliziten Status genau einmal" in item for item in errors), errors



@pytest.mark.parametrize(
    ("breadth", "xlv", "and_expected", "or_expected"),
    [
        ("4/11", "+1.0%", True, True),
        ("4/11", "-1.0%", False, True),
        ("2/11", "+1.0%", False, True),
        ("2/11", "-1.0%", False, False),
    ],
)
def test_scenario_parser_truth_table_for_and_and_or(breadth, xlv, and_expected, or_expected):
    indicators = f"Marktbreite {breadth}; XLV 3M {xlv}"
    atom_a = "Marktbreite mindestens 3/11"
    atom_b = "XLV positive 3M-Relative-Stärke"
    assert gate._evaluate_scenario_entry(f"{atom_a} UND {atom_b}", indicators).value is and_expected
    assert gate._evaluate_scenario_entry(f"{atom_a} ODER {atom_b}", indicators).value is or_expected


def test_scenario_parser_truth_table_confirms_and_has_precedence_over_or():
    # A is true, B and C are false. A OR (B AND C) must be true;
    # a left-to-right fold (A OR B) AND C would incorrectly be false.
    result = gate._evaluate_scenario_entry(
        "Marktbreite mindestens 3/11 ODER XLV positive 3M-Relative-Stärke "
        "UND XLE positive 3M-Relative-Stärke",
        "Marktbreite 4/11; XLV 3M -1.0%; XLE 3M -2.0%",
    )
    assert result.value is True


def test_scenario_gate_requires_all_three_scenario_blocks():
    report = report_fixture()
    start = report.index("Positives Alternativszenario:")
    end = report.index("Negatives Alternativszenario:", start)
    report = report[:start] + report[end:]
    errors = gate.validate_scenario_condition_consistency(report)
    assert any("Positives Alternativszenario fehlt" in item for item in errors), errors


def test_m05_accepts_explicit_legacy_archiviert_record_without_claiming_invalidation():
    import hashlib
    import json

    previous_hash = "a" * 64
    current_hash = "b" * 64
    comparison_payload = {
        "previous_sha256": previous_hash,
        "current_sha256": current_hash,
        "schema": "trading-radar-thesis-register-comparison-v1",
    }
    comparison_id = hashlib.sha256(
        json.dumps(comparison_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report = "\n".join((
        "2. Thesen-Update",
        "THESENREGISTER-VERGLEICH: PASS",
        f"VORHER-SHA256: {previous_hash}",
        f"NACHHER-SHA256: {current_hash}",
        f"THESENREGISTER-VERGLEICH-ID: {comparison_id}",
        "VORHER_THESEN_COUNT: 1",
        "VORHER_AKTIVE_THESEN: 0",
        "NACHHER_THESEN_COUNT: 1",
        "NACHHER_AKTIVE_THESEN: 0",
        "THESEN_NEU: KEINE",
        "THESEN_AKTUALISIERT: KEINE",
        "THESEN_UNVERAENDERT: KEINE",
        "THESEN_ARCHIVIERT: KEINE",
        "VORHER-THESIS-ID: LEGACY-001",
        "VORHER-REGISTER-VERSION: 1",
        "VORHER-THESIS-ACTIVE: NO",
        "THESIS_ID: LEGACY-001",
        "REGISTER-VERSION: 1",
        "THESIS_ACTIVE: NO",
        "THESIS_STATUS: ARCHIVIERT",
        "THESIS_REPLACED_BY: KEINE",
        "3. Ereignisradar",
    ))
    errors = []
    gate._validate_m05_m10(report, errors)
    assert not any("Semantischer Thesenregistervergleich widersprüchlich" in item for item in errors), errors
