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
