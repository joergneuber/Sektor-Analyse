import ast
import json
from decimal import Decimal
import re
from pathlib import Path

import Trading_Radar_Alleinauswertung as production
from trading_radar_report_gate import validate_report
from trading_radar_neuauswertung import audit_market_first_content, ensure_structured_thesis_block, validate_thesis_register

ROOT = Path(__file__).resolve().parents[1]


def test_scenarios_do_not_fabricate_thresholds_when_indicators_are_missing():
    report = (
        "1. Trading-Radar\n"
        "Zeithorizont: 4 bis 12 Wochen\n"
        "Basisszenario: Fortsetzung der Seitwärtsbewegung\n"
        "\n2. Thesen-Update\n"
    )
    result = production._trading_radar_szenarien_ergänzen(report)
    assert result.count("NICHT ABLEITBAR – Datenlücke:") == 3
    assert "0,00%" not in result
    assert "NICHT VERFÜGBAR" not in result
    assert "200-Tage-Linie" not in result


def test_scenarios_use_measured_market_data_only():
    report = (
        "1. Trading-Radar\n"
        "Zeithorizont: 4 bis 12 Wochen\n"
        "Basisszenario: Fortsetzung der Seitwärtsbewegung\n"
        "Marktbreite-Proxy: 8/11 Sektor-ETFs über 50T\n"
        "Volatilität VIX: 19.42\n"
        "\n4. Assetklassenvergleich\n"
        "Datenstichtag: 2026-10-30\n"
        "Sektoren: XLV (XLV) 1M +11.23%; 3M +14.38% | XLE (XLE) 1M +11.67%; 3M +19.81%\n"
        "\n2. Thesen-Update\n"
    )
    result = production._trading_radar_szenarien_ergänzen(report)
    assert "Gesundheit-ETF (XLV) 3M +14.38%" in result
    assert "Energie-ETF (XLE) 3M +19.81%" in result
    assert "VIX 19.42" in result
    assert "Eintrittsbedingung:" in result
    assert "Bestätigungstrigger:" in result
    assert "Invalidierungstrigger:" in result
    assert "Zeithorizont: 4 bis 12 Wochen" in result
    assert "NICHT VERFÜGBAR" not in result


def test_section7_heading_is_canonicalized_and_unverified_claims_removed():
    report = (
        "7. MSCI-Vergleich- Durchschnittliche Positionsperformance: -14,68%\n"
        "- Erläuterung: Portfolio-Gesamtperformance -5,74%; Underperformance -6,54 Prozentpunkte.\n"
        "8. Daten-/Prüfanhang\nOHLCV-Daten.\n"
    )
    result = production._trading_radar_mscivergleich_ueberschrift_normalisieren(report)
    assert result.startswith("7. MSCI-Vergleich\n")
    assert "-14,68%" not in result
    assert "-5,74%" not in result
    assert "-6,54 Prozentpunkte" not in result
    assert "Benchmark-Performance: NICHT VERFÜGBAR" in result


def test_candidate_source_normalizer_preserves_distinct_setups_for_same_ticker():
    source = ROOT / "tests" / "fixtures" / "hebeltrader_candidates_fixture.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({
        "candidates": [
            {"name": "Example AG", "ticker": "EXA", "einzel_check": {
                "ticker": "EXA", "status": "KAUFKANDIDAT C",
                "risiken": ["kein bestätigtes Einstiegssignal"],
            }},
            {"name": "Rejected AG", "ticker": "REJ", "einzel_check": {
                "ticker": "REJ", "status": "KEIN KANDIDAT",
                "risiken": ["Momentum zu schwach"],
            }},
        ]
    }), encoding="utf-8")
    try:
        report = (
            "5. Technische Kandidaten\nKeine belastbaren technischen Kandidaten.\n"
            "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\n"
            "AUF TRIGGER WARTEN: Keine Kandidaten\n"
            "EREIGNIS ABWARTEN: Keine Kandidaten\n"
            "NICHT HANDELBAR: Keine Kandidaten\n\n"
            "6. Portfolio & Risiko\n"
        )
        result = production._trading_radar_kandidatenquellen_absichern(
            report, {source.name: str(source)}
        )
        assert production._trading_radar_kandidaten_aus_abschnitt5(result) == ["EXA", "REJ"]
        assert result.count("Asset/Ticker:") == 2
        assert result.count("Setup-ID:") >= 2
        assert result.count("Statusgruppe: NICHT HANDELBAR") == 1
        assert result.count("Statusgruppe: VERWERFEN / INVALIDIERT") == 1
        assert "Einstieg: NICHT VERFÜGBAR" in result
        assert "Regel-/Datenstatus:" in result
    finally:
        source.unlink(missing_ok=True)



def test_same_ticker_with_trend_and_reversal_setups_is_not_collapsed(tmp_path):
    trend = tmp_path / "Setups(2026-10-30).csv"
    trend.write_text(
        "Ticker;Name;Strategie;Setup_Typ;Pattern;Status2;CRV1;Einstieg;Stop;TP1\n"
        "ABC;Alpha AG;Trendfolge;Pullback;Hammer;ACHTUNG;1,5;100;95;110\n",
        encoding="utf-8",
    )
    reversal = tmp_path / "Trendwende_Setups(2026-10-30).csv"
    reversal.write_text(
        "Ticker;Name;Strategie;Setup_Typ;Pattern;Status2;CRV1;Einstieg;Stop;TP1\n"
        "ABC;Alpha AG;Trendwende;Bodenbildung;Doppelboden;ACHTUNG;1,8;90;85;105\n",
        encoding="utf-8",
    )
    metals = tmp_path / "Edelmetalle_Setups(2026-10-30).csv"
    metals.write_text("Ticker;Name;Status2;Setup_Typ;Einstieg;Stop;TP1;CRV1\n", encoding="utf-8")
    report = (
        "5. Technische Kandidaten\nKeine belastbaren technischen Kandidaten.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\nAUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\nNICHT HANDELBAR: Keine Kandidaten\n"
        "VERWERFEN / INVALIDIERT: Keine Kandidaten\n6. Portfolio & Risiko\n"
    )
    result = production._trading_radar_kandidatenquellen_absichern(report, {
        trend.name: str(trend), reversal.name: str(reversal), metals.name: str(metals),
    })
    assert result.count("Asset/Ticker: Alpha AG (ABC)") == 2
    assert "Strategie" not in result or "Trendfolge" in result
    assert "Quelle: Setups(2026-10-30).csv" in result
    assert "Quelle: Trendwende_Setups(2026-10-30).csv" in result
    assert "Scanner-Setup: Pullback" in result
    assert "Scanner-Setup: Bodenbildung" in result
    setup_ids = re.findall(r"Setup-ID: ([0-9a-f]{16})", result)
    assert len(set(setup_ids)) == 2


def test_same_ticker_same_strategy_different_setup_types_get_distinct_identity(tmp_path):
    source = tmp_path / "Setups.csv"
    source.write_text(
        "Ticker;Name;Strategie;Setup_Typ;Pattern;Status2;CRV1;Einstieg;Stop;TP1\n"
        "ABC;Alpha AG;Trendfolge;Pullback;Hammer;ACHTUNG;1,5;100;95;110\n"
        "ABC;Alpha AG;Trendfolge;Breakout;Range-Ausbruch;ACHTUNG;1,7;102;97;115\n",
        encoding="utf-8",
    )
    report = (
        "5. Technische Kandidaten\nKeine belastbaren technischen Kandidaten.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\nAUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\nNICHT HANDELBAR: Keine Kandidaten\n"
        "VERWERFEN / INVALIDIERT: Keine Kandidaten\n6. Portfolio & Risiko\n"
    )
    result = production._trading_radar_kandidatenquellen_absichern(report, {source.name: str(source)})
    setup_ids = re.findall(r"Setup-ID: ([0-9a-f]{16})", result)
    assert len(set(setup_ids)) == 2


def test_exact_same_ticker_and_setup_from_overlapping_exports_is_deduplicated(tmp_path):
    first = tmp_path / "Setups(2026-10-30).csv"
    second = tmp_path / "Setups_backup(2026-10-30).csv"
    header = "Ticker;Name;Strategie;Setup_Typ;Pattern;Status2;CRV1;Einstieg;Stop;TP1\n"
    row = "ABC;Alpha AG;Trendfolge;Pullback;Hammer;ACHTUNG;1,5;100;95;110\n"
    first.write_text(header + row, encoding="utf-8")
    second.write_text(header + row, encoding="utf-8")
    report = "5. Technische Kandidaten\nAlt\n6. Portfolio & Risiko\n"
    result = production._trading_radar_kandidatenquellen_absichern(report, {
        first.name: str(first), second.name: str(second),
    })
    assert result.count("Asset/Ticker: Alpha AG (ABC)") == 1

def test_report_gate_ignores_candidate_section_preamble_as_a_candidate_record():
    report = (
        "5. Technische Kandidaten\n"
        "Kandidatenquellen: source.json\nQuellen-Prüstatus: Quelle inventarisiert\n\n"
        "NICHT HANDELBAR:\n"
        "- Asset/Ticker: Example AG (EXA)\n"
        "  - Trigger: Kein quellenbelegter Trigger; nicht freigegeben.\n"
        "  - Einstieg: NICHT VERFÜGBAR (nicht in der Quelle)\n"
        "  - Stop: NICHT VERFÜGBAR (nicht in der Quelle)\n"
        "  - TP1: NICHT VERFÜGBAR (nicht in der Quelle)\n"
        "  - TP2: NICHT VERFÜGBAR (nicht in der Quelle)\n"
        "  - CRV: NICHT VERFÜGBAR (nicht in der Quelle)\n"
        "  - Gegenargument: kein bestätigtes Einstiegssignal.\n"
        "  - Regel-/Datenstatus: Quelle liefert keine Handelslevels.\n"
        "  - Statusgruppe: NICHT HANDELBAR\n"
        "BESTÄTIGTE SETUPS: 0 bestätigte Setups – Begründung: kein bestätigter Trigger.\n"
        "WATCHLIST: 0 Watchlist-Einträge – Begründung: kein überprüfbarer Trigger.\n"
        "VERWORFENE KANDIDATEN: 0 Kandidaten – Begründung: keine.\n"
    )
    # The report-gate candidate parser is intentionally exercised through the
    # candidate sub-validator; the inventory preamble must not become record 1.
    errors = []
    gate_source = (ROOT / "trading_radar_report_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(gate_source)
    validate = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "validate_report")
    assert validate is not None
    # Validate the full fixture with a report that has the remaining headings.
    full = (
        "1. Trading-Radar\nIndizes/Marktbreite: S&P 500 +1.0%; Marktbreite 8/11.\n"
        "Zinsen/Renditen: US 10Y Treasury bei 4.5%.\nDollar/Liquidität/Volatilität: VIX 18.0 und Dollarproxy DXY 100.0.\n"
        "Marktregime: Restriktives Zinsregime mit selektiver Rotation.\n"
        "Basisszenario: NICHT ABLEITBAR – Datenlücke: Indikatoren fehlen.\n"
        "Positives Alternativszenario: NICHT ABLEITBAR – Datenlücke: Indikatoren fehlen.\n"
        "Negatives Alternativszenario: NICHT ABLEITBAR – Datenlücke: Indikatoren fehlen.\n"
        "2. Thesen-Update\nKeine neue oder aktualisierte These.\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
        "4. Assetklassenvergleich\nRelative Stärke und Rotation der Anlageklassen.\n"
        + "5. Technische Kandidaten\nKandidatenquellen: source.json\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\n"
        "AUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\n" + report.split("5. Technische Kandidaten\n", 1)[1] +
        "6. Portfolio & Risiko\nRisiko und Konzentration sind beschrieben.\n"
        "7. MSCI-Vergleich\nPortfolio-Gesamtperformance: NICHT VERFÜGBAR.\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "8. Daten-/Prüfanhang\nOHLCV-Abdeckung: 100/100. Keine Datenlücken; Kandidaten nicht eingeschränkt.\n"
        "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
    )
    errors = validate_report(full)
    assert not any("M-09: Kandidat 1 benötigt" in error for error in errors)
    assert not any("M-06:" in error for error in errors)
    assert audit_market_first_content(full) == [] or not any(
        "Kandidat" in error or "Kandidatengruppe" in error
        for error in audit_market_first_content(full)
    )


def test_treasury_provenance_uses_real_newlines():
    report = "US 10Y Treasury: 4.98%.\nUS 10Y Treasury: 4.93%."
    result = production._trading_radar_treasury_provenienz_ergänzen(report)
    assert "\\nUS 10Y Treasury" not in result
    assert result.count("\n") >= 1
    assert result.count("Quelle: NICHT VERFÜGBAR") == 2


def test_main_workflow_persists_thesis_register_and_reads_uppercase_gate_output():
    workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
    assert "Trading-Radar-Thesenregister wiederherstellen" in workflow
    assert "Trading-Radar-Thesenregister dauerhaft im Actions-Cache sichern" in workflow
    assert "steps.trading-radar-report-check.outputs.TRADING_RADAR_FACHLICH_FINAL" in workflow
    assert "steps.trading-radar-report-check.outputs.fachlich_final" not in workflow


def test_thesis_register_does_not_infer_empty_baseline_from_missing_history(tmp_path):
    from trading_radar_thesen import update_register
    payload = {"data_cutoff": "2026-11-01T12:00:00+00:00", "theses": []}
    report = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(payload) + "\n```"
    path = tmp_path / ".trading_radar_thesen.json"
    result = update_register(report, str(path))
    assert result["updated"] is False
    assert result["comparison_status"] == "BASELINE_MISSING"
    assert not path.exists()


def test_thesis_register_baseline_missing_is_explicit_then_next_run_compares(tmp_path):
    from trading_radar_thesen import update_register
    thesis = {
        "thesis_id": "macro-001", "thesis": "Belegte Markthese", "horizon": "2-4 Wochen",
        "alternative": "Defensive Sektoren verlieren ihre relative Stärke.",
        "evidence": ["Messwert A"], "counter_evidence": ["Gegenbeleg B"],
        "confidence": "mittel", "confirm_triggers": ["Trigger A"],
        "invalidate_triggers": ["Trigger B"], "event_dependencies": [],
        "asset_classes": ["Aktien"], "candidates": [], "status": "NEU",
    }
    payload = {"data_cutoff": "2026-11-01T12:00:00+00:00", "theses": [thesis]}
    report = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(payload) + "\n```"
    path = tmp_path / ".trading_radar_thesen.json"
    first = update_register(report, str(path))
    assert first["updated"] is True
    assert first["comparison_status"] == "BASELINE_MISSING"
    second = update_register(report, str(path))
    assert second["updated"] is True
    assert second["comparison_status"] == "PASS"


def test_thesis_evidence_replaces_false_no_change_claim_when_baseline_missing():
    result = production._trading_radar_thesenregister_nachweis_text(
        "2. Thesen-Update\nKeine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.\n"
        "3. Ereignisradar\n",
        ["THESENREGISTER-VERGLEICH: BASELINE_MISSING", "THESENREGISTER-GRUND: Vorzustand fehlt"],
    )
    assert "Thesenregister geprüft, keine Änderung" not in result
    assert "Vorzustand/strukturierter Nachweis fehlt" in result
    assert "THESENREGISTER-VERGLEICH: BASELINE_MISSING" in result


def test_thesis_register_update_in_exclusive_radar_branch_uses_radar_text():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    start = source.index("# M-05: Register aus genau diesem eigenständigen Radar-Bericht")
    end = source.index("# The persisted status marker", start)
    block = source[start:end]
    assert "update_register(" in block
    assert "text, os.path.join(os.getcwd(), \".trading_radar_thesen.json\")" in block
    assert "_trading_radar_thesenregister_nachweis_text" in block


def test_performance_tab2_presence_checks_actual_supplied_source_path():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert "os.path.basename(source_path).casefold() == \"geschlossene positionen_tab2.csv\"" in source
    assert "if not tab2_sources_available:" in source


def test_candidate_sources_are_normalized_before_ohlcv_candidate_mapping():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    start = source.index("def speichere_ergebnis(")
    end = source.index("def _write_github_output", start) if "def _write_github_output" in source[start:] else len(source)
    branch = source[start:end]
    assert branch.index("_trading_radar_kandidatenquellen_absichern(text, eingabedateien)") < branch.index("candidate_tickers = _trading_radar_kandidaten_aus_abschnitt5(text)")


def test_treasury_provenance_is_applied_in_the_exclusive_radar_branch():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    start = source.index("def speichere_ergebnis(")
    end = source.index("def _write_github_output", start) if "def _write_github_output" in source[start:] else len(source)
    branch = source[start:end]
    assert "_trading_radar_treasury_provenienz_ergänzen(" in branch
    assert branch.index("_trading_radar_treasury_provenienz_ergänzen(") < branch.index("report_gate_input =")


def test_benchmark_live_source_is_parsed_and_hash_bound(tmp_path):
    source = tmp_path / "Benchmark_Live.txt"
    source.write_text(
        "LIVE-BENCHMARK vs. MSCI WORLD\n"
        "Start der Systemrechnung / Trading-Engine: 07.08.2026\n"
        "Benchmark: iShares Core MSCI World UCITS ETF USD (Acc) | Ticker: EUNL.DE\n"
        "Methode: gleichgewichteter Trade-/Signalvergleich; kein Backtest.\n"
        "LIVE-SYSTEM SEIT 07.08.2026\n"
        "Ø System-Performance: +8,00%\n"
        "Ø MSCI-World-Performance im jeweils gleichen Zeitraum: +5,00%\n"
        "Out-/Underperformance: +3,00%\n"
        "Positionen besser als MSCI World: 4/4\n"
        "Positionen schlechter als MSCI World: 0/4\n"
        "Positionen gleichauf: 0/4\n"
        "TRADE-DETAILS\n"
        "AAA | Long | Scanner | 07.08.2026–01.11.2026 | System +8.00% | MSCI +5.00% | Delta +3.00%\n"
        "BBB | Long | Scanner | 07.08.2026–01.11.2026 | System +10.00% | MSCI +8.00% | Delta +2.00%\n"
        "CCC | Short | Scanner | 07.08.2026–01.11.2026 | System +6.00% | MSCI +4.00% | Delta +2.00%\n"
        "DDD | Long | Scanner | 07.08.2026–01.11.2026 | System +8.00% | MSCI +3.00% | Delta +5.00%\n",
        encoding="utf-8",
    )
    report = (
        "7. MSCI-Vergleich\nBenchmark-Performance: NICHT VERFÜGBAR\n"
        "8. Daten-/Prüfanhang\n"
    )
    result = production._trading_radar_benchmark_live_bericht_einsetzen(
        report, {"Benchmark_Live.txt": str(source)}
    )
    assert "Benchmark-Performance: +5.00%" in result
    assert "Benchmark-Vergleichsbasis: LIVE-SYSTEM" in result
    assert "System-Performance-Live: +8.00%" in result
    assert "Out-/Underperformance: +3.00 Prozentpunkte" in result
    assert "SHA256:" in result


def test_m01_gate_validates_live_system_delta_separately_from_portfolio_total():
    import importlib.util
    spec = importlib.util.spec_from_file_location("live_benchmark_fixture", ROOT / "tests/test_trading_radar_report_gate.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR; Ø Positionsperformance: NICHT VERFÜGBAR.\n"
        "Performance-Datenlücken: Berechnungswährung fehlt; keine belegte gemeinsame Währung.",
        "Benchmark-Performance: +5,00%\n"
        "Benchmark-Vergleichsbasis: LIVE-SYSTEM; gleichgewichteter Trade-/Signalvergleich; gleicher Zeitraum je Trade.\n"
        "System-Performance-Live: +8,00%\n"
        "Out-/Underperformance: +3,00 Prozentpunkte\n"
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR\n"
        "Performance-Datenlücken: vollständiger Tab-2-Export fehlt.",
    )
    errors = validate_report(report)
    assert not any("Out-/Underperformance" in error for error in errors)


def test_treasury_provenance_matches_real_macro_schema_without_percent_sign():
    macro = (
        "US 10Y Treasury: 5.0000 | Datenstand=2026-10-23 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-10-30T07:12:00+00:00 | DELAY_DAYS=7 | UNIT=%"
    )
    report = "US 10Y Treasury: 5.0000%."
    result = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {"Makro_Briefing.txt": "/tmp/Makro_Briefing.txt"}
    )
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "Abrufzeit: 2026-10-30T07:12:00+00:00" in result
    assert "Datenstichtag: 2026-10-23" in result
    assert "Einheit: %" in result
    assert "Verzögerung: 7 Kalendertage" in result
    assert "Quelle: NICHT VERFÜGBAR" not in result


def test_treasury_provenance_does_not_invent_missing_retrieval_metadata():
    macro = (
        "US 10Y Treasury: 5.0000 | Datenstand=2026-10-23 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)\n"
        "US 10Y Treasury: 5.0000 | Datenstand=2026-10-23 | STATUS=REAL"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(
        "US 10Y Treasury: 5.0000%.", macro, {}
    )
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "Datenstichtag: 2026-10-23" in result
    assert "Abrufzeit: NICHT VERFÜGBAR" in result
    assert "Verzögerung: NICHT VERFÜGBAR" in result


def test_fred_snapshot_emits_saved_retrieval_time_and_delay_for_treasury(monkeypatch):
    import datetime as dt
    import sys
    import types
    import pandas as pd
    sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame()))
    import makro_szenario as macro_module
    saved_at = dt.datetime(2026, 10, 30, 7, 12, tzinfo=dt.timezone.utc).timestamp()
    monkeypatch.setattr(
        macro_module, "fred_series",
        lambda series_id: pd.DataFrame({"DATE": [pd.Timestamp("2026-10-23")], series_id: [5.0]}),
    )
    monkeypatch.setattr(
        macro_module, "_cache_load",
        lambda: {"fred": {"DGS10": {
            "saved_at": saved_at, "source": "https://api.stlouisfed.org/fred/series/observations",
            "status": "REAL",
        }}},
    )
    monkeypatch.setattr(macro_module.dt, "datetime", dt.datetime)
    result = macro_module.fred_snapshot("US 10Y Treasury", "DGS10")
    assert "RETRIEVED_AT=2026-10-30T07:12:00+00:00" in result
    assert "DELAY_DAYS=" in result
    assert "UNIT=%" in result


def test_benchmark_live_rejects_aggregate_counts_that_do_not_reconcile(tmp_path):
    source = tmp_path / "Benchmark_Live.txt"
    source.write_text(
        "Start der Systemrechnung / Trading-Engine: 07.08.2026\n"
        "Benchmark: iShares Core MSCI World UCITS ETF USD (Acc) | Ticker: EUNL.DE\n"
        "Methode: gleichgewichteter Trade-/Signalvergleich; kein Backtest.\n"
        "Ø System-Performance: +8,00%\n"
        "Ø MSCI-World-Performance im jeweils gleichen Zeitraum: +5,00%\n"
        "Out-/Underperformance: +3,00%\n"
        "Positionen besser als MSCI World: 3/4\n"
        "Positionen schlechter als MSCI World: 0/4\n"
        "Positionen gleichauf: 0/4\n"
        "TRADE-DETAILS\n"
        "AAA | Long | Scanner | 07.08.2026–01.11.2026 | System +8.00% | MSCI +5.00% | Delta +3.00%\n"
        "BBB | Long | Scanner | 07.08.2026–01.11.2026 | System +10.00% | MSCI +8.00% | Delta +2.00%\n"
        "CCC | Short | Scanner | 07.08.2026–01.11.2026 | System +6.00% | MSCI +4.00% | Delta +2.00%\n"
        "DDD | Long | Scanner | 07.08.2026–01.11.2026 | System +8.00% | MSCI +3.00% | Delta +5.00%\n",
        encoding="utf-8",
    )
    report = "7. MSCI-Vergleich\nBenchmark-Performance: NICHT VERFÜGBAR\n\n8. Daten-/Prüfanhang\n"
    result = production._trading_radar_benchmark_live_bericht_einsetzen(
        report, {"Benchmark_Live.txt": str(source)}
    )
    assert "Benchmark-Performance: NICHT VERFÜGBAR" in result
    assert "rechnerisch inkonsistent" in result


def test_m02_market_regime_requires_a_common_data_cutoff_and_all_core_observations():
    from trading_radar_report_gate import validate_report
    report = (
        "1. Trading-Radar\n"
        "Marktregime: Selektives Risk-on mit defensiver Rotation.\n"
        "Indizes/Marktbreite: S&P 500 +1,0%; Marktbreite 8/11.\n"
        "Zinsen/Renditen: US 10Y Treasury bei 4,20%.\n"
        "Dollar/Liquidität/Volatilität: VIX 18,0; DXY 100,0.\n"
        "\n2. Thesen-Update\n"
    )
    full = report + "\n".join(
        f"{i}. {title}\nAbschnitt mit ausreichendem Inhalt und nachvollziehbarer Datenlage."
        for i, title in enumerate([
            "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
            "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang",
        ], start=2)
    ) + "\nTRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n"
    errors = validate_report(full)
    assert any("M-02: Datenstichtag" in error for error in errors)


def test_m04_gate_rejects_relative_strength_without_shared_cutoff():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m04_cutoff", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    text = report.report_fixture().replace(
        "Datenstichtag: 2026-10-30. Fenster: 1M und 3M.",
        "Fenster: 1M und 3M.",
    )
    errors = validate_report(text)
    assert any("M-04: gemeinsamer Datenstichtag" in error for error in errors)


def test_m06_event_dependency_is_assigned_to_event_wait_group(tmp_path):
    source = tmp_path / "hebeltrader_candidates.json"
    source.write_text(json.dumps({
        "candidates": [{
            "ticker": "EVT",
            "name": "Event Candidate",
            "status": "WARTET AUF EREIGNIS",
            "event_dependencies": ["FOMC 2026-11-05"],
            "risiken": ["Zinsentscheid kann Volatilität auslösen"],
        }]
    }), encoding="utf-8")
    report = (
        "5. Technische Kandidaten\nKeine belastbaren technischen Kandidaten.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\nAUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\nNICHT HANDELBAR: Keine Kandidaten\n"
        "VERWERFEN / INVALIDIERT: Keine Kandidaten\n6. Portfolio & Risiko\n"
    )
    result = production._trading_radar_kandidatenquellen_absichern(
        report, {source.name: str(source)}
    )
    assert "EREIGNIS ABWARTEN:\n" in result
    assert "Ereignisabhängigkeit: FOMC 2026-11-05" in result
    assert "WATCHLIST: Name: Event Candidate (EVT)" in result


def test_m09_tp2_is_optional_when_entry_plan_has_crv1_at_least_one():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_tp2_optional", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    text = fixture.report_fixture().replace(
        "TP2: 120 | ", "TP2: NICHT VERFÜGBAR (nicht erforderlich) | "
    )
    errors = validate_report(text)
    assert not any("M-09: Kandidat 1 benötigt" in error for error in errors), errors


def test_m08_run_evidence_rejects_missing_run_identity_and_source_hashes(tmp_path):
    from trading_radar_report_gate import validate_run_evidence
    path = tmp_path / "run_evidence.json"
    path.write_text(json.dumps({"start": {}, "end": {}}), encoding="utf-8")
    errors = validate_run_evidence(path)
    assert any("M-08: Run-Evidence enthält keine run_identity" in error for error in errors)


def test_m01_reconciles_the_actual_run413_live_benchmark_snapshot():
    fixture = ROOT / "tests" / "fixtures" / "Benchmark_Live_run413_2026-11-01.txt"
    report = (
        "7. MSCI-Vergleich\nBenchmark-Performance: NICHT VERFÜGBAR\n\n"
        "8. Daten-/Prüfanhang\n"
    )
    result = production._trading_radar_benchmark_live_bericht_einsetzen(
        report, {"Benchmark_Live.txt": str(fixture)}
    )
    assert "Benchmark-Performance: +0.80%" in result
    assert "System-Performance-Live: -5.74%" in result
    assert "Out-/Underperformance: -6.54 Prozentpunkte" in result
    assert "Trades=11" in result
    assert "besser=4; schlechter=7; gleichauf=0; gesamt=11" in result


def test_m03_m06_parse_actual_scanner_csv_schema_status2_setup_pattern_and_crv1(tmp_path):
    source = tmp_path / "Setups(2026-10-30).csv"
    source.write_text(
        "Ticker;Name;Setup_Typ;Pattern;Status2;Status_Grund;CRV1;Einstieg;Stop;TP1;TP2;Risikohinweis\n"
        "AAA;Alpha AG;Pullback-Zone;Hammer;VALIDE;Alles ok;1,95;100;95;110;;Risiko bleibt Marktlage\n"
        "BBB;Beta AG;EMA-Breakout;Kein;ACHTUNG;Bärischer MACD-Trend;1,20;50;47;54;;Momentum schwach\n",
        encoding="utf-8",
    )
    trendwende = tmp_path / "Trendwende_Setups(2026-10-30).csv"
    trendwende.write_text("Ticker;Name;Kurs;Stop;TP1;CRV1\n", encoding="utf-8")
    metals = tmp_path / "Edelmetalle_Setups(2026-10-30).csv"
    metals.write_text("Ticker;Name;Kurs;Stop;TP1;CRV1\n", encoding="utf-8")
    report = (
        "5. Technische Kandidaten\nKeine belastbaren technischen Kandidaten.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\nAUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\nNICHT HANDELBAR: Keine Kandidaten\n"
        "VERWERFEN / INVALIDIERT: Keine Kandidaten\n6. Portfolio & Risiko\n"
    )
    result = production._trading_radar_kandidatenquellen_absichern(
        report, {
            source.name: str(source),
            trendwende.name: str(trendwende),
            metals.name: str(metals),
        }
    )
    assert "Kandidatenquellen-Prüfstatus: VOLLSTÄNDIG" in result
    assert "Asset/Ticker: Alpha AG (AAA)" in result
    assert "Scanner-Setup: Pullback-Zone; Chartmuster: Hammer" in result
    assert "Statusgruppe: JETZT TECHNISCH PRÜFEN" in result
    assert "Asset/Ticker: Beta AG (BBB)" in result
    assert "Statusgruppe: AUF TRIGGER WARTEN" in result
    assert "TP2: NICHT VERFÜGBAR" in result


def test_m01_live_benchmark_in_section7_is_not_misclassified_as_external_portfolio_claim():
    import importlib.util
    from trading_radar_report_gate import validate_report
    spec = importlib.util.spec_from_file_location(
        "fixture_for_live_benchmark_gate", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture()
    start = report.index("7. MSCI-Vergleich")
    end = report.index("8. Daten-/Prüfanhang")
    section = production._trading_radar_benchmark_live_bericht_einsetzen(
        "7. MSCI-Vergleich\nBenchmark-Performance: NICHT VERFÜGBAR\n\n8. Daten-/Prüfanhang\n",
        {"Benchmark_Live.txt": str(ROOT / "tests/fixtures/Benchmark_Live_run413_2026-11-01.txt")},
    )
    replacement = section[section.index("7. MSCI-Vergleich"):section.index("8. Daten-/Prüfanhang")]
    replacement += (
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR\n"
        "Performance-Datenlücken: vollständiger Tab-2-Export fehlt.\n"
    )
    report = report[:start] + replacement + report[end:]
    errors = validate_report(report)
    assert not any("M-01: Numerische System-/Benchmark-Performance außerhalb" in error for error in errors)


def test_m07_prefers_one_complete_macro_observation_over_duplicate_summary_line():
    macro = (
        "US 10Y Treasury: 5.0000 | Datenstand=2026-10-23 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-10-30T07:12:00+00:00 | DELAY_DAYS=7 | UNIT=%\n"
        "US 10Y Treasury: 5.0000 | Datenstand=2026-10-23 | STATUS=REAL"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(
        "US 10Y Treasury: 5.00%.", macro, {}
    )
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "Abrufzeit: 2026-10-30T07:12:00+00:00" in result
    assert "Verzögerung: 7 Kalendertage" in result


def test_m03_source_inventory_cannot_be_complete_when_required_scanner_family_is_missing(tmp_path):
    source = tmp_path / "Setups(2026-10-30).csv"
    source.write_text("Ticker;Name;Status2;Setup_Typ;Einstieg;Stop;TP1;CRV1\n", encoding="utf-8")
    report = (
        "5. Technische Kandidaten\nKeine belastbaren technischen Kandidaten.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\nAUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\nNICHT HANDELBAR: Keine Kandidaten\n"
        "VERWERFEN / INVALIDIERT: Keine Kandidaten\n6. Portfolio & Risiko\n"
    )
    result = production._trading_radar_kandidatenquellen_absichern(
        report, {source.name: str(source)}
    )
    assert "Kandidatenquellen-Prüfstatus: UNVOLLSTÄNDIG" in result
    assert "Pflicht-Scannerquellen fehlen" in result


def test_m05_exclusive_mode_uses_one_register_update_and_never_overwrites_processed_report():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    first_update = source.index("register_result = update_register(")
    strict_check = source.rfind("strict_thesis_findings = validate_radar_report_strict(text)", 0, first_update)
    validation_guard = source.rfind("elif validation_errors:", 0, first_update)
    second_save_guard = source.index('and not globals().get("_GEMINI_TRADING_RADAR_ONLY_MODE")')
    exclusive_skip = source.index("kein zweites Registerupdate")
    assert strict_check >= 0 and validation_guard < strict_check
    assert "Thesis lifecycle is independent of unrelated report blockers" in source
    assert second_save_guard > first_update
    assert exclusive_skip > second_save_guard



def test_report_section_heading_validator_accepts_dot_and_middle_dot_styles():
    from trading_radar_neuauswertung import _canonicalize_radar_heading_style
    report = "\n".join([
        "### 1. Trading-Radar", "2. Thesen-Update", "### 3. Ereignisradar",
        "### 4. Assetklassenvergleich", "### 5. Technische Kandidaten",
        "### 6. Portfolio & Risiko", "### 7. MSCI-Vergleich",
        "### 8. Daten-/Prüfanhang",
    ])
    normalized = _canonicalize_radar_heading_style(report)
    for heading in (
        "1 · Trading-Radar", "2 · Thesen-Update", "3 · Ereignisradar",
        "4 · Assetklassenvergleich", "5 · Technische Kandidaten",
        "6 · Portfolio & Risiko", "7 · MSCI-Vergleich", "8 · Daten-/Prüfanhang",
    ):
        assert heading in normalized


def test_structured_thesis_block_is_built_only_from_complete_explicit_fields():
    from trading_radar_neuauswertung import ensure_structured_thesis_block, validate_radar_report_strict
    report = """### 1. Trading-Radar
Hauptanalyse: Defensive Sektorrotation mit selektiver Stärke in Energie und Rohstoffen.
Zeithorizont: Kurz- bis mittelfristig (4 bis 8 Wochen)
Konfidenz: Mittel
Positives Alternativszenario: Energie und Gesundheit halten ihre relative Stärke.
Negatives Alternativszenario: Technologie gewinnt die relative Stärke zurück.
Datenstand UTC: 2026-11-01T18:09:41+00:00
Stärkste Belege:
- VIX bei 19,42 und DXY bei 101,88.
- Acht von elf Sektor-ETFs über dem 50-Tage-Durchschnitt.
Wichtigste Gegenbelege:
- Nasdaq auf 3M-Basis schwächer.
Belegte Top-Profiteur-Kandidaten:
- Beispiel AG (ABC)
### 2. Thesen-Update
- Thesen-ID: ROTATION-01
- Version: 1.0
- Status: BESTÄTIGT
Gegenbelege: Nasdaq-Schwäche bleibt ein Gegenargument.
Bestätigungstrigger: Energie und Qualität schlagen den MSCI World weiter.
Invalidierungstrigger: Breiter Rückfall unter die 200-Tage-Linien.
### 3. Ereignisradar
Ereignisdetails fehlen und werden ausdrücklich nicht geschätzt.
### 4. Assetklassenvergleich
Relative Stärke zwischen Regionen, Indizes, Sektoren und Anlageklassen.
Gemeinsamer Datenstichtag: 2026-10-30T00:00:00+00:00
### 5. Technische Kandidaten
Keine Kandidaten mit vollständig bestätigten Handelslevels und CRV.
### 6. Portfolio & Risiko
Das Portfolio wird anhand der verfügbaren Positionsdaten und Risiken bewertet.
### 7. MSCI-Vergleich
Der Benchmarkvergleich wird separat mit gleicher Zeitbasis ausgewiesen.
### 8. Daten-/Prüfanhang
Datenqualität, Quellenstände und verbleibende Einschränkungen werden dokumentiert.
"""
    result, created = ensure_structured_thesis_block(report)
    assert created is True
    assert "TRADING_RADAR_THESEN_JSON" in result
    assert validate_radar_report_strict(result) == []


def test_structured_thesis_block_not_synthesized_when_required_evidence_is_missing():
    from trading_radar_neuauswertung import ensure_structured_thesis_block
    report = """1. Trading-Radar
Hauptanalyse: These.
2. Thesen-Update
Thesen-ID: X
Version: 1
Status: BESTÄTIGT
3. Ereignisradar
"""
    result, created = ensure_structured_thesis_block(report)
    assert created is False
    assert "TRADING_RADAR_THESEN_JSON" not in result


def test_ohlcv_appendix_emits_machine_readable_candidate_mapping_label():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert "Kandidaten-Mapping: VOLLSTÄNDIG" in source
    assert "NICHT VERIFIZIERBAR" in source
    assert "Universumsweite OHLCV-Datenlücken/-veraltung" in source


def test_thesis_baseline_persistence_is_not_blocked_by_unrelated_report_findings():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    start = source.index("strict_thesis_findings = validate_radar_report_strict(text)")
    end = source.index("if register_result.get(\"updated\"):", start)
    block = source[start:end]
    assert "if strict_thesis_findings:" in block
    assert "update_register(" in block
    assert "elif validation_errors:" not in block
    assert "overall report remains NICHT_FINAL" in block


def test_scenarios_accept_full_iso_timestamp_for_common_matrix_cutoff():
    report = (
        "1. Trading-Radar\n"
        "Zeithorizont: 4 bis 8 Wochen\n"
        "Basisszenario: NICHT ABLEITBAR – Datenlücke: gemeinsamer Datenstichtag der Relative-Stärke-Matrix\n"
        "Marktregime: Selektives defensives Rotationsregime.\n"
        "Indizes/Marktbreite: 8/11 Sektor-ETFs über ihrem 50-Tage-Durchschnitt.\n"
        "Volatilität VIX: 19.42\n"
        "\n4. Assetklassenvergleich\n"
        "Relative-Stärke-Matrix mit gemeinsamem Datenstichtag.\n"
        "Gemeinsamer Datenstichtag: 2026-10-30T00:00:00+00:00\n"
        "Sektoren: XLV (XLV) 1M +11.23%; 3M +14.38% | XLE (XLE) 1M +11.67%; 3M +19.81%\n"
        "\n5. Technische Kandidaten\nKeine Kandidaten.\n"
        "\n2. Thesen-Update\n"
    )
    result = production._trading_radar_szenarien_ergänzen(report)
    assert "Gemeinsamer Datenstichtag: 2026-10-30." in result
    assert "Basisszenario: Selektives defensives Rotationsregime." in result
    assert "Basisszenario: NICHT ABLEITBAR" not in result
    assert "Eintrittsbedingung:" in result
    assert "Gesundheit-ETF (XLV) 3M +14.38%" in result
    assert "Energie-ETF (XLE) 3M +19.81%" in result


def test_candidate_inventory_preserves_explicit_no_actionable_candidates_when_sources_are_empty(tmp_path):
    files = {}
    for name in ("Setups.csv", "Trendwende_Setups.csv", "Edelmetalle_Setups.csv"):
        path = tmp_path / name
        path.write_text("Ticker;Name;Status2;Setup_Typ;Einstieg;Stop;TP1;CRV1\n", encoding="utf-8")
        files[name] = str(path)
    report = (
        "5. Technische Kandidaten\nVorherige Kandidatenprosa.\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\n"
        "AUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\n"
        "NICHT HANDELBAR: Keine Kandidaten\n"
        "6. Portfolio & Risiko\n"
    )
    result = production._trading_radar_kandidatenquellen_absichern(report, files)
    assert "Keine belastbaren technischen Kandidaten; keine Handelsfreigabe." in result
    assert "Kandidatenquellen-Prüfstatus: VOLLSTÄNDIG" in result
    assert all(label in result for label in ("BESTÄTIGTE SETUPS:", "WATCHLIST:", "VERWORFENE KANDIDATEN:"))


def test_m02_accepts_actual_german_us_treasury_wording_from_run413():
    import importlib.util
    from trading_radar_report_gate import validate_report
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m02_run413", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: 10-jähriger US-Staatsanleihenzins bei 4,98%.",
    )
    errors = validate_report(report)
    assert not any("M-02: US-10Y-Rendite" in error for error in errors), errors


def test_m03_mapping_status_is_emitted_even_when_ohlcv_quality_has_gaps(monkeypatch):
    audit = {
        "status": "gaps", "checked_at": "2026-11-01T19:30:00+00:00",
        "universe_count": 4, "covered_count": 4,
        "missing_tickers": [], "stale_tickers": ["STALE"],
        "affected_candidates": [], "unmapped_candidate_tickers": [],
        "candidate_mapping_complete": True,
        "quality_status_counts": {"VALID": 2, "MISSING": 0, "STALE": 1, "INVALID": 1, "TOTAL": 4},
        "quality_status_by_ticker": {
            "AAA": "VALID", "BBB": "VALID", "STALE": "STALE", "BAD": "INVALID",
        },
        "invalid_ohlcv_tickers": ["BAD"], "recent_invalid_ohlcv_tickers": [],
        "historical_only_invalid_ohlcv_tickers": ["BAD"],
    }
    monkeypatch.setattr(production, "_TRADING_RADAR_OHLCV_AUDIT", audit, raising=False)
    result = production._trading_radar_ohlcv_nachweisblock(
        "5. Technische Kandidaten\nAsset/Ticker: Example AG (AAA)\n"
    )
    assert "Kandidaten-Mapping: VOLLSTÄNDIG" in result
    assert "OHLCV-Qualitätsstatus:" in result
    assert "STALE=1" in result


def test_m03_mapping_is_explicitly_not_verifiable_when_candidate_map_is_incomplete(monkeypatch):
    audit = {
        "status": "unavailable", "checked_at": "2026-11-01T19:30:00+00:00",
        "universe_count": 1, "covered_count": 1,
        "missing_tickers": [], "stale_tickers": [], "affected_candidates": [],
        "candidate_mapping_complete": False, "unmapped_candidate_tickers": ["ABC"],
        "quality_status_counts": {"VALID": 1, "MISSING": 0, "STALE": 0, "INVALID": 0, "TOTAL": 1},
        "quality_status_by_ticker": {"ABC": "VALID"},
    }
    monkeypatch.setattr(production, "_TRADING_RADAR_OHLCV_AUDIT", audit, raising=False)
    result = production._trading_radar_ohlcv_nachweisblock(
        "5. Technische Kandidaten\nAsset/Ticker: Example AG (ABC)\n"
    )
    assert "Kandidaten-Mapping: NICHT VERIFIZIERBAR" in result


def test_m07_provenance_recognizes_run413_german_treasury_label():
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-30 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-01T19:20:00+00:00 | DELAY_DAYS=2 | UNIT=%"
    )
    report = "Zinsen/Renditen: 10-jähriger US-Staatsanleihenzins bei 4,98%."
    result = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {"Makro_Briefing.txt": "/tmp/Makro_Briefing.txt"}
    )
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "Abrufzeit: 2026-11-01T19:20:00+00:00" in result
    assert "Datenstichtag: 2026-10-30" in result
    assert "Einheit: %" in result
    assert "Verzögerung: 2 Kalendertage" in result


def test_m05_standalone_radar_prompt_requires_structured_block_after_section2():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    marker = "Direkt nach Abschnitt 2 und VOR Abschnitt 3 muss zusätzlich genau ein"
    assert marker in source
    assert "TRADING_RADAR_THESEN_JSON" in source
    assert "Trading-Radar-Thesenregister-Struktur-Retry" in source
    assert "thesis_id" in source and "confirm_triggers" in source


def test_m01_benchmark_section_does_not_call_msc_world_msc_inc():
    source = ROOT / "tests" / "fixtures" / "Benchmark_Live_run413_2026-11-01.txt"
    report = (
        "4. Assetklassenvergleich\nRelative Stabilität gegenüber +0,80% MSCI Inc. (MSCI).\n"
        "5. Technische Kandidaten\nKeine Kandidaten.\n"
        "7. MSCI-Vergleich\nOut-/Underperformance: -6,54 Prozentpunkte (MSCI Inc. (MSCI)-Performance).\n"
        "8. Daten-/Prüfanhang\n"
    )
    result = production._trading_radar_benchmark_live_bericht_einsetzen(
        report, {"Benchmark_Live.txt": str(source)}
    )
    assert "MSCI World" in result
    assert "MSCI Inc. (MSCI)" not in result


def test_m05_missing_thesis_json_uses_one_bounded_structured_retry(monkeypatch, tmp_path):
    import os
    import types
    headings = (
        "1. Trading-Radar", "2. Thesen-Update", "3. Ereignisradar",
        "4. Assetklassenvergleich", "5. Technische Kandidaten",
        "6. Portfolio & Risiko", "7. MSCI-Vergleich", "8. Daten-/Prüfanhang",
    )
    report_parts = []
    for index, heading in enumerate(headings, start=1):
        content = (
            "Marktbeobachtung mit belastbaren gelieferten Zahlen und relativer Stärke. " * 30
            if index in (1, 4) else f"Vorhandener Abschnitt {index}."
        )
        report_parts.append(heading + "\n" + content)
    first_report = "\n\n".join(report_parts)
    structured = {
        "data_cutoff": "2026-11-01T19:30:53+00:00",
        "theses": [{
            "thesis_id": "ROTATION-2026-11-01",
            "version": 1,
            "updated_at": "2026-11-01T19:30:53+00:00",
            "data_cutoff": "2026-11-01T19:30:53+00:00",
            "thesis": "Defensive Qualität und Energie zeigen relative Stärke.",
            "horizon": "4 bis 8 Wochen",
            "alternative": "Wachstumssektoren gewinnen ihre relative Stärke zurück.",
            "evidence": ["XLE 3M +19.81%", "XLP 3M +6.07%"],
            "counter_evidence": ["Nasdaq 3M -1.84%"],
            "confidence": "mittel",
            "confirm_triggers": ["Defensive Sektoren halten ihre relative Stärke."],
            "invalidate_triggers": ["Breite Marktrotation zurück in Wachstumssektoren."],
            "event_dependencies": [],
            "asset_classes": ["Aktien", "Energie"],
            "candidates": [],
            "status": "NEU",
        }],
    }
    from trading_radar_neuauswertung import validate_thesis_register
    assert validate_thesis_register(structured) == []
    second_response = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(structured) + "\n```"
    responses = iter([
        types.SimpleNamespace(text=first_report),
        types.SimpleNamespace(text=second_response),
    ])
    calls = []

    monkeypatch.setattr(production, "_offene_positionen_quellblock", lambda *_: "OFFENE POSITIONEN")
    monkeypatch.setattr(production, "lade_offenen_positionen_check_tab2", lambda: "TAB 2")
    monkeypatch.setattr(production, "_gemini_tokenzahl", lambda *a, **k: 100)
    monkeypatch.setattr(production, "_gemini_radar_response_diagnose", lambda *a, **k: None)
    monkeypatch.setattr(production, "GEMINI_MODELLREIHENFOLGE", ("model-a",))
    monkeypatch.setattr(production, "GEMINI_MAX_503_POOL_PASSES_PER_REQUEST_PER_RUN", 1)
    monkeypatch.setattr(production, "_gemini_generate_content_quota_safe",
                        lambda *a, **k: (calls.append(a[5] if len(a) > 5 else k.get("label")) or next(responses)))
    monkeypatch.setattr(production, "_speichere_gemini_input_manifest", lambda *_: None)
    production._gemini_503_failed_models_by_request.clear()
    production._gemini_503_failures_this_run.clear()

    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = production._gemini_trading_radar_eigenstaendig(
            client=object(), preferred_modell="model-a",
            eingabedateien={}, bestehende_auswertung="",
        )
    finally:
        os.chdir(previous)

    assert len(calls) == 2
    assert calls[1] == "Trading-Radar-Thesenregister-Struktur-Retry"
    assert result.count("```TRADING_RADAR_THESEN_JSON") == 1
    assert result.index("2. Thesen-Update") < result.index("```TRADING_RADAR_THESEN_JSON")
    assert result.index("```TRADING_RADAR_THESEN_JSON") < result.index("3. Ereignisradar")


def test_m07_gate_catches_unprovenanced_german_treasury_conflict():
    import importlib.util
    from trading_radar_report_gate import validate_report
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_conflict", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: 10-jähriger US-Staatsanleihenzins bei 4,98%.",
    )
    report = report.replace(
        "Dollar/Liquidität/Volatilität:",
        "US-10J-Rendite (^TNX): 4.93% | Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-01T19:30:53+00:00; Datenstichtag: 2026-10-30; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs.\nDollar/Liquidität/Volatilität:",
    )
    errors = validate_report(report)
    assert not any("M-02: US-10Y-Rendite" in error for error in errors)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors)


def test_m03_unavailable_ohlcv_snapshot_still_labels_candidate_mapping_unverifiable(monkeypatch):
    monkeypatch.setattr(production, "_TRADING_RADAR_OHLCV_AUDIT", {
        "status": "unavailable", "checked_at": "NICHT VERFÜGBAR",
        "universe_count": None, "covered_count": None,
    }, raising=False)
    result = production._trading_radar_ohlcv_nachweisblock(
        "5. Technische Kandidaten\nAsset/Ticker: Example AG (EXA)\n"
    )
    assert "Kandidaten-Mapping: NICHT VERIFIZIERBAR" in result


def test_m07_provenance_reconciles_both_run413_treasury_observations():
    import importlib.util
    from trading_radar_report_gate import validate_report
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_provenance", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: 10-jähriger US-Staatsanleihenzins bei 4,98%.",
    )
    report = report.replace(
        "Dollar/Liquidität/Volatilität:",
        "US-10J-Rendite (^TNX): 4.93% | Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-01T19:30:53+00:00; Datenstichtag: 2026-10-30; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs.\nDollar/Liquidität/Volatilität:",
    )
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-30 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-01T19:20:00+00:00 | DELAY_DAYS=2 | UNIT=%"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {"Makro_Briefing.txt": "/tmp/Makro_Briefing.txt"}
    )
    errors = validate_report(result)
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors)



def test_multi_thesis_json_repairs_missing_alternatives_by_thesis_id_and_status_alias():
    report = """1. Trading-Radar
Datenstand UTC: 2026-11-02T08:11:03+00:00
Positives Alternativszenario: Kein Zeithorizont belegt.
Negatives Alternativszenario: Kein Zeithorizont belegt.

2. Thesen-Update
- Thesen-ID: T-QUALITY
  - Version: 2
  - Status: BESTAETIGT
  - Alternative: Rezession führt zu defensiver Umschichtung.
- Thesen-ID: T-ENERGY
  - Version: 3
  - Status: BESTAETIGT
  - Alternative: Industrieflaute belastet Energie- und Rohstoffwerte.

```TRADING_RADAR_THESEN_JSON
{"data_cutoff":"2026-11-02T08:11:03+00:00","theses":[
{"thesis_id":"T-QUALITY","version":2,"updated_at":"2026-11-02T08:11:03+00:00","data_cutoff":"2026-11-02T08:11:03+00:00","thesis":"Qualität outperformt","horizon":"4 Wochen","evidence":["Qualitätsstärke"],"counter_evidence":["Zinsrisiko"],"confidence":"mittel","confirm_triggers":["Outperformance"],"invalidate_triggers":["Gewinneinbruch"],"event_dependencies":[],"asset_classes":["Aktien"],"candidates":["AAA"],"status":"BESTAETIGT"},
{"thesis_id":"T-ENERGY","version":3,"updated_at":"2026-11-02T08:11:03+00:00","data_cutoff":"2026-11-02T08:11:03+00:00","thesis":"Energie hält sich","horizon":"4 Wochen","evidence":["Energienachfrage"],"counter_evidence":["Industrieflaute"],"confidence":"mittel","confirm_triggers":["Stärke"],"invalidate_triggers":["200T-Bruch"],"event_dependencies":[],"asset_classes":["Energie"],"candidates":["XOM"],"status":"BESTAETIGT"}
]}
```

3. Ereignisradar
Keine relevanten Ereignisse.
"""
    repaired, changed = ensure_structured_thesis_block(report)
    assert changed
    block = re.search(r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", repaired, re.S)
    payload = json.loads(block.group(1))
    assert [item["alternative"] for item in payload["theses"]] == [
        "Rezession führt zu defensiver Umschichtung.",
        "Industrieflaute belastet Energie- und Rohstoffwerte.",
    ]
    assert [item["status"] for item in payload["theses"]] == ["BESTÄTIGT", "BESTÄTIGT"]
    assert "Status: BESTÄTIGT" in repaired
    assert validate_thesis_register(payload) == []


def test_m02_m07_run1270_entity_normalized_treasury_observation_is_repaired_end_to_end():
    from trading_radar_report_gate import validate_report
    original = (
        ROOT / "tests" / "fixtures" / "trading_radar_run1270_original.txt"
    ).read_text(encoding="utf-8")
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-11-02 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-02T19:57:03+00:00 | DELAY_DAYS=0 | UNIT=%"
    )
    repaired = production._trading_radar_treasury_provenienz_ergänzen(
        original, macro, {"Makro_Briefing.txt": "/tmp/Makro_Briefing.txt"}
    )
    rates_line = next(line for line in repaired.splitlines() if line.startswith("Zinsen/Renditen:"))
    assert "(10Y) Treasury 4,98%" in rates_line
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in rates_line
    assert "Abrufzeit: 2026-11-02T19:57:03+00:00" in rates_line
    assert "Datenstichtag: 2026-11-02" in rates_line
    assert "Einheit: %" in rates_line
    assert "Verzögerung: 0 Kalendertage" in rates_line
    assert "Makro-Provenienz: Quelle=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in repaired
    findings = validate_report(repaired)
    assert not any("M-02: US-10Y-Rendite" in item for item in findings), findings
    # M-02 keeps the original normalized Treasury wording; M-07 correctly
    # remains blocked because the conflict row's Live-Wert is only partially
    # sourced and cannot borrow metadata from the rounded live snapshot.
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_treasury_provenance_second_pass_replaces_unavailable_placeholders():
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-11-02 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-02T19:57:03+00:00 | DELAY_DAYS=0 | UNIT=%"
    )
    report = (
        "US 10Y Treasury: 4.98% | Quelle: NICHT VERFÜGBAR | "
        "Abrufzeit: NICHT VERFÜGBAR | Datenstichtag: NICHT VERFÜGBAR | "
        "Einheit: NICHT VERFÜGBAR | Verzögerung: NICHT VERFÜGBAR"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert "Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "Abrufzeit: 2026-11-02T19:57:03+00:00" in result
    assert "Datenstichtag: 2026-11-02" in result
    assert "Einheit: %" in result
    assert "Verzögerung: 0 Kalendertage" in result
    assert "NICHT VERFÜGBAR" not in result


def test_m07_rejects_invalid_retrieval_timestamp_even_when_other_fields_exist():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_invalid_timestamp", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: gestern | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
    )
    report = report.replace(
        "Dollar/Liquidität/Volatilität:",
        "US-10J-Rendite (^TNX): 4.93% | Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-01T19:30:53+00:00; Datenstichtag: 2026-10-30; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs.\nDollar/Liquidität/Volatilität:",
    )
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_us10y_recognizer_does_not_treat_tips_or_thresholds_as_nominal_observations():
    from trading_radar_report_gate import extract_us10y_observations
    assert extract_us10y_observations("10Y TIPS 2,63%") == []
    assert extract_us10y_observations("10Y-Rendite über 5,05%") == []
    # Bare normalized labels are not geographically identifiable on their own.
    assert extract_us10y_observations("(10Y) Treasury 4,98%") == []
    run1270_curve = (
        "Zinsen/Renditen: ... (10Y) Treasury 4,98%, 2Y Treasury 4,61%, "
        "Realzins ... (10Y) TIPS 2,63%."
    )
    assert extract_us10y_observations(
        run1270_curve, allow_run1270_context=True
    ) == [Decimal("4.98")]


def test_us10y_recognizer_rejects_foreign_uk_and_japan_treasuries():
    from trading_radar_report_gate import extract_us10y_observations
    assert extract_us10y_observations("UK 10Y Treasury 4,25%") == []
    assert extract_us10y_observations("Japan 10Y Treasury 1,10%") == []
    assert extract_us10y_observations(
        "Zinsen/Renditen: UK 10Y Treasury 4,25%, 10Y TIPS 2,63%.",
        allow_run1270_context=True,
    ) == []
    assert extract_us10y_observations(
        "Zinsen/Renditen: Japan 10Y Treasury 1,10%, 10Y TIPS 2,63%.",
        allow_run1270_context=True,
    ) == []
    assert extract_us10y_observations("US 10Y Treasury 4,98%") == [Decimal("4.98")]


def test_m07_requires_provenance_even_for_a_single_treasury_value():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_single_unprovenanced_value", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 5,00%.",
    )
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_m07_deduplicates_identical_treasury_values_without_false_ambiguity():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_duplicate_identical_values", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% und US 10Y Treasury bei 5,00% | "
        "Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
    )
    findings = validate_report(report)
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings
    duplicate_line = (
        "US 10Y Treasury 5.00% und US 10Y Treasury 5.00% | "
        "Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | "
        "Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag"
    )
    enriched = production._trading_radar_treasury_provenienz_ergänzen(duplicate_line)
    assert "MEHRERE_WERTE_NICHT_EINDEUTIG_ZUORDENBAR" not in enriched


def test_m07_rejects_placeholder_sources_with_explanatory_suffixes():
    from trading_radar_report_gate import (
        _treasury_observation_has_complete_provenance, validate_report
    )
    import importlib.util
    line = (
        "US 10Y Treasury 4.98% | Quelle: NICHT VERFÜGBAR (unbekannt) | "
        "Abrufzeit: 2026-11-02T20:00:00+00:00 | Datenstichtag: 2026-11-02 | "
        "Einheit: % | Verzögerung: NICHT VERFÜGBAR (nicht ermittelt)"
    )
    assert not _treasury_observation_has_complete_provenance(line)
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_placeholder_suffix", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z",
        "Quelle: NICHT VERFÜGBAR (unbekannt) | Abrufzeit: 2026-10-30T04:30:00Z",
    )
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_m07_rejects_non_informative_source_and_delay_values():
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance
    base = (
        "US 10Y Treasury 4.98% | Quelle: {source} | "
        "Abrufzeit: 2026-11-02T20:00:00+00:00 | Datenstichtag: 2026-11-02 | "
        "Einheit: % | Verzögerung: {delay}"
    )
    assert not _treasury_observation_has_complete_provenance(
        base.format(source="siehe oben", delay="0 Kalendertage")
    )
    assert not _treasury_observation_has_complete_provenance(
        base.format(source="FRED DGS10", delay="offen")
    )



def test_m07_run1271_conflict_row_extracts_macro_and_live_values_independently():
    from trading_radar_report_gate import extract_us10y_observations, validate_report
    import importlib.util

    conflict_line = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.0000% (Quellzeile vorhanden; Quelle/Zeitstempel nicht eindeutig zuordenbar) | "
        "Live-Wert: 4.8940% (Quelle: Yahoo Finance ^TNX; Abruf: 2026-11-03T04:35:08+00:00) | "
        "Abgleich: nicht aufgelöst; keine der Beobachtungen wird überschrieben."
    )
    assert extract_us10y_observations(conflict_line) == [Decimal("4"), Decimal("4.894")]

    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_run1271_conflict", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: 2026-11-01T04:50:54+00:00 | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 5 Kalendertage.",
    )
    report = report.replace(
        "Dollar/Liquidität/Volatilität:",
        "US-10J-Rendite (^TNX): 4.89% | Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-03T04:35:08+00:00; Datenstichtag: 2026-11-02; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs.\n"
        + conflict_line + "\nDollar/Liquidität/Volatilität:",
    )
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings



def test_m07_live_conflict_provenance_cannot_cover_missing_macro_provenance():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_role_separation", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: 2026-11-01T04:50:54+00:00 | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 5 Kalendertage.",
    )
    conflict = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.0000% (Quellzeile vorhanden; Quelle/Zeitstempel nicht eindeutig zuordenbar) | "
        "Live-Wert: 4.8940% (Quelle: Yahoo Finance ^TNX; Abrufzeit: 2026-11-03T04:35:08+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst; keine der Beobachtungen wird überschrieben."
    )
    report = report.replace("Dollar/Liquidität/Volatilität:", conflict + "\nDollar/Liquidität/Volatilität:")
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_m07_accepts_conflict_only_when_both_role_specific_provenance_blocks_are_complete():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_both_roles", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: 2026-11-01T04:50:54+00:00 | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 5 Kalendertage.",
    )
    conflict = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.0000% (Makro-Provenienz: Quelle=FRED DGS10; "
        "Abrufzeit=2026-11-02T20:00:00+00:00; Datenstichtag=2026-11-02; Einheit=%; Verzögerung=0 Kalendertage) | "
        "Live-Wert: 4.8940% (Quelle: Yahoo Finance ^TNX; Abrufzeit: 2026-11-03T04:35:08+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst; keine der Beobachtungen wird überschrieben."
    )
    report = report.replace("Dollar/Liquidität/Volatilität:", conflict + "\nDollar/Liquidität/Volatilität:")
    findings = validate_report(report)
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings

def test_m07_rejects_url_only_generic_and_placeholder_source_suffixes():
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance
    base = (
        "US 10Y Treasury 4.98% | Quelle: {source} | "
        "Abrufzeit: 2026-11-02T20:00:00+00:00 | Datenstichtag: 2026-11-02 | "
        "Einheit: % | Verzögerung: 1 Börsentag"
    )
    rejected = (
        "https://api.stlouisfed.org/fred/series/observations",
        "FRED",
        "Yahoo Finance",
        "Datenquelle intern",
        "Quelle vorhanden",
        "FRED DGS10 (unbekannt)",
        "FRED DGS10 – Quelle vorhanden",
    )
    for source in rejected:
        assert not _treasury_observation_has_complete_provenance(base.format(source=source)), source
    assert _treasury_observation_has_complete_provenance(
        base.format(source="FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)")
    )
    assert _treasury_observation_has_complete_provenance(
        base.format(source="Yahoo Finance (^TNX)")
    )


def test_m07_accepts_only_numeric_delay_or_explicit_market_data_cadence():
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance
    base = (
        "US 10Y Treasury 4.98% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-11-02T20:00:00+00:00 | Datenstichtag: 2026-11-02 | "
        "Einheit: % | Verzögerung: {delay}"
    )
    for delay in (
        "wird nachgereicht", "noch zu prüfen", "tagesaktuell", "aktuell",
        "verzögert", "5 Kalendertage (wird nachgereicht)", "0 Tage; Quelle offen",
    ):
        assert not _treasury_observation_has_complete_provenance(base.format(delay=delay)), delay
    for delay in ("0 Kalendertage", "1 Börsentag", "15 Minuten", "Tages-Schlusskurs", "Realtime"):
        assert _treasury_observation_has_complete_provenance(base.format(delay=delay)), delay

def test_m07_partial_source_record_is_not_masked_by_complete_duplicate_of_same_value():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_partial_duplicate", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: NICHT VERFÜGBAR | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
    )
    report = report.replace(
        "Dollar/Liquidität/Volatilität:",
        "US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | "
        "Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.\n"
        "US-10J-Rendite (^TNX): 4.93% | Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-01T19:30:53+00:00; Datenstichtag: 2026-10-30; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs.\nDollar/Liquidität/Volatilität:",
    )
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_m07_rejects_two_distinct_treasury_values_sharing_one_provenance_line():
    from trading_radar_report_gate import validate_report
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_shared_provenance_line", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury 4,98% und US 10Y Treasury 4,93% | "
        "Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | "
        "Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
    )
    findings = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings


def test_m07_identical_macro_live_values_still_require_both_role_provenances():
    from trading_radar_report_gate import validate_report
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_identical_role_values", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: 2026-11-01T04:50:54+00:00 | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 5 Kalendertage.",
    )
    conflict = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.8940% (Quellzeile vorhanden; Makro-Provenienz fehlt) | "
        "Live-Wert: 4.8940% (Quelle: Yahoo Finance (^TNX); Abruf: 2026-11-03T04:35:08+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst."
    )
    report = report.replace("Dollar/Liquidität/Volatilität:", conflict + "\nDollar/Liquidität/Volatilität:")
    errors = validate_report(report)
    assert any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors), errors


def test_m07_identical_macro_live_values_pass_only_with_two_complete_role_sources():
    from trading_radar_report_gate import validate_report
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fixture_for_m07_identical_role_values_complete", ROOT / "tests/test_trading_radar_report_gate.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: US 10Y Treasury bei 4,98% | Quelle: FRED DGS10 | Abrufzeit: 2026-11-01T04:50:54+00:00 | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 5 Kalendertage.",
    )
    conflict = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.8940% (Makro-Provenienz: Quelle: FRED DGS10 (https://api.stlouisfed.org/fred/series/observations); "
        "Abruf: 2026-11-03T04:30:00+00:00; Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: 1 Börsentag) | "
        "Live-Wert: 4.8940% (Quelle: Yahoo Finance (^TNX); Abruf: 2026-11-03T04:35:08+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst."
    )
    report = report.replace("Dollar/Liquidität/Volatilität:", conflict + "\nDollar/Liquidität/Volatilität:")
    errors = validate_report(report)
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in error for error in errors), errors


def test_m07_parenthesized_us10y_provenance_is_recognized_without_duplicate_fields():
    macro = (
        "US 10Y Treasury: 4.0000 | Datenstand=2026-11-02 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | DELAY_DAYS=1 | UNIT=%"
    )
    report = (
        "Zinsen/Renditen: US 10Y Treasury bei 4.00% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)."
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {}
    )
    # The parenthesized local annotation is completed from the matching DGS10
    # observation; fields must not be duplicated at the end of the row.
    assert result.count("Quelle:") == 1, result
    assert "Quelle: FRED DGS10" in result
    assert "Abrufzeit: 2026-11-03T07:00:00+00:00" in result
    assert "Datenstichtag: 2026-11-02" in result
    assert "Einheit: %" in result
    assert "Verzögerung: 1 Kalendertage" in result
    assert result.count("Abrufzeit:") == 1, result
    assert result.count("Datenstichtag:") == 1, result
    assert result.count("Einheit:") == 1, result
    assert result.count("Verzögerung:") == 1, result
    findings = validate_report(result)
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in findings), findings

def test_m07_row_end_provenance_is_migrated_into_us10y_local_annotation():
    macro = (
        "US 10Y Treasury: 4.0000 | STATUS=REAL | SOURCE=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | "
        "DELAY_DAYS=1 | UNIT=% | Datenstand=2026-11-02"
    )
    report = (
        "Zinsen/Renditen: US 10Y Treasury bei 4.00% "
        "| Quelle: FRED DGS10 | Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage."
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert "4.00% (Quelle: FRED DGS10;" in result, result
    assert result.count("Quelle:") == 1, result
    assert result.count("Abrufzeit:") == 1, result
    assert result.count("Datenstichtag:") == 1, result
    assert result.count("Einheit:") == 1, result
    assert result.count("Verzögerung:") == 1, result
    assert "| Quelle:" not in result, result
    assert result.endswith(")."), result
    assert production._trading_radar_treasury_provenienz_ergänzen(result, macro, {}) == result
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance
    assert _treasury_observation_has_complete_provenance(result), result


def test_m07_local_placeholders_are_filled_from_row_end_without_duplicate_provenance():
    macro = (
        "US 10Y Treasury: 4.0000 | STATUS=REAL | SOURCE=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | "
        "DELAY_DAYS=1 | UNIT=% | Datenstand=2026-11-02"
    )
    report = (
        "Zinsen/Renditen: US 10Y Treasury bei 4.00% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR). "
        "| Quelle: FRED DGS10 | Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage."
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert result.count("Quelle:") == 1, result
    assert result.count("Abrufzeit:") == 1, result
    assert result.count("Datenstichtag:") == 1, result
    assert result.count("Einheit:") == 1, result
    assert result.count("Verzögerung:") == 1, result
    assert "Quelle: FRED DGS10" in result, result
    assert "NICHT VERFÜGBAR" not in result, result
    assert "| Quelle:" not in result, result
    assert result.endswith(")."), result
    assert production._trading_radar_treasury_provenienz_ergänzen(result, macro, {}) == result
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance
    assert _treasury_observation_has_complete_provenance(result), result

def test_m07_unrelated_parenthetical_note_is_preserved_when_provenance_is_inserted():
    report = "Zinsen/Renditen: US 10Y Treasury bei 4.00% (vorläufiger Hinweis)."
    result = production._trading_radar_treasury_provenienz_ergänzen(report, "", {})
    assert "(Quelle: NICHT VERFÜGBAR;" in result, result
    assert "(vorläufiger Hinweis)" in result, result
    assert result.count("vorläufiger Hinweis") == 1, result



def _assert_parentheses_are_stack_balanced(value):
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            assert depth >= 0, f"Klammer-Unterlauf an Position {index}: {value!r}"
    assert depth == 0, f"Nicht geschlossene Klammer(n): {value!r}"


def test_m07_adversarial_foreign_nested_provenance_is_never_borrowed_or_removed():
    macro = (
        "US 10Y Treasury: 4.0000 | STATUS=REAL | SOURCE=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | DELAY_DAYS=1 | UNIT=% | "
        "Datenstand=2026-11-02"
    )
    foreign_note = (
        "(anderer Indikator (Quelle: Yahoo Finance ^TNX; "
        "Abrufzeit: 2026-11-03T07:01:00+00:00; Datenstichtag: 2026-11-02; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs))"
    )
    report = (
        "Zinsen/Renditen: US 10Y Treasury bei 4.00% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR) " + foreign_note + "."
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    # The foreign note is a terminal attribution boundary: matching macro
    # provenance must not be borrowed across it.
    assert "Quelle: FRED DGS10" not in result, result
    assert "Quelle: Yahoo Finance ^TNX" not in result.split("US 10Y Treasury bei 4.00%", 1)[1].split(foreign_note, 1)[0], result
    assert foreign_note in result, result
    assert result.count("anderer Indikator") == 1, result
    assert result.count("Quelle:") == 2, result
    _assert_parentheses_are_stack_balanced(result)


def test_m07_only_top_level_row_end_fields_are_migrated_and_removed():
    macro = (
        "US 10Y Treasury: 4.0000 | STATUS=REAL | SOURCE=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | DELAY_DAYS=1 | UNIT=% | "
        "Datenstand=2026-11-02"
    )
    foreign_note = (
        "(anderer Indikator (Quelle: Yahoo Finance ^TNX; "
        "Abrufzeit: 2026-11-03T07:01:00+00:00; Datenstichtag: 2026-11-02; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs))"
    )
    report = (
        "Zinsen/Renditen: US 10Y Treasury bei 4.00% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR) " + foreign_note +
        " | Quelle: FRED DGS10 | Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage."
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert foreign_note in result, result
    # Top-level row-end fields after a foreign note remain verbatim but are
    # not migrated into the local US10Y annotation.
    assert "Quelle: FRED DGS10" in result, result
    assert "Quelle: Yahoo Finance ^TNX" in result, result
    assert result.count("Quelle: FRED DGS10") == 1, result
    assert "| Quelle: FRED DGS10" in result, result
    _assert_parentheses_are_stack_balanced(result)


def test_m07_provenance_migration_preserves_unrelated_parenthetical_text_byte_for_byte():
    macro = (
        "US 10Y Treasury: 4.0000 | STATUS=REAL | SOURCE=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | DELAY_DAYS=1 | UNIT=% | "
        "Datenstand=2026-11-02"
    )
    unrelated = "(Kommentar: vorläufig; andere Quelle (nicht US10Y: Yahoo Finance ^TNX))"
    report = "Zinsen/Renditen: US 10Y Treasury bei 4.00% " + unrelated + "."
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert unrelated in result, result
    assert result.index(unrelated) > result.index("US 10Y Treasury bei 4.00%"), result
    assert result.count("Kommentar: vorläufig") == 1, result
    _assert_parentheses_are_stack_balanced(result)


def test_m07_local_annotation_preserves_unknown_nested_text_verbatim():
    macro = (
        "US 10Y Treasury: 4.0000 | STATUS=REAL | SOURCE=FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-03T07:00:00+00:00 | DELAY_DAYS=1 | UNIT=% | "
        "Datenstand=2026-11-02"
    )
    unknown = "FREITEXT-UNBEKANNT (verschachtelte Notiz; Yahoo Finance ^TNX)"
    report = (
        "US 10Y Treasury bei 4.00% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR; " + unknown + ")"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert unknown in result, result
    assert result.count(unknown) == 1, result
    assert "Quelle: FRED DGS10" in result, result


def test_m07_conflicting_duplicate_local_sources_are_not_silently_reduced():
    report = (
        "US 10Y Treasury bei 4.00% "
        "(Quelle: FRED DGS10; Quelle: Yahoo Finance ^TNX; "
        "Abrufzeit: 2026-11-03T07:00:00+00:00; Datenstichtag: 2026-11-02; "
        "Einheit: %; Verzögerung: 1 Kalendertage)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, "", {})
    assert "MEHRFACH WIDERSPRÜCHLICHE ANGABEN" in result, result
    assert "Quelle: FRED DGS10" not in result, result


def test_m07_unbalanced_local_annotation_is_not_rewritten_or_used_as_provenance():
    malformed = (
        "US 10Y Treasury bei 4.00% "
        "(Quelle: FRED DGS10; Abrufzeit: 2026-11-03T07:00:00+00:00; "
        "Datenstichtag: 2026-11-02; Einheit: %; Verzögerung: 1 Kalendertage "
        "(fremde Klammer)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(malformed, "", {})
    assert "Quelle: FRED DGS10" in result, result
    assert "(fremde Klammer)" in result, result



def test_m07_foreign_top_level_provenance_is_not_borrowed_or_deleted():
    foreign = (
        "US 10Y Treasury bei 4.00% | anderer Indikator: 10Y TIPS "
        "| Quelle: Yahoo Finance ^TNX "
        "| Abrufzeit: 2026-11-03T07:00:00+00:00 "
        "| Datenstichtag: 2026-11-02 | Einheit: % | Verzögerung: 1 Kalendertage"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(foreign, "", {})
    assert "Quelle: NICHT VERFÜGBAR" in result, result
    assert "| anderer Indikator: 10Y TIPS" in result, result
    assert "| Quelle: Yahoo Finance ^TNX" in result, result
    assert "| Abrufzeit: 2026-11-03T07:00:00+00:00" in result, result



def test_m05_deterministic_register_changes_override_model_no_change_claim():
    evidence = [
        "THESENREGISTER-VERGLEICH: PASS",
        "VORHER-SHA256: " + "a" * 64,
        "NACHHER-SHA256: " + "b" * 64,
        "THESEN_NEU: TH-NEW",
        "THESEN_AKTUALISIERT: TH-OLD",
        "THESEN_UNVERAENDERT: TH-STABLE",
        "THESEN_ARCHIVIERT: KEINE",
        "THESIS_ID: TH-NEW",
        "REGISTER-VERSION: 1",
    ]
    report = (
        "2. Thesen-Update\n"
        "Keine neue oder aktualisierte These.\n\n"
        "3. Ereignisradar\n"
        "Ereignisse werden separat bewertet.\n"
    )
    result = production._trading_radar_thesenregister_nachweis_text(report, evidence)
    assert "Keine neue oder aktualisierte These" not in result, result
    assert "Registeränderungen laut Vergleich: Neue: TH-NEW; Aktualisiert: TH-OLD." in result, result
    assert "THESEN_NEU: TH-NEW" in result
    assert "THESEN_AKTUALISIERT: TH-OLD" in result


def test_m05_no_change_claim_remains_when_persisted_register_has_no_changes():
    evidence = [
        "THESENREGISTER-VERGLEICH: PASS",
        "VORHER-SHA256: " + "a" * 64,
        "NACHHER-SHA256: " + "a" * 64,
        "THESEN_NEU: KEINE",
        "THESEN_AKTUALISIERT: KEINE",
        "THESEN_UNVERAENDERT: TH-STABLE",
        "THESEN_ARCHIVIERT: KEINE",
    ]
    report = (
        "2. Thesen-Update\n"
        "Keine neue oder aktualisierte These.\n\n"
        "3. Ereignisradar\n"
        "Ereignisse werden separat bewertet.\n"
    )
    result = production._trading_radar_thesenregister_nachweis_text(report, evidence)
    assert "Keine neue oder aktualisierte These" in result, result
    assert "Registeränderungen laut Vergleich:" not in result, result



def test_m05_report_gate_accepts_deterministic_change_summary_instead_of_no_change_claim():
    import hashlib
    import json
    from trading_radar_report_gate import _validate_m05_m10

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
    evidence = [
        "THESENREGISTER-VERGLEICH: PASS",
        f"VORHER-SHA256: {previous_hash}",
        f"NACHHER-SHA256: {current_hash}",
        f"THESENREGISTER-VERGLEICH-ID: {comparison_id}",
        "VORHER_THESEN_COUNT: 2",
        "VORHER_AKTIVE_THESEN: 2",
        "NACHHER_THESEN_COUNT: 3",
        "NACHHER_AKTIVE_THESEN: 3",
        "THESEN_NEU: TH-NEW",
        "THESEN_AKTUALISIERT: TH-OLD",
        "THESEN_UNVERAENDERT: TH-STABLE",
        "THESEN_ARCHIVIERT: KEINE",
        "VORHER-THESIS-ID: TH-OLD",
        "VORHER-REGISTER-VERSION: 1",
        "VORHER-THESIS-ACTIVE: YES",
        "VORHER-THESIS-ID: TH-STABLE",
        "VORHER-REGISTER-VERSION: 1",
        "VORHER-THESIS-ACTIVE: YES",
        "THESIS_ID: TH-OLD",
        "REGISTER-VERSION: 2",
        "THESIS_ACTIVE: YES",
        "THESIS_ID: TH-STABLE",
        "REGISTER-VERSION: 1",
        "THESIS_ACTIVE: YES",
        "THESIS_ID: TH-NEW",
        "REGISTER-VERSION: 1",
        "THESIS_ACTIVE: YES",
    ]
    report = (
        "2. Thesen-Update\n"
        "Status: BESTÄTIGT\n"
        "Keine neue oder aktualisierte These.\n\n"
        "3. Ereignisradar\n"
        "Ereignisse werden separat bewertet.\n"
    )
    repaired = production._trading_radar_thesenregister_nachweis_text(report, evidence)
    assert "Keine neue oder aktualisierte These" not in repaired
    assert "Registeränderungen laut Vergleich: Neue: TH-NEW; Aktualisiert: TH-OLD." in repaired
    errors = []
    _validate_m05_m10(repaired, errors)
    assert not any("Bericht behauptet 'keine Änderung'" in error for error in errors), errors
    assert not any("Semantischer Thesenregistervergleich widersprüchlich" in error for error in errors), errors
