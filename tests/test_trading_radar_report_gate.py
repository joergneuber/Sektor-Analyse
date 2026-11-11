"""Focused regression coverage for the M-05..M-10 report contract."""
import importlib.util
import hashlib
import json
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
        "Marktregime: Selektives Risk-on mit defensiver Rotation.\n"
        "Marktregime-Datenstichtag: 2026-10-30\n"
        "Indizes/Marktbreite: S&P 500 +1,0%; Marktbreite 8/11.\n"
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.\n"
        "Dollar/Liquidität/Volatilität: VIX 18,0; DXY 100,0.\n"
        "Index S&P 500: 1M +1,0%; 3M +2,0%.\nIndex Nasdaq: 1M +0,5%; 3M +1,5%.\n"
        "Basisszenario: Seitwärts bis aufwärts. Eintritt: Marktbreite stabil. Bestätigungstrigger: Ausbruch. Invalidierungstrigger: Bruch 200-Tage-Linie. Zeithorizont: 4 Wochen. Indikatoren: Marktbreite. Konsequenz: selektive Longs.\n"
        "Positives Alternativszenario: Beschleunigter Aufwärtstrend. Eintritt: Renditen fallen. Bestätigungstrigger: neue Hochs. Invalidierungstrigger: Rückfall unter Ausbruchsniveau. Zeithorizont: 4 Wochen. Indikatoren: Renditen, Breite. Konsequenz: Risiko erhöhen.\n"
        "Negatives Alternativszenario: Korrektur. Eintritt: Renditen steigen. Bestätigungstrigger: Marktbreite bricht. Invalidierungstrigger: Rückeroberung 50-Tage-Linie. Zeithorizont: 4 Wochen. Indikatoren: Renditen, Breite. Konsequenz: Risiko reduzieren.",
        "2. Thesen-Update\nThesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.\n"
        "THESENREGISTER-VERGLEICH: PASS\nVORHER-SHA256: " + before_hash + "\nNACHHER-SHA256: " + after_hash + "\n"
        "THESENREGISTER-VERGLEICH-ID: " + comparison_id + "\n"
        "VORHER_THESEN_COUNT: 1\nVORHER_AKTIVE_THESEN: 1\nNACHHER_THESEN_COUNT: 1\nNACHHER_AKTIVE_THESEN: 1\n"
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "THESEN_NEU: KEINE\nTHESEN_AKTUALISIERT: T-1\nTHESEN_UNVERAENDERT: KEINE\nTHESEN_ARCHIVIERT: KEINE\n"
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: YES\nVERSION: 2",
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

    def test_m07_accepts_updated_thesis_status(self):
        text = report_fixture().replace(
            "Status: BESTÄTIGT",
            "Status: Aktualisiert",
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
            "- Reaktionskanäle: Anleihen, Devisen, Aktien",
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

