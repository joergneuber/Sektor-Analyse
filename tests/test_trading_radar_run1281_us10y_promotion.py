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
