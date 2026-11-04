import json
from pathlib import Path

import pandas as pd

import Trading_Radar_Alleinauswertung as production
from trading_radar_market_first import append_live_market_snapshot, SECTOR_ETF_LABELS
from trading_radar_report_gate import (
    _us10y_conflict_observation_records,
    _treasury_observation_has_complete_provenance,
    validate_report,
)


def _market_data():
    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
    ]
    index = pd.date_range("2026-07-01", periods=90, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.1 for i in range(len(index))] for col in columns},
        index=index,
    )
    data[("^TNX", "Close")] = [49.0 + i * 0.001 for i in range(len(index))]
    data[("XLE", "Close")] = [100.0 + i * 0.35 for i in range(len(index))]
    data[("XLV", "Close")] = [100.0 + i * 0.25 for i in range(len(index))]
    return data


def test_sector_etf_matrix_uses_readable_german_labels():
    report = (
        "### 1. Trading-Radar\nMarktregime: stabil.\n"
        "### 2. Thesen-Update\nThese.\n"
        "### 4. Assetklassenvergleich\nVergleich.\n"
        "### 5. Technische Kandidaten\nKeine Kandidaten.\n"
    )
    result, findings = append_live_market_snapshot(report, fetcher=lambda _symbols: _market_data())
    assert not findings
    for ticker, label in SECTOR_ETF_LABELS.items():
        assert f"{label} ({ticker})" in result
    assert "XLE (XLE)" not in result
    assert "XLV (XLV)" not in result


def test_us10y_different_observation_dates_are_reported_as_temporal_and_keep_provenance():
    report = (
        "### 1. Trading-Radar\n"
        "Zinsen/Renditen: US 10Y Treasury bei 4.9800% "
        "(Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations); "
        "Abrufzeit: 2026-11-04T08:00:00+00:00; "
        "Datenstichtag: 2026-10-29; Einheit: %; Verzögerung: 6 Kalendertage)\n"
        "### 2. Thesen-Update\nInvalidierungstrigger: US-10Y unter 4,50%.\n"
        "### 4. Assetklassenvergleich\nVergleich.\n"
        "### 5. Technische Kandidaten\nKeine Kandidaten.\n"
    )
    result, findings = append_live_market_snapshot(report, fetcher=lambda _symbols: _market_data())
    assert not findings
    paired = next(line for line in result.splitlines() if "ZEITLICH UNTERSCHIEDLICHE BEOBACHTUNGEN" in line)
    assert "Makro-Wert: 4.9800%" in paired
    assert "Live-Wert:" in paired
    assert "Datenstichtag: 2026-10-29" in paired
    assert "Datenstichtag: 2026-11-03" in paired
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in paired
    assert "QUELLENKONFLIKT: OFFEN" not in paired
    records = _us10y_conflict_observation_records(paired)
    assert [role for role, _value, _record in records] == ["Makro", "Live"]
    assert all(_treasury_observation_has_complete_provenance(record) for _role, _value, record in records)



def test_report_gate_accepts_temporal_pair_without_open_conflict_marker():
    import importlib.util
    fixture_path = Path(__file__).with_name("test_trading_radar_report_gate.py")
    spec = importlib.util.spec_from_file_location("temporal_report_gate_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture()
    temporal_line = (
        "- ZEITLICH UNTERSCHIEDLICHE BEOBACHTUNGEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.9800% (Makro-Provenienz: Quelle: FRED DGS10; "
        "Abrufzeit: 2026-11-04T08:00:00+00:00; Datenstichtag: 2026-10-29; "
        "Einheit: %; Verzögerung: 6 Kalendertage) | "
        "Live-Wert: 4.9200% (Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-04T14:44:07+00:00; Datenstichtag: 2026-11-04T00:00:00+00:00; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs) | "
        "Beide Werte bleiben mit eigenem Datenstichtag und eigener Provenienz erhalten."
    )
    report = report.replace("Dollar/Liquidität/Volatilität:", temporal_line + "\nDollar/Liquidität/Volatilität:", 1)
    errors = validate_report(report)
    assert not any("M-07:" in item for item in errors), errors
    assert not any("M-02: US-10Y-Rendite" in item for item in errors), errors

def test_open_positions_are_sorted_by_performance_descending(tmp_path):
    source = tmp_path / "Offene Positionen+Check.csv"
    source.write_text(
        "Name;Ticker;Status;Einstieg;Einstiegsdatum;Aktueller_Kurs;Performance_Seit_Einstieg%;Richtung;Stop;TP1;TP2\n"
        "Verlierer AG;LOSS;Offen;100;01.01.2026;80;-20,00;Long;70;120;140\n"
        "Gewinner AG;WIN;Offen;100;01.01.2026;110;10,00;Long;90;120;140\n"
        "Berechnet AG;CALC;Offen;100;01.01.2026;120;;Long;90;130;150\n"
        "Short AG;SHORT;Offen;100;01.01.2026;80;;Short;70;90;80\n"
        "Richtung Unklar AG;UNK;Offen;100;01.01.2026;150;;;;;\n",
        encoding="utf-8",
    )
    result = production._offene_positionen_quellblock(str(source))
    assert result.index("Short AG (SHORT)") < result.index("Berechnet AG (CALC)")
    assert result.index("Berechnet AG (CALC)") < result.index("Gewinner AG (WIN)")
    assert result.index("Gewinner AG (WIN)") < result.index("Verlierer AG (LOSS)")
    assert result.index("Verlierer AG (LOSS)") < result.index("Richtung Unklar AG (UNK)")
    assert "Performance seit Einstieg: +20.00%" in result
    assert "Performance seit Einstieg: NICHT VERFÜGBAR" in result
    assert "SORTIERUNG=Performance seit Einstieg absteigend" in result


def test_open_warrant_sort_uses_warrant_return_not_underlying_return(tmp_path):
    source = tmp_path / "Offene Positionen+Check.csv"
    source.write_text(
        "Name;Ticker;Status;Einstieg;Einstiegsdatum;Aktueller_Kurs;Performance_Seit_Einstieg%;Richtung;Produkt_Typ;OS_Einstiegskurs;OS_Aktueller_Kurs;OS_Performance%\n"
        "Optionsschein auf Beispiel;OS1;Offen;100;01.01.2026;180;80,00;Long;Optionsschein;2,00;2,20;10,00\n"
        "Aktie Beispiel;STK1;Offen;100;01.01.2026;120;20,00;Long;Aktie;;;;\n",
        encoding="utf-8",
    )
    result = production._offene_positionen_quellblock(str(source))
    assert result.index("Aktie Beispiel (STK1)") < result.index("Optionsschein auf Beispiel (OS1)")
    assert "Optionsschein-Performance seit Einstieg: +10.00%" in result
    assert "OS-Einstieg: 2,00" in result
    assert "OS-Kurs: 2,20" in result

def test_candidate_register_links_sector_direction_thesis_and_cutoff(tmp_path):
    setups = tmp_path / "Setups(2026-11-04).csv"
    setups.write_text(
        "Ticker;Name;Sektor;Richtung;Thesen-ID;Datenstichtag;Status;Trigger;Einstieg;Stop;TP1;CRV1;Setup_ID\n"
        "CVX;Chevron Corporation;Energie;Long;MARKTREGIME_2026_11;2026-11-03;VALIDE;Bruch des Hochs;150;140;165;1,5;COMMON_SETUP\n"
        "XOM;Exxon Mobil;Energie;Long;MARKTREGIME_2026_11;2026-11-03;VALIDE;Breakout;120;112;132;1,4;COMMON_SETUP\n",
        encoding="utf-8",
    )
    trendwende = tmp_path / "Trendwende_Setups(2026-11-04).csv"
    trendwende.write_text("Ticker;Name;Sektor;Status\n", encoding="utf-8")
    edelmetalle = tmp_path / "Edelmetalle_Setups(2026-11-04).csv"
    edelmetalle.write_text("Ticker;Name;Sektor;Status\n", encoding="utf-8")
    report = (
        "### 4. Assetklassenvergleich\n"
        "Relative-Stärke-Matrix (adjusted close; 1M=21 Handelstage, 3M=63 Handelstage):\n"
        "Gemeinsamer Datenstichtag: 2026-11-03\nSektoren:\n"
        "- Energie-ETF (XLE) 1M +5.00%; 3M +17.00%\n"
        "- Gesundheit-ETF (XLV) 1M +3.00%; 3M +12.50%\n"
        "- Finanz-ETF (XLF) 1M +1.00%; 3M +5.00%\n"
        "Gewinner: XLE; Verlierer: XLF; Divergenz: 12.00 Prozentpunkte\n"
        "### 5. Technische Kandidaten\nAlter Text.\n### 6. Portfolio & Risiko\nPortfolio.\n"
    )
    inputs = {
        "Setups": str(setups),
        "Trendwende_Setups": str(trendwende),
        "Edelmetalle_Setups": str(edelmetalle),
    }
    result = production._trading_radar_kandidatenquellen_absichern(report, inputs)
    result = production._trading_radar_marktrotation_zu_kandidaten(result)
    assert "MARKTROTATION → AKTIENPRÜFUNG" in result
    assert "Energie-ETF (XLE): +17.00%" in result
    assert "Scanner-Abgleich: 2 prüfbare Setups, 0 Watchlist-/Trigger-Kandidaten" in result
    assert "Sektor: Energie | US-Sektor-ETF-Referenz: Energie-ETF (XLE)" in result
    assert "3M-Relative-Stärke: +17.00%" in result
    assert "Richtung: Long" in result
    assert "Verknüpfte These: MARKTREGIME_2026_11" in result
    assert "Datenstichtag: 2026-11-03" in result
    ids = [line.split("Setup-ID: ", 1)[1].split(" |", 1)[0]
           for line in result.splitlines() if "Asset/Ticker:" in line]
    assert len(ids) == 2 and len(set(ids)) == 2





def test_readable_cleanup_preserves_independent_post_save_report_gate():
    import importlib.util
    fixture_path = Path(__file__).with_name("test_trading_radar_report_gate.py")
    spec = importlib.util.spec_from_file_location("readable_gate_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture()
    report = report.replace(
        "6. Portfolio & Risiko\nGeschlossene Positionen:",
        "6. Portfolio & Risiko\n"
        "- Gewinner AG (AAA): Einstieg: 100 | Aktueller Kurs: 120 | These verletzt: Nein.\n"
        "- Verlierer AG (BBB): Einstieg: 100 | Aktueller Kurs: 70 | These verletzt: Ja.\n"
        "Geschlossene Positionen:",
        1,
    )
    cleaned = production._trading_radar_lesefassung_komprimieren(report)
    assert validate_report(cleaned) == []


def test_compact_thesis_summary_satisfies_post_save_identity_gate_for_multiple_theses():
    import importlib.util
    fixture_path = Path(__file__).with_name("test_trading_radar_report_gate.py")
    spec = importlib.util.spec_from_file_location("multi_thesis_gate_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture()
    report = report.replace(
        "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.",
        "Thesen-ID: T-1 | Version: 2 | Status: BESTÄTIGT | gegenüber Vorversion aktualisiert.\n"
        "Thesen-ID: T-2 | Version: 1 | Status: BESTÄTIGT | unverändert.",
        1,
    )
    report = report.replace("VORHER_THESEN_COUNT: 1", "VORHER_THESEN_COUNT: 2", 1)
    report = report.replace("VORHER_AKTIVE_THESEN: 1", "VORHER_AKTIVE_THESEN: 2", 1)
    report = report.replace("NACHHER_THESEN_COUNT: 1", "NACHHER_THESEN_COUNT: 2", 1)
    report = report.replace("NACHHER_AKTIVE_THESEN: 1", "NACHHER_AKTIVE_THESEN: 2", 1)
    report = report.replace("THESEN_UNVERAENDERT: KEINE", "THESEN_UNVERAENDERT: T-2", 1)
    report = report.replace(
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES",
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "VORHER-THESIS-ID: T-2\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES",
        1,
    )
    report = report.replace(
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: YES",
        "THESIS_ID: T-1\nREGISTER-VERSION: 2\nTHESIS_ACTIVE: YES\n"
        "THESIS_ID: T-2\nREGISTER-VERSION: 1\nTHESIS_ACTIVE: YES",
        1,
    )
    payload = {
        "data_cutoff": "2026-11-04T14:40:00+00:00",
        "theses": [
            {"thesis_id": "T-1", "version": 2, "status": "BESTÄTIGT", "thesis": "These eins"},
            {"thesis_id": "T-2", "version": 1, "status": "BESTÄTIGT", "thesis": "These zwei"},
        ],
    }
    report = report.replace(
        "2. Thesen-Update\n",
        "2. Thesen-Update\n```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```\n",
        1,
    )
    cleaned = production._trading_radar_lesefassung_komprimieren(report)
    assert "```TRADING_RADAR_THESEN_JSON" not in cleaned
    assert "- T-1 | Version 2 | Status BESTÄTIGT" in cleaned
    assert "- T-2 | Version 1 | Status BESTÄTIGT" in cleaned
    assert validate_report(cleaned) == []

def test_rotation_summary_does_not_invent_trade_ideas_when_strong_sector_has_no_scanner_candidate():
    report = (
        "### 4. Assetklassenvergleich\n"
        "Relative-Stärke-Matrix (adjusted close; 1M=21 Handelstage, 3M=63 Handelstage):\n"
        "Gemeinsamer Datenstichtag: 2026-11-03\nSektoren:\n"
        "- Energie-ETF (XLE) 1M +5.00%; 3M +17.00%\n"
        "- Gesundheit-ETF (XLV) 1M +3.00%; 3M +12.50%\n"
        "- Finanz-ETF (XLF) 1M +1.00%; 3M +5.00%\n"
        "Gewinner: XLE; Verlierer: XLF; Divergenz: 12.00 Prozentpunkte\n"
        "### 5. Technische Kandidaten\nKeine Kandidaten.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\n"
        "AUF TRIGGER WARTEN: Keine Kandidaten\n"
        "### 6. Portfolio & Risiko\nPortfolio.\n"
    )
    result = production._trading_radar_marktrotation_zu_kandidaten(report)
    assert "Energie-ETF (XLE): +17.00%" in result
    assert "Im strukturierten Scannerregister ist diesem Sektor kein Kandidat zugeordnet" in result
    assert "keine Tradeidee abgeleitet" in result
    assert "Asset/Ticker:" not in result

def test_portfolio_section6_keeps_only_affected_positions_and_points_to_full_section9():
    report = (
        "6. Portfolio & Risiko\n"
        "- Gewinner AG (AAA): Einstieg: 100 | Aktueller Kurs: 120 | These verletzt: Nein | Nächster Prüfschritt: Halten.\n"
        "- Verlierer AG (BBB): Einstieg: 100 | Aktueller Kurs: 70 | These verletzt: Ja | Nächster Prüfschritt: Risiko prüfen.\n"
        "Konzentrationen und gemeinsame Risikotreiber: Rohstoffexponierung.\n"
        "7. MSCI-Vergleich\nVergleich.\n"
        "9. Vollständige offene Positionen\nOFFENE_POSITIONEN_ANZAHL=2\n"
    )
    result = production._trading_radar_lesefassung_komprimieren(report)
    section6 = result.split("7. MSCI-Vergleich", 1)[0]
    assert "Vollständige Positionsübersicht: Abschnitt 9 (2 offene Positionen" in section6
    assert "Verlierer AG (BBB)" in section6
    assert "Gewinner AG (AAA)" not in section6
    assert "Konzentrationen und gemeinsame Risikotreiber" in section6
    assert "9. Vollständige offene Positionen" in result

def test_readable_report_cleanup_keeps_section9_and_replaces_machine_blocks():
    payload = {
        "theses": [{
            "thesis_id": "MARKTREGIME_2026_11", "version": 3, "status": "BESTÄTIGT",
            "confidence": "mittel", "thesis": "Restriktiv-stabiles Regime.",
            "horizon": "4 Wochen", "alternative": "Rasche Lockerung.",
            "evidence": ["FRED DGS10 4,98%"], "counter_evidence": ["VIX steigt"],
            "confirm_triggers": ["Renditen stabil"], "invalidate_triggers": ["Renditeeinbruch"],
        }]
    }
    report = (
        "2. Thesen-Update\n```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```\n8. Daten-/Prüfanhang\n"
        "OHLCV-Symbole-VALID: A, B, C\nOHLCV-Symbole-INVALID: X, Y\n"
        "Bewusst ausgeschlossene Ticker (nicht Teil des aktiven Datenuniversums): A, B\n"
        "9. Vollständige offene Positionen\nPosition A\n"
        "Name nicht aufgelöst (DHL) AG (DHL.DE)\nSilber Name nicht aufgelöst (ETC)\n"
    )
    result = production._trading_radar_lesefassung_komprimieren(report)
    assert "```TRADING_RADAR_THESEN_JSON" not in result
    assert "Thesenregister – kompakte Übersicht" in result
    assert "Restriktiv-stabiles Regime." in result
    assert "Belege: FRED DGS10 4,98%" in result
    assert "Gegenbelege: VIX steigt" in result
    assert "OHLCV-Symbole-VALID: A, B, C" in result
    assert "OHLCV-Symbole-INVALID: X, Y" in result
    assert "Bewusst ausgeschlossene Ticker: 2" in result
    assert "9. Vollständige offene Positionen" in result
    assert "DHL Group (DHL.DE)" in result
    assert "Silber-ETC" in result
