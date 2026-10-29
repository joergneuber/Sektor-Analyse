"""Regression tests for preserving already-dated FOMC event records."""

import trading_radar_market_first as radar


def test_explicitly_dated_fomc_record_preserves_status_and_unknown_fields(monkeypatch):
    report = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  Datum: 2026-10-28
  Quelle: Federal Reserve
  Uhrzeit: NICHT VERFÜGBAR
  Zeitzone: NICHT VERFÜGBAR
  Quellenprüfung: NICHT VERIFIZIERT — nicht live bestätigt
- Ereignis: FOMC-Zinsentscheid
  Datum: 2026-10-28
  Quelle: Federal Reserve
  Uhrzeit: NICHT VERFÜGBAR
  Zeitzone: NICHT VERFÜGBAR
  Quellenprüfung: NICHT VERIFIZIERT — nicht live bestätigt

4. Assetklassenvergleich
Marktbild.
"""
    calls = []

    def unsafe_enrichment(body, report_text, *args, **kwargs):
        calls.append((body, report_text))
        return body.replace("2026-10-28", "2026-12-09").replace(
            "Uhrzeit: NICHT VERFÜGBAR", "Uhrzeit: 14:00"
        ).replace("Quellenprüfung: NICHT VERIFIZIERT", "Quellenprüfung: LIVE_VERIFIZIERT")

    monkeypatch.setattr(radar, "_enrich_fomc_event", unsafe_enrichment)
    result, findings = radar.normalize_event_section(report)

    # Existing dated records must bypass calendar enrichment altogether.
    assert calls == []
    assert result.count("Ereignis: FOMC-Zinsentscheid") == 1
    assert result.count("Datum: 2026-10-28") == 1
    assert "Datum: 2026-12-09" not in result
    assert result.count("Uhrzeit: NICHT VERFÜGBAR") == 1
    assert result.count("Zeitzone: NICHT VERFÜGBAR") == 1
    assert "Quellenprüfung: NICHT VERIFIZIERT" in result
    assert "Quellenprüfung: LIVE_VERIFIZIERT" not in result
    assert "4. Assetklassenvergleich" in result
    assert any("nicht live" in item.lower() for item in findings)


def test_undated_fomc_record_still_allows_enrichment(monkeypatch):
    report = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  Quelle: Federal Reserve

4. Assetklassenvergleich
Marktbild.
"""
    calls = []

    def safe_enrichment(body, report_text, *args, **kwargs):
        calls.append(True)
        return body + "\\nQuellenprüfung: NICHT VERIFIZIERT — Test\\n"

    monkeypatch.setattr(radar, "_enrich_fomc_event", safe_enrichment)
    radar.normalize_event_section(report)
    assert calls == [True]
