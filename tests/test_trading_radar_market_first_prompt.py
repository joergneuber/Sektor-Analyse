from trading_radar_neuauswertung import RADAR_INSTRUCTION, audit_market_first_content


def test_prompt_declares_market_first_and_portfolio_last():
    assert RADAR_INSTRUCTION.index("GESAMTMARKT UND MARKTREGIME") < RADAR_INSTRUCTION.index("RELATIVE STAERKE")
    assert RADAR_INSTRUCTION.index("RELATIVE STAERKE") < RADAR_INSTRUCTION.index("THESEN, SZENARIEN")
    assert RADAR_INSTRUCTION.index("THESEN, SZENARIEN") < RADAR_INSTRUCTION.index("TECHNISCHE KANDIDATEN")
    assert RADAR_INSTRUCTION.index("TECHNISCHE KANDIDATEN") < RADAR_INSTRUCTION.index("PORTFOLIO-AUSWIRKUNG")


def test_market_first_audit_flags_missing_rotation_and_regime():
    report = """1 · Trading-Radar
Portfolio war schwach.
2 · Thesen-Update
Keine Thesen.
3 · Ereignisradar
Ein Ereignis.
4 · Assetklassenvergleich
Keine Daten.
5 · Technische Kandidaten
Keine Kandidaten.
6 · Portfolio & Risiko
Viele Positionen.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert any("Marktregime" in e for e in errors)
    assert any("Kapitalrotation" in e for e in errors)


def test_market_first_audit_accepts_market_led_structure():
    # Keep this acceptance fixture aligned with the production gate contract:
    # individually labeled market fields, a complete thesis record, and all
    # four required candidate groups explicitly represented (empty is valid).
    report = """### 1. Trading-Radar
Indizes/Marktbreite: S&P 500 and Nasdaq breadth weaken.
Zinsen/Renditen: Treasury yields rise.
Dollar/Liquidität/Volatilität: Dollar and VIX rise; liquidity remains tight.
Marktregime: risk-off.
Basisszenario: defensive market conditions.
### 2. Thesen-Update
These T1:
Gegenbelege: Earnings remain resilient despite weak breadth.
Alternative: sideways market if volatility fades.
Bestätigungstrigger: breadth improves across several sessions.
Invalidierungstrigger: S&P 500 falls below the last confirmed swing low.
### 3. Ereignisradar
FOMC.
### 4. Assetklassenvergleich
Relative Stärke zeigt Rotation aus Growth in Energie; Gewinner und Verlierer divergieren.
### 5. Technische Kandidaten
### JETZT TECHNISCH PRÜFEN
Keine Kandidaten.
### AUF TRIGGER WARTEN
Keine Kandidaten.
### EREIGNIS ABWARTEN
Keine Kandidaten.
### NICHT HANDELBAR
Keine Kandidaten.
### 6. Portfolio & Risiko
Relevante Auswirkungen.
### 7. MSCI-Vergleich
Vergleich.
### 8. Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []


def test_market_first_normalizer_adds_evidence_labels_and_explicit_empty_groups():
    from trading_radar_neuauswertung import normalize_market_first_report_structure, audit_market_first_content

    report = """1. Trading-Radar
Hauptaussage: Eine flächendeckende aggressive Rally ist nicht tragfähig; disziplinierte Kapitalrotation dominiert. US 10Y Treasury bei 5,0000%, 10Y TIPS bei 2,6600%.

**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**
- Index S&P 500 (^GSPC): letzter Schluss 7722.60; 1M +0.67%; 3M +1.61%.
- Index Nasdaq Composite (^IXIC): letzter Schluss 26477.81; 1M -1.19%; 3M +2.18%.
- Index Russell 2000 (^RUT): letzter Schluss 2818.87; 1M +0.39%; 3M -5.47%.
- Volatilität VIX: 17.40
- Dollar-Proxy DXY (DX-Y.NYB): 102.32
- Anleihe-ETF TLT 1M: +4.77%.
- Marktbreite-Proxy: 7/11 Sektor-ETFs über ihrem 50-Tage-Durchschnitt.

2. Thesen-Update
- Keine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.

3. Ereignisradar
- Keine relevanten Ereignisse im geprüften Zeitraum.

4. Assetklassenvergleich
Relative Stärke und Kapitalrotation zeigen selektive Gewinner und Verlierer.

5. Technische Kandidaten
Keine belastbaren technischen Kandidaten; keine Handelsfreigabe.

6. Portfolio & Risiko
Portfolio bleibt getrennt von der Marktregimebewertung.
"""
    normalized = normalize_market_first_report_structure(report)
    assert "Indizes/Marktbreite:" in normalized
    assert "Zinsen/Renditen:" in normalized
    assert "Dollar/Liquidität/Volatilität:" in normalized
    assert "Marktregime:" in normalized
    assert "Basisszenario:" in normalized
    assert normalized.count("JETZT TECHNISCH PRÜFEN: Keine Kandidaten") == 1
    assert audit_market_first_content(normalized) == []
