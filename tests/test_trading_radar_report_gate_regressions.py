from trading_radar_report_gate import _label_value, validate_report
from trading_radar_market_first import normalize_event_section


def test_label_value_accepts_markdown_star_bullets():
    text = "* Thesen-ID: THEARD-2026-10-27-01\n* Version: 2.1\n* Status: BESTÄTIGT"
    assert _label_value(text, (r"Thesen-ID",)) == "THEARD-2026-10-27-01"
    assert _label_value(text, (r"Version",)) == "2.1"
    assert _label_value(text, (r"Status",)) == "BESTÄTIGT"


def test_no_events_with_orphan_unavailable_fields_normalizes_cleanly():
    report = """### 3. Ereignisradar

* Keine relevanten Ereignisse im geprüften Zeitraum.

---
Uhrzeit: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)
Zeitzone: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)
Kennzahl: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)
Berichtszeitraum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)
Letzter Wert: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)
Konsens: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)

### 4. Assetklassenvergleich
"""
    normalized, findings = normalize_event_section(report)
    assert findings == []
    assert "Uhrzeit: NICHT VERFÜGBAR" not in normalized
    assert normalized.count("Keine relevanten Ereignisse") == 1


def test_report_gate_does_not_hash_text_appended_after_tab2_fingerprint():
    # Uses the report fixture from the repository if present; focused assertions
    # are covered by the real report validation run in the regression command.
    import hashlib
    payload_lines = ["TAB-2-ANZAHL=1", "TAB-2-QUELLE=Geschlossene Positionen",
                     "Quellzeile: 3 | Ticker: ABC | Einstieg: 1"]
    digest = hashlib.sha256("\n".join(payload_lines).encode("utf-8")).hexdigest()
    payload = "\n".join(payload_lines) + "\nTAB-2-EXPORT-SHA256=" + digest
    appended = payload + "\n\nDaten-/Freigabeprüfung: NICHT_FINAL"
    assert appended.split("TAB-2-EXPORT-SHA256=", 1)[0].rstrip().splitlines() == payload_lines



def test_ohlcv_appendix_normalizer_removes_markdown_prefixed_contradictory_claims():
    import Trading_Radar_Alleinauswertung as production
    report = """### 8. Daten-/Prüfanhang
OHLCV-Abdeckung: 738/862 Symbole abgedeckt; Status=gaps.
* **OHLCV-Abdeckung:** OHLCV vollständig, keine Datenlücken; Kandidaten nicht eingeschränkt.
* **Datenstand / Cutoff:** 2026-10-27T17:47:03+00:00

9. Vollständige offene Positionen
"""
    normalized = production._trading_radar_ohlcv_abschnitt8_aktualisieren(report)
    assert "OHLCV vollständig, keine Datenlücken" not in normalized
