from datetime import datetime, timezone, timedelta
import pytest
from trading_radar_market_first import normalize_event, deduplicate_events, classify_ohlcv_row, summarize_ohlcv_status


def test_event_missing_time_timezone_marked_once_and_not_verified():
    event = normalize_event({"name": "FOMC", "date": "2026-10-28", "source": "Fed",
                             "time": "NICHT VERFÜGBAR", "timezone": "NICHT VERFÜGBAR"})
    assert event["time"] is None
    assert event["timezone"] is None
    assert event["time_verified"] is False
    assert event["status"] == "READY"
    assert event["missing_required"] == []


def test_event_duplicates_are_collapsed():
    rows = [{"name": "CPI", "date": "2026-11-12", "source": "BLS"},
            {"name": "CPI", "date": "2026-11-12", "source": "BLS"}]
    assert len(deduplicate_events(rows)) == 1


def test_event_missing_required_fields_are_explicit():
    row = normalize_event({"name": "CPI"})
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["date", "source"]


def test_ohlcv_missing_stale_invalid_and_valid_are_separate():
    cutoff = datetime(2026, 10, 27, tzinfo=timezone.utc)
    base = {"open": 10, "high": 12, "low": 9, "close": 11, "volume": 100,
            "timestamp": "2026-10-26T00:00:00+00:00"}
    assert classify_ohlcv_row({**base, "close": None}, cutoff=cutoff) == "MISSING"
    assert classify_ohlcv_row({**base, "timestamp": "2026-09-01T00:00:00+00:00"}, cutoff=cutoff) == "STALE"
    assert classify_ohlcv_row({**base, "high": 8}, cutoff=cutoff) == "INVALID"
    assert classify_ohlcv_row(base, cutoff=cutoff) == "VALID"


def test_ohlcv_summary_counts_only_known_states():
    assert summarize_ohlcv_status(["VALID", "MISSING", "STALE", "INVALID"]) == {
        "INVALID": 1, "MISSING": 1, "STALE": 1, "VALID": 1, "TOTAL": 4}
    with pytest.raises(ValueError):
        summarize_ohlcv_status(["UNKNOWN"])



def test_fomc_event_is_enriched_from_project_calendar_and_official_source(tmp_path):
    from trading_radar_market_first import _enrich_fomc_event
    import json
    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28", "2026-12-09"]}), encoding="utf-8")
    report = "Datenstand: 2026-10-28T04:29:51+00:00"
    body = "- **Ereignis 1:**\n- **Kennzahl:** FOMC Zinsentscheid\n- **Quelle:** Federal Reserve / fomc_termine.json\nUhrzeit: NICHT VERFÜGBAR"
    class FakeResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = "<html><body><table><tr><td>October 27-28, 2026</td><td>FOMC meeting</td><td>2:00 p.m. Press Conference 2:30 p.m.</td></tr></table></body></html>"
    result = _enrich_fomc_event(body, report, calendar, source_requester=lambda *a, **k: FakeResponse())
    assert "Datum: 2026-10-28" in result
    assert "Uhrzeit: 14:00" in result
    assert "America/New_York" in result
    assert "federalreserve.gov" in result
    assert "Quellenprüfung: LIVE_VERIFIZIERT" in result
    assert "Pressekonferenz: 14:30 (im selben Eintrag bestätigt)" in result


def test_fomc_page_wide_matches_cannot_verify_different_calendar_rows(tmp_path):
    from trading_radar_market_first import _verify_fomc_official_source
    from datetime import date
    class FakeResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = ("<table><tr><td>October 27-28, 2026</td><td>Other event</td><td>2:00 p.m.</td></tr>"
                "<tr><td>October 29, 2026</td><td>FOMC meeting</td><td>2:30 p.m.</td></tr></table>")
    verified, detail = _verify_fomc_official_source(
        date(2026, 10, 28), requester=lambda *a, **k: FakeResponse()
    )
    assert not verified
    assert "same" not in detail.lower() or "Kein einzelner" in detail


def test_fomc_press_conference_not_claimed_without_same_event_time(tmp_path):
    from trading_radar_market_first import _enrich_fomc_event
    import json
    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28"]}), encoding="utf-8")
    report = "Datenstand: 2026-10-28T04:29:51+00:00"
    body = "- Ereignis: FOMC Zinsentscheid"
    class FakeResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = "<table><tr><td>October 27-28, 2026</td><td>FOMC meeting</td><td>2:00 p.m.</td></tr></table>"
    result = _enrich_fomc_event(body, report, calendar, source_requester=lambda *a, **k: FakeResponse())
    assert "Quellenprüfung: LIVE_VERIFIZIERT" in result
    assert "Pressekonferenz: NICHT SEPARAT VERIFIZIERT" in result


def test_fomc_same_day_event_is_skipped_after_1400_new_york_time(tmp_path):
    from trading_radar_market_first import _enrich_fomc_event
    import json
    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28", "2026-12-09"]}), encoding="utf-8")
    report = "Datenstand: 2026-10-28T19:00:00+00:00"
    body = "- Ereignis: FOMC Zinsentscheid"
    requested = []
    class FakeResponse:
        status_code = 200
        text = "<table><tr><td>December 8-9, 2026</td><td>FOMC meeting</td><td>2:00 p.m.</td></tr></table>"
        @property
        def url(self):
            return requested[-1]
    def requester(url, **kwargs):
        requested.append(url)
        return FakeResponse()
    result = _enrich_fomc_event(body, report, calendar, source_requester=requester)
    assert requested
    assert "2026-december.htm" in requested[0]
    assert "Datum: 2026-12-09" in result


def test_fomc_event_is_not_marked_verified_when_official_source_is_unavailable(tmp_path):
    from trading_radar_market_first import _enrich_fomc_event, normalize_event_section
    import json
    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28"]}), encoding="utf-8")
    report = "### 1. Trading-Radar\nDatenstand: 2026-10-28T04:29:51+00:00\n### 2. Thesen-Update\n### 3. Ereignisradar\n"
    body = "- Ereignis: FOMC-Zinsentscheid\nKennzahl: FOMC\nQuelle: Federal Reserve"
    result = _enrich_fomc_event(body, report, calendar, source_requester=lambda *a, **k: (_ for _ in ()).throw(TimeoutError("offline")))
    assert "Quellenprüfung: NICHT VERIFIZIERT" in result
    whole = report + result + "\n### 4. Assetklassenvergleich\n"
    _, findings = normalize_event_section(whole)
    assert any("nicht live" in finding.lower() for finding in findings)


def test_ohlcv_universe_gaps_are_not_a_blanket_report_veto():
    from trading_radar_market_first import validate_ohlcv_audit
    audit = {
        "status": "gaps", "universe_count": 862, "covered_count": 846,
        "missing_tickers": ["ADYEN.AS"], "stale_tickers": [],
        "recent_invalid_ohlcv_tickers": [],
        "affected_candidates": [{"ticker": "ADYEN.AS", "tradeability": "NICHT HANDELBAR"}],
    }
    assert validate_ohlcv_audit(audit) == []


def test_affected_candidate_is_downgraded_without_changing_other_candidate():
    from trading_radar_market_first import downgrade_ohlcv_affected_candidates
    report = """### 5. Technische Kandidaten
- **Asset/Ticker:** XPO, Inc. (XPO)
  - **Trigger:** Ausbruch
  - **Regel-/Datenstatus:** JETZT TECHNISCH PRÜFEN
- **Asset/Ticker:** Microsoft (MSFT)
  - **Trigger:** Ausbruch
  - **Regel-/Datenstatus:** JETZT TECHNISCH PRÜFEN

### 6. Portfolio & Risiko
"""
    audit = {"missing_tickers": ["XPO"], "stale_tickers": [], "recent_invalid_ohlcv_tickers": []}
    result = downgrade_ohlcv_affected_candidates(report, audit)
    xpo = result.split("Microsoft")[0]
    assert "NICHT HANDELBAR" in xpo
    assert "OHLCV-Datenhinweis" in xpo
    assert "JETZT TECHNISCH PRÜFEN" in result.split("Microsoft")[1]


def test_live_market_snapshot_adds_real_numeric_market_evidence():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    index = pd.date_range("2026-06-01", periods=80, freq="B")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame({col: [100 + i * 0.2 for i in range(len(index))] for col in columns}, index=index)
    report = "### 1. Trading-Radar\nMarktregime konstruktiv.\n\n### 2. Thesen-Update\n"
    result, findings = append_live_market_snapshot(report, fetcher=lambda requested: data)
    assert not findings
    assert "Index S&P 500" in result
    assert "Marktbreite-Proxy: 11/11" in result


def test_live_market_snapshot_inserts_inside_section_one_with_decorated_heading():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    index = pd.date_range("2026-06-01", periods=80, freq="B")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame({col: [100 + i * 0.2 for i in range(len(index))] for col in columns}, index=index)
    report = "### 1. Trading-Radar — Executive-Entscheidung\nMarktregime: konstruktiv.\n\n### 2. Thesen-Update (Register)\n"
    result, findings = append_live_market_snapshot(report, fetcher=lambda requested: data)
    assert findings == []
    assert result.index("**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**") < result.index("### 2. Thesen-Update")
    assert "Index S&P 500" in result.split("### 2. Thesen-Update")[0]
    assert result.count("**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**") == 1

    # Reprocessing must replace the previous snapshot, not duplicate it,
    # even when section 2 has a decorated heading suffix.
    repeated, repeated_findings = append_live_market_snapshot(result, fetcher=lambda requested: data)
    assert repeated_findings == []
    assert repeated.count("**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**") == 1
    assert "### 2. Thesen-Update (Register)" in repeated


def test_market_first_audit_does_not_fail_only_on_portfolio_length():
    from trading_radar_neuauswertung import audit_market_first_content
    long_portfolio = "Position mit dokumentiertem Datenstand. " * 100
    report = f"""1 · Trading-Radar
Marktregime: risk-off. Indizes/Marktbreite: S&P 500 und Nasdaq schwach; Marktbreite rückläufig.
Zinsen/Renditen: Treasury-Renditen steigen. Dollar/Liquidität/Volatilität: Dollar und VIX ziehen an; Liquidität eng.
Basisszenario: defensives Marktbild.
2 · Thesen-Update
Thesen-Name: T1
Gegenbelege: Gewinne stabilisieren sich trotz schwacher Breite.
Alternative: Seitwärtsmarkt bei nachlassender Volatilität.
Bestätigungstrigger: Marktbreite verbessert sich über mehrere Sitzungen.
Invalidierungstrigger: S&P 500 fällt unter das letzte bestätigte Swing-Tief.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke zeigt Rotation aus Growth in Energie; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: Kandidat A
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: Kandidat B
Asset/Ticker: Beispiel AG (ABC)
Statusgruppe: AUF TRIGGER WARTEN
Trigger: Ausbruch über bestätigten Widerstand
Gegenargument: Volumen bestätigt den Ausbruch noch nicht
Regel-/Datenstatus: Watchlist; nicht freigegeben.
6 · Portfolio & Risiko
{long_portfolio}
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert not any("Portfolio-Abschnitt ist deutlich umfangreicher" in error for error in errors)


def test_market_first_audit_accepts_compact_risk_regime_with_numeric_market_evidence():
    from trading_radar_neuauswertung import audit_market_first_content

    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth 7/11 sectors above their 50-day averages.
Zinsen/Renditen: US 10Y Treasury yield 4.2%.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4.
Basisszenario: selective risk-on with narrow breadth.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow across several sectors.
Alternative: sideways market if yields rise further.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energy outperforms growth; winners and losers diverge.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Portfolio impacts reviewed.
7 · MSCI-Vergleich
Comparison reviewed.
8 · Daten-/Prüfanhang
Data reviewed."""
    assert audit_market_first_content(report) == []


def test_market_first_audit_still_rejects_qualitative_only_rates_and_dollar_fields():
    from trading_radar_neuauswertung import audit_market_first_content

    report = """1 · Trading-Radar
Marktregime: risk-off.
Indizes/Marktbreite: S&P 500 breadth weakens across 7/11 sectors.
Zinsen/Renditen: Treasury yields stable across the curve.
Dollar/Liquidität/Volatilität: DXY stable and VIX lower, liquidity tight.
Basisszenario: defensive market conditions with weak breadth.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: earnings remain resilient despite weak breadth.
Alternative: sideways market if volatility fades.
Bestätigungstrigger: breadth improves across several sessions.
Invalidierungstrigger: S&P 500 falls below the last confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke shows rotation from growth into energy; winners and losers diverge.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Relevant impacts reviewed.
7 · MSCI-Vergleich
Comparison reviewed.
8 · Daten-/Prüfanhang
Data reviewed."""
    errors = audit_market_first_content(report)
    assert any("Zinsen/Renditen" in error for error in errors)
    assert any("Dollar/Liquidität/Volatilität" in error for error in errors)
    assert not any("Marktregime" in error for error in errors)

def test_market_first_audit_requires_fields_per_thesis_and_candidate():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Begründung: Markt bleibt konstruktiv; Trigger und Alternative wurden irgendwo erwähnt.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: Beispiel AG (ABC).
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
Asset/Ticker: Beispiel AG (ABC)
Trigger: Ausbruch
Gegenargument: Volumen fehlt
Regel-/Datenstatus: Watchlist
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert any("Gegenbelege" in e for e in errors)
    assert any("Alternative" in e for e in errors)
    assert any("Bestätigungstrigger" in e for e in errors)
    assert any("Invalidierungstrigger" in e for e in errors)
    assert any("Statusgruppe" in e for e in errors)


def test_market_first_audit_accepts_structurally_complete_thesis_and_candidate():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: Beispiel AG (ABC).
Asset/Ticker: Beispiel AG (ABC)
Statusgruppe: AUF TRIGGER WARTEN
Trigger: Ausbruch über Widerstand
Gegenargument: Volumen bestätigt nicht
Regel-/Datenstatus: Watchlist, nicht freigegeben.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []


def test_market_first_audit_accepts_four_explicitly_empty_candidate_groups():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []


def test_market_first_audit_rejects_candidate_in_wrong_group():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
Asset/Ticker: Beispiel AG (ABC)
Statusgruppe: NICHT HANDELBAR
Trigger: Ausbruch über Widerstand
Gegenargument: Volumen bestätigt nicht
Regel-/Datenstatus: Watchlist.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert any("stimmt nicht mit der umgebenden Kandidatengruppe" in e for e in errors)


def test_market_first_audit_rejects_explicitly_empty_group_with_candidate():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
Asset/Ticker: Beispiel AG (ABC)
Statusgruppe: AUF TRIGGER WARTEN
Trigger: Ausbruch über Widerstand
Gegenargument: Volumen bestätigt nicht
Regel-/Datenstatus: Watchlist.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert any("als leer markiert" in e for e in errors)


def test_market_first_audit_accepts_markdown_group_headings_with_empty_markers():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
### JETZT TECHNISCH PRÜFEN
Keine Kandidaten.
### AUF TRIGGER WARTEN
0 Kandidaten
### EREIGNIS ABWARTEN
Explizit leer
### NICHT HANDELBAR
Keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []


def test_market_first_audit_rejects_duplicate_candidate_group_headings():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert any("kommt mehrfach vor" in error for error in errors)


def test_market_first_audit_handles_optional_rejected_group_consistently():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: US 10Y Treasury yield 4.2% remains stable.
Dollar/Liquidität/Volatilität: DXY 102.3; VIX 18.4, both below prior readings.
Basisszenario: selective risk-on.
2 · Thesen-Update
Thesen-Name: T-01
Gegenbelege: breadth remains narrow.
Alternative: sideways market if yields rise.
Bestätigungstrigger: breadth improves for three sessions.
Invalidierungstrigger: S&P 500 closes below confirmed swing low.
3 · Ereignisradar
FOMC.
4 · Assetklassenvergleich
Relative Stärke: Energie outperformt Growth; Gewinner und Verlierer divergieren.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: keine Kandidaten.
AUF TRIGGER WARTEN: keine Kandidaten.
EREIGNIS ABWARTEN: keine Kandidaten.
NICHT HANDELBAR: keine Kandidaten.
### VERWERFEN / INVALIDIERT
Keine Kandidaten.
6 · Portfolio & Risiko
Portfolio.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []


def test_fomc_official_calendar_accepts_bounded_category_markup():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source

    html = """<html><body>
    <h2>FOMC Meetings</h2><div>Time</div><div>Release Date(s)</div>
    <div>2:00 p.m.</div><div>FOMC Meeting</div>
    <div>Two-day meeting, October 27 - 28, 2026</div>
    <div>Press Conference</div><div>2:30 p.m.</div>
    <div>FOMC Press Conference</div><div>28</div>
    <h2>Beige Book</h2><div>2:00 p.m.</div>
    </body></html>"""

    class Response:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = html

    verified, detail = _verify_fomc_official_source(date(2026, 10, 28), requester=lambda *a, **k: Response())
    assert verified
    assert "PRESSEKONFERENZ_VERIFIZIERT" in detail


def test_event_scenario_bullets_remain_in_same_event_record(monkeypatch):
    import trading_radar_market_first as market_first

    monkeypatch.setattr(market_first, "_enrich_fomc_event", lambda body, report_text: body)
    report = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  Datum: 2026-10-28
  Quelle: Federal Reserve, offizieller Kalender
  Uhrzeit: 14:00
  Zeitzone: America/New_York
  Kennzahl: Fed Funds Rate
  Zeitraum: Sitzungstermin 2026-10-28
  Letzter Wert: 3,75%-4,00%
  Konsens: 3,75%-4,00%
- Szenarien unter Konsens: unerwartete Zinssenkung
- Szenarien entsprechend Konsens: Korridor bleibt unverändert
- Szenarien über Konsens: unerwartete Zinserhöhung
- Reaktionskanäle: Anleihen, Devisen, Aktien

4. Assetklassenvergleich
"""
    result, findings = market_first.normalize_event_section(report)
    assert result.count("- Ereignis: FOMC-Zinsentscheid") == 1
    assert not any("ohne Datum" in item or "ohne verifizierte Quelle" in item for item in findings)


def test_fomc_official_calendar_retries_canonical_month_url_after_http_error():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source

    requested = []
    class ErrorResponse:
        status_code = 403
        url = "https://www.federalreserve.gov/newsevents/2026-december.htm?view=calendar"
        text = "blocked"

    class SuccessResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-december.htm"
        text = """<html><head><title>Calendar December 2026</title></head><body>
        <h2>FOMC Meetings</h2><div>Time</div><div>Release Date(s)</div>
        <div>2:00 p.m.</div><div>FOMC Meeting</div>
        <div>Two-day meeting, December 8 - 9</div>
        <div>Press Conference</div><div>2:30 p.m.</div>
        <div>FOMC Press Conference</div><div>9</div>
        <h2>Beige Book</h2><div>2:00 p.m.</div>
        </body></html>"""

    def requester(url, **kwargs):
        requested.append(url)
        return ErrorResponse() if len(requested) == 1 else SuccessResponse()

    verified, detail = _verify_fomc_official_source(date(2026, 12, 9), requester=requester)
    assert verified
    assert len(requested) == 2
    assert "HTTP 403" in detail
    assert "PRESSEKONFERENZ_VERIFIZIERT" in detail


def test_fomc_official_calendar_failure_diagnostic_includes_both_urls():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source

    requested = []
    def requester(url, **kwargs):
        requested.append(url)
        raise OSError("connection reset")

    verified, detail = _verify_fomc_official_source(date(2026, 12, 9), requester=requester)
    assert not verified
    assert len(requested) == 2
    assert "connection reset" in detail
    assert "2026-december.htm?view=calendar" in detail
    assert "2026-december.htm" in detail


def test_ohlcv_audit_accepts_complete_candidate_mapping_with_no_affected_candidates():
    from trading_radar_market_first import validate_ohlcv_audit
    audit = {
        "status": "gaps",
        "universe_count": 862,
        "covered_count": 862,
        "affected_candidates": [],
        "candidate_tickers_checked": ["AEP", "REGN"],
        "candidate_mapping_complete": True,
        "unmapped_candidate_tickers": [],
    }
    assert validate_ohlcv_audit(audit) == []


def test_ohlcv_audit_does_not_accept_empty_affected_list_without_mapping_proof():
    from trading_radar_market_first import validate_ohlcv_audit
    audit = {
        "status": "gaps",
        "universe_count": 862,
        "covered_count": 862,
        "affected_candidates": [],
        "candidate_tickers_checked": [],
        "candidate_mapping_complete": False,
        "unmapped_candidate_tickers": [],
    }
    findings = validate_ohlcv_audit(audit)
    assert any("Kandidaten-Mapping-Nachweis" in item for item in findings)


def test_ohlcv_audit_keeps_affected_candidate_rows_visible():
    from trading_radar_market_first import validate_ohlcv_audit
    audit = {
        "status": "gaps",
        "universe_count": 862,
        "covered_count": 862,
        "affected_candidates": ["AEP"],
        "candidate_tickers_checked": ["AEP", "REGN"],
        "candidate_mapping_complete": True,
        "unmapped_candidate_tickers": [],
    }
    assert validate_ohlcv_audit(audit) == []


def test_report_candidate_parser_reads_ticker_from_asset_ticker_field():
    import Trading_Radar_Alleinauswertung as report_module
    report = """# 5. Technische Kandidaten
- **Asset/Ticker**: American Electric Power Company, Inc. (AEP)
  - **Regel-/Datenstatus**: NICHT HANDELBAR

- **Asset/Ticker**: Regeneron Pharmaceuticals, Inc. (REGN)
  - **Regel-/Datenstatus**: NICHT HANDELBAR

# 6. Portfolio & Risiko
"""
    assert report_module._trading_radar_kandidaten_aus_abschnitt5(report) == ["AEP", "REGN"]


def test_market_first_normalizer_adds_evidence_labels_and_groups_nontradable_records():
    from trading_radar_neuauswertung import normalize_market_first_report_structure, audit_market_first_content
    report = """# 1. Trading-Radar
**Hauptthese**: Moderate Marktstabilisierung.
- **Stärkste Belege**: S&P 500 stabil.
- **Wichtigste Gegenbelege**: Small Caps schwach.
- **Nächste beobachtbare Bestätigung/Invalidierung**: Qualitätshoch bestätigen; Rendite über 5,20% invalidiert.

Live-Marktbelege:
- Index S&P 500 (^GSPC): letzter Schluss 100; 1M +1%; 3M +2%.
- Volatilität VIX: 17.
- Dollar-Proxy DXY: 102.
- US-10J-Rendite (^TNX): 5,00%.
- Marktbreite-Proxy: 8/11 Sektor-ETFs über 50-Tage-Durchschnitt.

# 2. Thesen-Update
**Thesen-ID**: TH-1
**Version**: 1
**Status**: BESTÄTIGT
**Begründung**: Defensive Rotation bleibt sichtbar.
**Gegenbelege**: Small Caps schwach.
**Alternative**: Rotation endet.
**Bestätigungstrigger**: Qualitäts-ETF überschreitet Hoch.
**Invalidierungstrigger**: Rendite über 5,20%.

# 3. Ereignisradar
Keine relevanten Ereignisse im geprüften Zeitraum.

# 4. Assetklassenvergleich
Aktien zeigen relative Stärke.

# 5. Technische Kandidaten
- **Asset/Ticker**: American Electric Power Company, Inc. (AEP)
  - **Trigger**: A-Status gemeldet, Setup nicht bestätigt.
  - **Einstieg**: NICHT VERFÜGBAR
  - **Stop**: NICHT VERFÜGBAR
  - **TP1**: NICHT VERFÜGBAR
  - **TP2**: NICHT VERFÜGBAR
  - **CRV**: NICHT VERFÜGBAR
  - **Gegenargument**: Levels fehlen.
  - **Regel-/Datenstatus**: NICHT HANDELBAR – Statusmeldung ist kein Kauf.

# 6. Portfolio & Risiko
Keine offenen Positionen.

# 7. MSCI-Vergleich
Kennzahlen nicht verfügbar.

# 8. Daten-/Prüfanhang
OHLCV-Abdeckung: 862/862; Status=gaps.
"""
    normalized = normalize_market_first_report_structure(report)
    assert "Indizes/Marktbreite:" in normalized
    assert "Dollar/Liquidität/Volatilität:" in normalized
    assert "Statusgruppe: NICHT HANDELBAR" in normalized
    findings = audit_market_first_content(normalized)
    assert not any("Kandidatengruppe" in finding or "Statusgruppe fehlt" in finding for finding in findings), findings
