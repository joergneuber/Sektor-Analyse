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
    report = """1 · Trading-Radar
Indizes und Marktbreite deuten auf ein risk-off Marktregime. Renditen steigen, Dollar und VIX ziehen an; Liquidität bleibt eng. Basisszenario: defensives Marktbild.
2 · Thesen-Update
These T1: Gegenbeleg und Alternativszenario dokumentiert; Trigger bestätigt, sonst Invalidierung.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke zeigt Rotation aus Growth in Energie; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
Watchlist; auf Trigger warten, nicht handelbar bis technisch geprüft.
6 · Portfolio & Risiko
Relevante Auswirkungen.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []
