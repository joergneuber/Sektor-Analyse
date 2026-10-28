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


def test_market_first_audit_requires_fields_per_thesis_and_candidate():
    from trading_radar_neuauswertung import audit_market_first_content
    report = """1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
Zinsen/Renditen: Treasury yields stable.
Dollar/Liquidität/Volatilität: DXY stable, VIX lower.
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
