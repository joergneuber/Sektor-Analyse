"""Run-1281 regression tests for executive promotion of source-stamped US-10Y evidence."""
from tests.test_m07_production_parser_boundary import production
from tests.test_trading_radar_report_gate import report_fixture
from trading_radar_report_gate import validate_report


LIVE_TNX = (
    "- US-10J-Rendite (^TNX): 4.89% "
    "(Quelle: Yahoo Finance (^TNX); "
    "Abrufzeit: 2026-11-05T10:45:00+00:00; "
    "Datenstichtag: 2026-11-04T00:00:00+00:00; "
    "Einheit: %; Verzögerung: Tages-Schlusskurs) "
    "| Quelldatei: Makro_Briefing(2026-11-05).txt"
)


def _run1281_report():
    report = report_fixture(ohlcv_warning=False)
    report = report.replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: Fed Funds Effective Rate 3,88%, 10Y Treasury 4,98%, "
        "10Y TIPS 2,63% (Realzins).",
    )
    marker = "\n\n2. Thesen-Update"
    live_block = (
        "\n\n**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**\n"
        "- Datenstand UTC: 2026-11-05T10:45:00+00:00\n"
        + LIVE_TNX
        + "\n"
    )
    assert marker in report
    return report.replace(marker, live_block + marker, 1)


def _macro_line(*, value="4.9800", observed="2026-10-29", retrieved="2026-10-30T14:30:00+00:00"):
    return (
        f"US 10Y Treasury: {value} | Datenstand={observed} | "
        "STATUS=REAL_CACHED | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations?series_id=DGS10) | "
        f"RETRIEVED_AT={retrieved} | DELAY_DAYS=7 | UNIT=%"
    )


def _normalize(report, macro_text):
    return production._trading_radar_treasury_provenienz_ergänzen(
        report,
        macro_text,
        {"Makro": "Makro_Briefing(2026-11-05).txt"},
    )


def test_run1281_live_tnx_only_in_appendix_is_promoted_and_fred_is_separate():
    normalized = _normalize(_run1281_report(), _macro_line())
    executive = normalized.split("2. Thesen-Update", 1)[0]
    rates_line = next(line for line in executive.splitlines() if line.startswith("Zinsen/Renditen:"))
    fred_line = next(line for line in executive.splitlines() if "US 10Y Treasury (FRED DGS10" in line)

    assert "US-10J-Rendite (^TNX): 4.89%" in rates_line
    assert "Yahoo Finance (^TNX)" in rates_line
    assert "Datenstichtag: 2026-11-04" in rates_line
    assert "10Y Treasury 4,98%" not in rates_line
    assert "4.9800%" in fred_line and "Datenstichtag: 2026-10-29" in fred_line
    assert "Quelle: FRED DGS10" in fred_line
    assert "Abrufzeit: 2026-10-30T14:30:00+00:00" in fred_line
    assert rates_line != fred_line

    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-02:")], errors
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_run1281_missing_fred_retrieval_time_is_disclosed_not_fabricated():
    normalized = _normalize(
        _run1281_report(),
        _macro_line(retrieved="NICHT VERFUEGBAR"),
    )
    executive = normalized.split("2. Thesen-Update", 1)[0]
    rates_line = next(line for line in executive.splitlines() if line.startswith("Zinsen/Renditen:"))

    assert "US-10J-Rendite (^TNX): 4.89%" in rates_line
    assert "Makro-Cache DGS10 (FRED-Serie): 4.9800%" in executive
    partial_fred_line = next(
        line for line in executive.splitlines() if "Makro-Cache DGS10" in line
    )
    assert "Abrufzeit: NICHT VERFÜGBAR" not in partial_fred_line
    assert "fehlende Provenienzfelder: Abrufzeit" in partial_fred_line
    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-02:")], errors
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_fred_value_mismatch_is_not_used_as_provenance_for_generic_treasury_text():
    normalized = _normalize(
        _run1281_report(),
        _macro_line(value="4.9700"),
    )
    executive = normalized.split("2. Thesen-Update", 1)[0]
    rates_line = next(line for line in executive.splitlines() if line.startswith("Zinsen/Renditen:"))
    assert "US-10J-Rendite (^TNX): 4.89%" in rates_line
    assert "US 10Y Treasury (FRED DGS10" not in executive
    assert "Nicht zugeordneter Makro-Textwert: 10Y Treasury 4.98%" in executive



def test_newer_complete_fred_observation_becomes_primary_executive_value():
    normalized = _normalize(
        _run1281_report(),
        _macro_line(observed="2026-11-05", retrieved="2026-11-05T10:41:13+00:00"),
    )
    executive = normalized.split("2. Thesen-Update", 1)[0]
    rates_line = next(line for line in executive.splitlines() if line.startswith("Zinsen/Renditen:"))
    yahoo_line = next(line for line in executive.splitlines() if line.startswith("- US-10J-Rendite (^TNX)"))

    assert "US 10Y Treasury (FRED DGS10" in rates_line
    assert "Datenstichtag: 2026-11-05" in rates_line
    assert "US-10J-Rendite (^TNX)" not in rates_line
    assert "4.89%" in yahoo_line and "Datenstichtag: 2026-11-04" in yahoo_line
    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-02:")], errors
    assert not [item for item in errors if item.startswith("M-07:")], errors

def test_foreign_ten_year_yield_does_not_satisfy_or_get_promoted_as_us10y():
    report = report_fixture(ohlcv_warning=False).replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: Fed Funds Effective Rate 3,88%; 10Y TIPS 2,63%.",
    )
    report = report.replace(
        "\n\n2. Thesen-Update",
        "\n\n**Live-Marktbelege:**\n"
        "- German 10-year Bund yield: 2.45% (Quelle: Deutsche Bundesbank; "
        "Abrufzeit: 2026-11-05T10:45:00+00:00; Datenstichtag: 2026-11-04; "
        "Einheit: %; Verzögerung: Tages-Schlusskurs)\n\n2. Thesen-Update",
        1,
    )
    normalized = _normalize(report, _macro_line())
    executive = normalized.split("2. Thesen-Update", 1)[0]
    rates_line = next(line for line in executive.splitlines() if line.startswith("Zinsen/Renditen:"))
    assert "US-10J-Rendite" not in rates_line
    assert "German 10-year Bund yield" not in rates_line
    errors = validate_report(normalized)
    assert any("M-02: US-10Y-Rendite fehlt" in item for item in errors), errors


def test_treasury_promotion_is_idempotent():
    once = _normalize(_run1281_report(), _macro_line())
    twice = _normalize(once, _macro_line())
    executive = twice.split("2. Thesen-Update", 1)[0]
    assert executive.count("US-10J-Rendite (^TNX): 4.89%") == 2  # executive + original live evidence
    assert executive.count("US 10Y Treasury (FRED DGS10") == 1
    errors = validate_report(twice)
    assert not [item for item in errors if item.startswith("M-02:")], errors
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_run1343_dgs10_identity_markers_do_not_break_following_provenance_fields():
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-29 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations?series_id=DGS10) | "
        "SERIES_ID=DGS10 | INSTRUMENT=US10Y_NOMINAL | "
        "RETRIEVED_AT=2026-10-30T14:30:00+00:00 | DELAY_DAYS=1 | UNIT=%"
    )
    normalized = _normalize(_run1281_report(), macro)
    executive = normalized.split("2. Thesen-Update", 1)[0]
    fred_line = next(
        line for line in executive.splitlines()
        if "US 10Y Treasury (FRED DGS10" in line
    )

    assert "Abrufzeit: 2026-10-30T14:30:00+00:00" in fred_line
    assert "Datenstichtag: 2026-10-29" in fred_line
    assert "Einheit: %" in fred_line
    assert "Verzögerung: 1 Kalendertage" in fred_line
    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-02:")], errors
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_run1343_rejects_wrong_or_partial_dgs10_identity_markers():
    base = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-29 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations?series_id=DGS10) | "
        "SERIES_ID={series_id} | INSTRUMENT={instrument} | "
        "RETRIEVED_AT=2026-10-30T14:30:00+00:00 | DELAY_DAYS=1 | UNIT=%"
    )
    for series_id, instrument in (
        ("DFII10", "US10Y_NOMINAL"),
        ("DGS10", "US10Y_REAL"),
        ("DGS10", ""),
    ):
        macro = base.format(series_id=series_id, instrument=instrument)
        normalized = _normalize(_run1281_report(), macro)
        executive = normalized.split("2. Thesen-Update", 1)[0]
        assert "US 10Y Treasury (FRED DGS10" not in executive
        assert "Nicht zugeordneter Makro-Textwert: 10Y Treasury 4.98%" in executive


def test_run1343_live_and_fred_values_are_enriched_independently_before_multi_value_gate():
    report = report_fixture(ohlcv_warning=False)
    original_rates = (
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | "
        "Einheit: % | Verzögerung: 1 Börsentag."
    )
    mixed_rates = (
        "Zinsen/Renditen: US-10J-Rendite (^TNX): 4.85% "
        "(Quelle: Yahoo Finance (^TNX); Abrufzeit: 2026-11-26T05:30:44+00:00; "
        "Datenstichtag: 2026-11-25; Einheit: %; Verzögerung: Tages-Schlusskurs); "
        "US 10Y Treasury 4,91 % (Quelle: FRED DGS10 "
        "(https://api.stlouisfed.org/fred/series/observations); "
        "Abrufzeit: NICHT VERFÜGBAR; Datenstichtag: 2026-11-24; "
        "Einheit: NICHT VERFÜGBAR; Verzögerung: NICHT VERFÜGBAR); "
        "Realzins 10Y TIPS 2,88 %."
    )
    assert original_rates in report
    report = report.replace(original_rates, mixed_rates, 1)
    macro = (
        "US 10Y Treasury: 4.9100 | Datenstand=2026-11-24 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "SERIES_ID=DGS10 | INSTRUMENT=US10Y_NOMINAL | "
        "RETRIEVED_AT=2026-11-26T05:03:40+00:00 | DELAY_DAYS=2 | UNIT=%"
    )

    normalized = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {"Makro": "Makro_Briefing(2026-11-26).txt"}
    )
    rates_line = next(
        line for line in normalized.splitlines()
        if line.startswith("Zinsen/Renditen:")
    )

    assert "US-10J-Rendite (^TNX): 4.85%" in rates_line
    assert "US 10Y Treasury 4,91 %" in rates_line
    assert "Abrufzeit: 2026-11-26T05:03:40+00:00" in rates_line
    assert "Datenstichtag: 2026-11-24" in rates_line
    assert "Einheit: %" in rates_line
    assert "Verzögerung: 2 Kalendertage" in rates_line
    assert "MEHRERE_WERTE_NICHT_EINDEUTIG_ZUORDENBAR" not in rates_line
    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-02:")], errors
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_run1343_multi_value_row_stays_blocked_when_dgs10_identity_is_invalid():
    report = report_fixture(ohlcv_warning=False)
    original_rates = (
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | "
        "Einheit: % | Verzögerung: 1 Börsentag."
    )
    mixed_rates = (
        "Zinsen/Renditen: US-10J-Rendite (^TNX): 4.85% "
        "(Quelle: Yahoo Finance (^TNX); Abrufzeit: 2026-11-26T05:30:44+00:00; "
        "Datenstichtag: 2026-11-25; Einheit: %; Verzögerung: Tages-Schlusskurs); "
        "US 10Y Treasury 4,91 % (Quelle: FRED DGS10; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: 2026-11-24; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR); Realzins 10Y TIPS 2,88 %."
    )
    assert original_rates in report
    report = report.replace(original_rates, mixed_rates, 1)
    macro = (
        "US 10Y Treasury: 4.9100 | Datenstand=2026-11-24 | STATUS=REAL | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "SERIES_ID=DFII10 | INSTRUMENT=US10Y_NOMINAL | "
        "RETRIEVED_AT=2026-11-26T05:03:40+00:00 | DELAY_DAYS=2 | UNIT=%"
    )

    normalized = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {"Makro": "Makro_Briefing(2026-11-26).txt"}
    )
    rates_line = next(
        line for line in normalized.splitlines()
        if line.startswith("Zinsen/Renditen:")
    )
    assert "Abrufzeit: NICHT VERFÜGBAR" in rates_line
    assert "MEHRERE_WERTE_NICHT_EINDEUTIG_ZUORDENBAR" in rates_line
    errors = validate_report(normalized)
    assert any(item.startswith("M-07:") for item in errors), errors


def test_run1349_final_post_compression_pass_repairs_late_thesis_json_us10y_evidence():
    """The thesis JSON compressor runs after the first provenance pass.

    Its compact prose can introduce a fresh US-10Y mention, so the final pass
    must run after compression and before the persisted report gate.
    """
    import json

    report = _run1281_report()
    # Simulate the first, pre-compression enrichment pass.
    report = _normalize(report, _macro_line())

    thesis_payload = {
        "theses": [{
            "thesis_id": "macro-us10y-01",
            "version": 1,
            "status": "BESTÄTIGT",
            "confidence": "mittel",
            "thesis": "Das nominale US-Zinsniveau bleibt erhöht.",
            "horizon": "2-4 Wochen",
            "alternative": "Seitwärtsbewegung",
            "evidence": ["US 10Y Treasury 4.98%"],
            "counter_evidence": [],
            "confirm_triggers": [],
            "invalidate_triggers": [],
            "event_dependencies": [],
            "asset_classes": ["Anleihen"],
            "candidates": [],
        }],
    }
    marker = "3. Ereignisradar"
    assert marker in report
    report = report.replace(
        marker,
        "```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(thesis_payload, ensure_ascii=False)
        + "\n```\n"
        + marker,
        1,
    )

    compressed = production._trading_radar_lesefassung_komprimieren(report)
    generated_evidence = next(
        line for line in compressed.splitlines()
        if "Belege: US 10Y Treasury 4.98%" in line
    )
    assert "Abrufzeit:" not in generated_evidence

    final_text = _normalize(compressed, _macro_line())
    repaired_evidence = next(
        line for line in final_text.splitlines()
        if "Belege: US 10Y Treasury 4.98%" in line
    )
    assert "Quelle: FRED DGS10" in repaired_evidence
    assert "Abrufzeit: 2026-10-30T14:30:00+00:00" in repaired_evidence
    assert "Datenstichtag: 2026-10-29" in repaired_evidence
    assert "Einheit: %" in repaired_evidence
    assert "Verzögerung: 7 Kalendertage" in repaired_evidence
    errors = validate_report(final_text)
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_run1349_production_and_gate_recognize_treasury_rendite_and_zins_descriptors():
    report = report_fixture(ohlcv_warning=False)
    original_rates = (
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | "
        "Einheit: % | Verzögerung: 1 Börsentag."
    )
    replacement = (
        "Zinsen/Renditen: US 10Y Treasury Rendite bei 4.91% "
        "(Datenstichtag: 2026-11-24; Quelle: FRED DGS10).\n"
        "US 10Y Treasury-Zins stabilisiert sich bei 4.91%."
    )
    assert original_rates in report
    report = report.replace(original_rates, replacement, 1)
    macro = _macro_line(
        value="4.9100",
        observed="2026-11-24",
        retrieved="2026-11-27T11:14:59+00:00",
    )

    normalized = _normalize(report, macro)
    matching_lines = [
        line for line in normalized.splitlines()
        if "US 10Y Treasury Rendite bei 4.91%" in line
        or "US 10Y Treasury-Zins stabilisiert sich bei 4.91%" in line
    ]
    assert len(matching_lines) == 2
    for line in matching_lines:
        assert "Quelle: FRED DGS10" in line
        assert "Abrufzeit: 2026-11-27T11:14:59+00:00" in line
        assert "Datenstichtag: 2026-11-24" in line
        assert "Einheit: %" in line
        assert "Verzögerung: 7 Kalendertage" in line
    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-07:")], errors


def test_run1349_production_validates_the_compressed_report_after_final_treasury_recheck():
    from pathlib import Path

    source = Path(production.__file__).read_text(encoding="utf-8")
    start = source.index("def speichere_ergebnis(")
    end = source.index("\ndef _gemini_report_metall_status", start)
    body = source[start:end]
    compression = body.index("text = _trading_radar_lesefassung_komprimieren(text)")
    final_recheck = body.index("TRADING_RADAR_US10Y_FINAL_RECHECK=")
    final_gate = body.index("report_gate_input = text.rstrip()")
    assert compression < final_recheck < final_gate


def test_run1349_production_recognizes_plural_treasury_renditen_wording():
    """Plural Renditen + 'von' is a numeric US-10Y observation too."""
    report = report_fixture(ohlcv_warning=False)
    report = report.replace(
        "3. Ereignisradar",
        "stabile US 10Y Treasury Renditen von 4.91%.\n3. Ereignisradar",
        1,
    )
    macro = _macro_line(
        value="4.9100",
        observed="2026-11-24",
        retrieved="2026-11-27T11:14:59+00:00",
    )
    normalized = _normalize(report, macro)
    matching = [
        line for line in normalized.splitlines()
        if "stabile US 10Y Treasury Renditen von 4.91%" in line
    ]
    assert len(matching) == 1
    for field in (
        "Quelle: FRED DGS10",
        "Abrufzeit: 2026-11-27T11:14:59+00:00",
        "Datenstichtag: 2026-11-24",
        "Einheit: %",
        "Verzögerung: 7 Kalendertage",
    ):
        assert field in matching[0]
    errors = validate_report(normalized)
    assert not [item for item in errors if item.startswith("M-07:")], errors
