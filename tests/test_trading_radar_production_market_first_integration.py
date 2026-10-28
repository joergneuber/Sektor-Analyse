from pathlib import Path

from trading_radar_market_first import normalize_event_section, validate_ohlcv_audit

ROOT = Path(__file__).resolve().parents[1]


def test_production_entrypoint_runs_market_first_audit_on_saved_report():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert "audit_market_first_content(report_text)" in source
    assert 'fachlich_final = "NICHT_FINAL"' in source
    assert "TRADING_RADAR_MARKET_FIRST_GATE=FAIL" in source
    assert "TRADING_RADAR_MARKET_FIRST_GATE=PASS" in source


def test_production_path_normalizes_events_and_validates_real_ohlcv_snapshot():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert "normalize_event_section(text)" in source
    assert "historical_ohlcv.audit_snapshot(candidate_tickers=candidate_tickers)" in source
    assert "validate_ohlcv_audit(" in source
    assert "explicit_no_candidates=explicit_no_candidates" in source
    assert "validation_errors.extend(\"OHLCV: \" + item for item in ohlcv_findings)" in source


def test_production_prompt_sets_market_first_order_and_portfolio_is_last():
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    market = source.index("1) GESAMTMARKT/MARKTREGIME")
    rotation = source.index("2) RELATIVE STÄRKE/KAPITALROTATION")
    theses = source.index("3) THESEN/VORSCHAU")
    candidates = source.index("4) TECHNISCHE KANDIDATEN/TRADING")
    portfolio = source.index("5) PORTFOLIO-AUSWIRKUNG")
    assert market < rotation < theses < candidates < portfolio


def test_event_section_deduplicates_identical_records_and_preserves_unknowns_once():
    report = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  Datum: 2026-10-28
  Quelle: Federal Reserve
  Uhrzeit: NICHT VERFÜGBAR
  Zeitzone: NICHT VERFÜGBAR
- Ereignis: FOMC-Zinsentscheid
  Datum: 2026-10-28
  Quelle: Federal Reserve
  Uhrzeit: NICHT VERFÜGBAR
  Zeitzone: NICHT VERFÜGBAR

4. Assetklassenvergleich
Marktbild.
"""
    result, findings = normalize_event_section(report)
    assert result.count("Ereignis: FOMC-Zinsentscheid") == 1
    assert result.count("Uhrzeit:") == 1
    assert result.count("Zeitzone:") == 1
    assert "4. Assetklassenvergleich" in result
    # FOMC ohne LIVE_VERIFIZIERT muss trotz erfolgreicher Deduplizierung
    # als Freigabebefund bestehen bleiben; unbekannte Uhrzeiten werden nicht erfunden.
    assert any("nicht live" in item.lower() for item in findings)


def test_event_section_missing_source_or_date_returns_findings():
    report = """3. Ereignisradar
- Ereignis: Inflationsbericht
  Uhrzeit: 08:30

4. Assetklassenvergleich
"""
    _, findings = normalize_event_section(report)
    assert any("Quelle" in item or "Quelle" in item.lower() for item in findings)
    assert any("Datum" in item for item in findings)


def test_ohlcv_status_inconsistency_blocks_final():
    audit = {
        "status": "complete", "universe_count": 10, "covered_count": 9,
        "missing_tickers": ["ABC"], "stale_tickers": [], "invalid_ohlcv_tickers": [],
    }
    findings = validate_ohlcv_audit(audit)
    assert any("widerspricht" in item.lower() for item in findings)
    assert any("nicht vollständig" in item.lower() for item in findings)


def test_ohlcv_missing_or_invalid_counts_fail_closed():
    assert validate_ohlcv_audit({"status": "unavailable", "universe_count": None, "covered_count": None})
    assert validate_ohlcv_audit({"status": "gaps", "universe_count": 4, "covered_count": 3, "missing_tickers": ["ABC"]})



def test_ohlcv_empty_affected_candidates_fails_closed():
    audit = {
        "status": "gaps",
        "universe_count": 4,
        "covered_count": 3,
        "affected_candidates": [],
    }
    findings = validate_ohlcv_audit(audit)
    assert any("nicht vollständig" in item.lower() for item in findings)
    assert any("kandidatenfolgen fehlen" in item.lower() for item in findings)


def test_ohlcv_null_or_wrong_type_affected_candidates_fails_closed():
    for value in (None, "", {}, 0):
        findings = validate_ohlcv_audit({
            "status": "gaps",
            "universe_count": 4,
            "covered_count": 3,
            "affected_candidates": value,
        })
        assert any("kandidatenfolgen fehlen" in item.lower() for item in findings)


def test_ohlcv_nonempty_candidate_assessment_allows_documented_universe_gap():
    findings = validate_ohlcv_audit({
        "status": "gaps",
        "universe_count": 4,
        "covered_count": 3,
        "affected_candidates": [{"ticker": "ABC", "tradeability": "NICHT HANDELBAR"}],
    })
    assert not any("kandidatenfolgen fehlen" in item.lower() for item in findings)


def test_explicit_zero_candidate_set_allows_documented_universe_ohlcv_gaps():
    from trading_radar_market_first import validate_ohlcv_audit

    audit = {
        "status": "gaps", "universe_count": 862, "covered_count": 862,
        "missing_tickers": [], "stale_tickers": ["ABC"],
        "recent_invalid_ohlcv_tickers": ["XYZ"], "affected_candidates": [],
        "candidate_tickers_checked": [], "candidate_mapping_complete": False,
        "candidate_impact": "candidate_mapping_incomplete",
    }
    # This exception is only valid when the report separately declares all four
    # candidate groups explicitly empty; an empty list alone must still fail closed.
    assert validate_ohlcv_audit(audit)
    assert validate_ohlcv_audit(audit, explicit_no_candidates=True) == []
