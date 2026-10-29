import re

from trading_radar_neuauswertung import (
    RADAR_INSTRUCTION,
    audit_market_first_content,
    normalize_market_first_report_structure,
)


def _production_shaped_report():
    return """### 1. Trading-Radar

*   **Hauptthese**: Moderate US-Leitmarkt-Konsolidierung bei anhaltend straffen Realzinsen und selektiver Sektorrotation.
*   **Wichtigste Veränderungen**: 10Y Treasury bleibt hoch; Volatilität bleibt moderat.

**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**
- Index S&P 500 (^GSPC): letzter Schluss 7743.98; 1M +1.21%; 3M +0.10%.
- Index Nasdaq Composite (^IXIC): letzter Schluss 26549.85; 1M -1.16%; 3M -0.13%.
- Volatilität VIX: 17.22
- Dollar-Proxy DXY (DX-Y.NYB): 102.21
- US-10J-Rendite (^TNX): 0.50%
- Anleihe-ETF TLT 1M: +4.55%.
- Marktbreite-Proxy: 8/11 Sektor-ETFs über ihrem 50-Tage-Durchschnitt.

### 2. Thesen-Update

*   **Geprüfter Registerstand**: Das vorhandene Register wurde geprüft.
*   **Aktualisierte Thesen**:
    *   Thesen-ID: M-2026-01 (Makro & Zinsumfeld)
    *   Version: 3.0
    *   Status: BESTÄTIGT
    *   *Gegenbelege:* Trotz restriktiver Renditen zeigen Leitmärkte selektive Resilienz.
    *   *Alternative:* Sinkende Inflation könnte rasche Zinssenkungen auslösen.
    *   *Bestätigungstrigger:* Renditen bleiben auf dem belegten hohen Niveau.
    *   *Invalidierungstrigger:* Eine belegte Notfall-Zinssenkung verändert das Regime.

### 3. Ereignisradar
- Keine relevanten Ereignisse im geprüften Zeitraum.

### 4. Assetklassenvergleich
Relative Stärke und Kapitalrotation zeigen selektive Gewinner und Verlierer.

### 5. Technische Kandidaten
JETZT TECHNISCH PRÜFEN: Keine Kandidaten
AUF TRIGGER WARTEN: Keine Kandidaten
EREIGNIS ABWARTEN: Keine Kandidaten
NICHT HANDELBAR: Keine Kandidaten

### 6. Portfolio & Risiko
Positionen werden anhand der Marktthese bewertet.

### 7. MSCI-Vergleich
Portfolio- und Benchmarkperformance werden getrennt.

### 8. Daten-/Prüfanhang
Datenstände und Quellenlücken sind dokumentiert.
"""


def test_nested_bold_hauptthese_triggers_deterministic_market_fields():
    report = _production_shaped_report()
    normalized = normalize_market_first_report_structure(report)
    for label in (
        "Indizes/Marktbreite:",
        "Zinsen/Renditen:",
        "Dollar/Liquidität/Volatilität:",
        "Marktregime:",
        "Basisszenario:",
    ):
        assert normalized.count(label) == 1
    assert audit_market_first_content(normalized) == []


def test_market_audit_accepts_same_nested_italic_labels_as_normalizer():
    report = normalize_market_first_report_structure(_production_shaped_report())
    # Convert generated market labels to nested italic Markdown. Both stages
    # must treat this as the same field syntax.
    for label in (
        "Indizes/Marktbreite", "Zinsen/Renditen",
        "Dollar/Liquidität/Volatilität", "Marktregime", "Basisszenario",
    ):
        report = report.replace(f"{label}:", f"*   *{label}*:")
    assert audit_market_first_content(report) == []


def test_normalizer_does_not_invent_missing_rate_or_dollar_evidence():
    report = _production_shaped_report()
    report = report.replace("- Volatilität VIX: 17.22", "- Volatilität VIX: NICHT VERFÜGBAR")
    report = report.replace("- Dollar-Proxy DXY (DX-Y.NYB): 102.21", "- Dollar-Proxy DXY: NICHT VERFÜGBAR")
    report = report.replace("- US-10J-Rendite (^TNX): 0.50%", "- US-10J-Rendite: NICHT VERFÜGBAR")
    report = report.replace("- Anleihe-ETF TLT 1M: +4.55%.", "- Anleihe-ETF TLT 1M: NICHT VERFÜGBAR.")
    normalized = normalize_market_first_report_structure(report)
    assert "Zinsen/Renditen:" not in normalized
    assert "Dollar/Liquidität/Volatilität:" not in normalized
    findings = audit_market_first_content(normalized)
    assert any("Zinsen/Renditen" in finding for finding in findings)
    assert any("Dollar/Liquidität/Volatilität" in finding for finding in findings)


def test_existing_generic_market_field_values_do_not_pass_audit():
    report = normalize_market_first_report_structure(_production_shaped_report())
    report = re.sub(
        r"(?m)^Zinsen/Renditen:.*$",
        "Zinsen/Renditen: Zins- und Renditebelege siehe Hauptaussage und stärkste Belege in diesem Abschnitt",
        report,
    )
    report = re.sub(
        r"(?m)^Dollar/Liquidität/Volatilität:.*$",
        "Dollar/Liquidität/Volatilität: Dollar-/Volatilitätsbelege nicht separat ausgewiesen",
        report,
    )
    findings = audit_market_first_content(report)
    assert any("Zinsen/Renditen" in finding for finding in findings)
    assert any("Dollar/Liquidität/Volatilität" in finding for finding in findings)


def test_italic_thesis_fields_are_recognized_without_weakening_value_checks():
    report = normalize_market_first_report_structure(_production_shaped_report())
    assert audit_market_first_content(report) == []
    broken = report.replace(
        "*   *Invalidierungstrigger:* Eine belegte Notfall-Zinssenkung verändert das Regime.",
        "*   *Invalidierungstrigger:* NICHT VERFÜGBAR",
    )
    assert any("Invalidierungstrigger" in finding for finding in audit_market_first_content(broken))
    broken_parenthetical = report.replace(
        "*   *Gegenbelege:* Trotz restriktiver Renditen zeigen Leitmärkte selektive Resilienz.",
        "*   *Gegenbelege:* NICHT VERFÜGBAR (keine Quelle)",
    )
    assert any("Gegenbelege" in finding for finding in audit_market_first_content(broken_parenthetical))


def test_market_first_normalization_is_idempotent():
    once = normalize_market_first_report_structure(_production_shaped_report())
    twice = normalize_market_first_report_structure(once)
    for label in (
        "Indizes/Marktbreite:",
        "Zinsen/Renditen:",
        "Dollar/Liquidität/Volatilität:",
        "Marktregime:",
        "Basisszenario:",
    ):
        assert twice.count(label) == 1
    assert audit_market_first_content(twice) == []


def test_production_prompt_uses_one_canonical_section_four_and_explicit_fields():
    from pathlib import Path

    # The production eigenlauf prompt must not instruct Gemini to create a
    # second, contradictory section 4.
    production_source = (Path(__file__).resolve().parents[1] / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert "ABSCHNITT 4 – ASSETKLASSENVERGLEICH:" in production_source
    assert "ABSCHNITT 4 – RELATIVE ASSETATTRAKTIVITÄT:" not in production_source
    assert "Indizes/Marktbreite:" in production_source
    assert "Gegenbelege:" in production_source
    # The shared prompt retains the same canonical report heading.
    assert "4 · Assetklassenvergleich" in RADAR_INSTRUCTION



def test_yahoo_tnx_percent_scale_is_not_divided_twice():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    ]
    dates = pd.date_range("2026-06-01", periods=80, freq="B")
    expected_tnx = ((5.0, "5.00%"), (50.0, "5.00%"), (19.5, "1.95%"), (10.0, "1.00%"))
    for raw_tnx, expected in expected_tnx:
        columns = pd.MultiIndex.from_product([symbols, ["Close"]])
        data = pd.DataFrame(
            {column: [100.0 + i * 0.2 for i in range(len(dates))] for column in columns},
            index=dates,
        )
        data[("^TNX", "Close")] = [raw_tnx] * len(dates)
        report = (
            "### 1. Trading-Radar\n"
            "* **Hauptthese**: US-Zinsen bleiben restriktiv.\n"
            "### 2. Thesen-Update\n"
        )
        result, findings = append_live_market_snapshot(report, fetcher=lambda requested, d=data: d)
        assert findings == []
        assert f"- US-10J-Rendite (^TNX): {expected}" in result
        assert "- US-10J-Rendite (^TNX): 0.50%" not in result
