from datetime import datetime, timezone, timedelta
import json
import pytest
from trading_radar_market_first import normalize_event, deduplicate_events, classify_ohlcv_row, summarize_ohlcv_status


def test_event_missing_time_timezone_marked_once_and_not_verified():
    event = normalize_event({"name": "FOMC", "date": "2026-10-28", "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                             "source_verification": "LIVE_VERIFIZIERT — HTTP 200; Datum, FOMC und 14:00 Uhr im abgegrenzten offiziellen Kalenderbereich bestätigt (https://www.federalreserve.gov/newsevents/2026-october.htm; Abruf: https://www.federalreserve.gov/newsevents/2026-october.htm)",
                             "time": "NICHT VERFÜGBAR", "timezone": "NICHT VERFÜGBAR"})
    assert event["time"] is None
    assert event["timezone"] is None
    assert event["time_verified"] is False
    # A historical report diagnostic is not proof of an executed verifier.
    assert event["status"] == "INCOMPLETE"
    assert event["missing_required"] == ["source"]


def test_event_duplicates_are_collapsed():
    rows = [{"name": "CPI", "date": "2026-11-12", "source": "https://www.bls.gov/schedule/news_release/cpi.htm"},
            {"name": "CPI", "date": "2026-11-12", "source": "https://www.bls.gov/schedule/news_release/cpi.htm"}]
    assert len(deduplicate_events(rows)) == 1


def test_event_deduplication_is_casefolded_and_preserves_first_record():
    first = {
        "event_id": "FOMC-Decision",
        "name": "Federal Reserve Rate Decision",
        "date": "2026-11-05",
        "source": "HTTPS://WWW.FEDERALRESERVE.GOV/newsevents/2026-November.htm",
    }
    duplicate = {
        "event_id": "fomc-decision",
        "name": "federal reserve rate decision",
        "date": " 2026-11-05 ",
        "source": "https://www.federalreserve.gov/newsevents/2026-November.htm",
    }

    result = deduplicate_events([first, duplicate])

    assert len(result) == 1
    assert result[0]["event_id"] == "FOMC-Decision"
    assert result[0]["name"] == "Federal Reserve Rate Decision"
    assert result[0]["source"] == first["source"]


def test_event_deduplication_normalizes_unicode_and_whitespace_only_for_key():
    rows = [
        {"event_id": "ＣＰＩ", "name": "  Core   CPI ", "date": "2026-11-05", "source": "BLS"},
        {"event_id": "cpi", "name": "core cpi", "date": "2026-11-05", "source": "bls"},
    ]
    assert len(deduplicate_events(rows)) == 1


def test_event_deduplication_does_not_casefold_case_sensitive_url_paths():
    rows = [
        {"event_id": "E1", "name": "CPI", "date": "2026-11-05", "source": "Official calendar (https://example.org/Calendar)"},
        {"event_id": "E1", "name": "CPI", "date": "2026-11-05", "source": "official calendar (https://example.org/calendar)"},
    ]
    assert len(deduplicate_events(rows)) == 2


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("2026-11-20T21:14:00+00:00", "2026-11-19"),  # 14 minutes after Friday close
        ("2026-11-20T21:15:00+00:00", "2026-11-20"),  # publication buffer elapsed
        ("2026-11-21T17:00:00+00:00", "2026-11-20"),  # Saturday
        ("2026-11-22T23:00:00+00:00", "2026-11-20"),  # Sunday
        ("2026-11-23T14:00:00+00:00", "2026-11-20"),  # Monday before close
        ("2026-11-23T21:16:00+00:00", "2026-11-23"),  # Monday after close + buffer
        ("2026-11-26T22:00:00+00:00", "2026-11-25"),  # Thanksgiving
        ("2026-11-27T18:00:00+00:00", "2026-11-25"),  # Black Friday before 13:15 ET
        ("2026-11-27T18:16:00+00:00", "2026-11-27"),  # Black Friday after early close + buffer
    ],
)
def test_completed_nyse_session_cutoff_uses_real_xnys_sessions(clock, expected):
    from trading_radar_market_first import _completed_nyse_session_cutoff

    assert _completed_nyse_session_cutoff(clock).isoformat() == expected


def test_completed_nyse_session_cutoff_fails_closed_on_calendar_runtime_error(monkeypatch):
    import exchange_calendars
    from trading_radar_market_first import _completed_nyse_session_cutoff

    def broken_calendar(_name):
        raise RuntimeError("calendar database unavailable")

    monkeypatch.setattr(exchange_calendars, "get_calendar", broken_calendar)
    with pytest.raises(RuntimeError, match="calendar database unavailable"):
        _completed_nyse_session_cutoff("2026-11-22T22:00:00+00:00")


def test_market_snapshot_fails_closed_when_xnys_calendar_is_unavailable(monkeypatch):
    import builtins
    from trading_radar_market_first import append_live_market_snapshot

    report = (
        "1. Trading-Radar\nIndizes/Marktbreite: neutral.\nBasisszenario: neutral.\n"
        "2. Thesen-Update\n3. Ereignisradar\n"
        "4. Assetklassenvergleich\n5. Technische Kandidaten\n"
    )
    fetch_called = False
    original_import = builtins.__import__

    def import_without_exchange_calendar(name, *args, **kwargs):
        if name == "exchange_calendars":
            raise ImportError("calendar deliberately unavailable in regression test")
        return original_import(name, *args, **kwargs)

    def fetcher(_symbols):
        nonlocal fetch_called
        fetch_called = True
        raise AssertionError("fetcher must not run without a trusted session cutoff")

    monkeypatch.setattr(builtins, "__import__", import_without_exchange_calendar)
    output, findings = append_live_market_snapshot(
        report, fetcher=fetcher, as_of_time="2026-11-22T22:00:00+00:00"
    )

    assert output == report
    assert not fetch_called
    assert len(findings) == 1
    assert "NYSE-Kalenderfehler" in findings[0]
    assert "calendar deliberately unavailable" in findings[0]


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
    assert "Event-ID: FOMC_DECISION" in result
    assert "Event-ID: FOMC_PRESS_CONFERENCE" in result
    assert "Ereignis: FOMC Pressekonferenz" in result
    assert "Uhrzeit: 14:30" in result



def test_fomc_enrichment_emits_separate_press_conference_event_identity(tmp_path):
    from trading_radar_market_first import _enrich_fomc_event, normalize_event_section
    import json

    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28"]}), encoding="utf-8")
    report = "Datenstand: 2026-10-28T04:29:51+00:00"
    body = "- Ereignis: FOMC-Zinsentscheid\n- Kennzahl: FOMC Zinsentscheid"

    class Response:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = "<table><tr><td>October 27-28, 2026</td><td>FOMC meeting</td><td>2:00 p.m. Press Conference 2:30 p.m.</td></tr></table>"

    results = {}
    enriched = _enrich_fomc_event(
        body, report, calendar, source_requester=lambda *a, **k: Response(),
        verification_results=results,
    )
    assert results[("2026-10-28", "DECISION")].event_id == "FOMC_DECISION"
    assert results[("2026-10-28", "PRESS_CONFERENCE")].event_id == "FOMC_PRESS_CONFERENCE"
    normalized, findings = normalize_event_section(
        "3. Ereignisradar\n" + enriched + "\n4. Assetklassenvergleich\n",
        fomc_requester=lambda *a, **k: Response(),
    )
    assert normalized.count("- Ereignis: FOMC-Zinsentscheid") == 1
    assert normalized.count("- Ereignis: FOMC Pressekonferenz") == 1
    assert "Ereignis ohne verifizierte Quelle" not in findings

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
    assert "Ereignis: FOMC Pressekonferenz" not in result
    assert "Event-ID: FOMC_DECISION" in result


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
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert not findings
    assert "Index S&P 500" in result
    assert "Marktbreite-Proxy: 11/11" in result


def test_live_market_snapshot_populates_canonical_m02_fields_from_real_snapshot():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    index = pd.date_range("2026-06-01", periods=80, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    values = {col: [100.0 + i * 0.2 for i in range(len(index))] for col in columns}
    values[("^VIX", "Close")] = [18.0 + i * 0.01 for i in range(len(index))]
    values[("DX-Y.NYB", "Close")] = [101.0 + i * 0.01 for i in range(len(index))]
    data = pd.DataFrame(values, index=index)
    report = (
        "### 1. Trading-Radar\nMarktregime: konstruktiv.\n"
        "Dollar/Liquidität/Volatilität: M2-Geldmenge 23.413,90 Mrd. USD; Name nicht aufgelöst (VIX).\n"
        "### 2. Thesen-Update\n"
    )
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert findings == []
    risk = next(line for line in result.splitlines() if line.startswith("Dollar/Liquidität/Volatilität:"))
    assert "VIX: 18.79" in risk
    assert "DXY: 101.79" in risk
    assert "Marktregime-Datenstichtag: 2026-09-18" in result


def test_live_market_snapshot_inserts_inside_section_one_with_decorated_heading():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    index = pd.date_range("2026-06-01", periods=80, freq="B")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame({col: [100 + i * 0.2 for i in range(len(index))] for col in columns}, index=index)
    report = "### 1. Trading-Radar — Executive-Entscheidung\nMarktregime: konstruktiv.\n\n### 2. Thesen-Update (Register)\n"
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert findings == []
    assert result.index("**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**") < result.index("### 2. Thesen-Update")
    assert "Index S&P 500" in result.split("### 2. Thesen-Update")[0]
    assert result.count("**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**") == 1

    # Reprocessing must replace the previous snapshot, not duplicate it,
    # even when section 2 has a decorated heading suffix.
    repeated, repeated_findings = append_live_market_snapshot(
        result, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
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


def test_fomc_official_calendar_returns_separate_decision_and_press_evidence():
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

    requester = lambda *a, **k: Response()
    decision = _verify_fomc_official_source(date(2026, 10, 28), requester=requester)
    press = _verify_fomc_official_source(
        date(2026, 10, 28), requester=requester, event_kind="PRESS_CONFERENCE"
    )
    assert decision.verified and decision.event_id == "FOMC_DECISION" and decision.event_time == "14:00"
    assert press.verified and press.event_id == "FOMC_PRESS_CONFERENCE" and press.event_time == "14:30"
    wrong_day = _verify_fomc_official_source(date(2026, 10, 27), requester=requester)
    assert not wrong_day.verified




def test_fomc_separate_press_row_is_bound_to_the_confirmed_meeting_end_date():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source

    class Response:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = """<html><title>October 2026</title><table>
        <tr><td>October 27 - 28</td><td>FOMC Meeting</td><td>2:00 p.m.</td></tr>
        <tr><td>28</td><td>FOMC Press Conference</td><td>2:30 p.m.</td></tr>
        </table></html>"""

    requester = lambda *a, **k: Response()
    decision = _verify_fomc_official_source(date(2026, 10, 28), requester=requester)
    press = _verify_fomc_official_source(
        date(2026, 10, 28), requester=requester, event_kind="PRESS_CONFERENCE"
    )
    wrong_day = _verify_fomc_official_source(
        date(2026, 10, 27), requester=requester, event_kind="PRESS_CONFERENCE"
    )
    assert decision.verified
    assert press.verified and press.event_id == "FOMC_PRESS_CONFERENCE"
    assert not wrong_day.verified

def test_fomc_decision_evidence_cannot_verify_press_conference_event():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source, normalize_event

    class Response:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = "<tr><td>October 27-28, 2026</td><td>FOMC Meeting</td><td>2:00 p.m. Press Conference 2:30 p.m.</td></tr>"

    decision = _verify_fomc_official_source(date(2026, 10, 28), requester=lambda *a, **k: Response())
    press_event = normalize_event({
        "name": "FOMC Pressekonferenz", "date": "2026-10-28",
        "source": "Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_verification": "LIVE_VERIFIZIERT",
    }, verification_evidence=decision)
    assert press_event["source_verified"] is False
    press = _verify_fomc_official_source(
        date(2026, 10, 28), requester=lambda *a, **k: Response(), event_kind="PRESS_CONFERENCE"
    )
    press_event = normalize_event({
        "name": "FOMC Pressekonferenz", "date": "2026-10-28",
        "source": "Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_verification": "LIVE_VERIFIZIERT",
    }, verification_evidence=press)
    assert press_event["source_verified"] is True

def test_event_scenario_bullets_remain_in_same_event_record(monkeypatch):
    import trading_radar_market_first as market_first

    class FedResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = "<html><title>October 2026</title><tr><td>October 27 - 28</td><td>FOMC Meeting</td><td>2:00 p.m.</td><td>2:30 p.m.</td></tr></html>"
    report = """3. Ereignisradar
- Ereignis: FOMC-Zinsentscheid
  Datum: 2026-10-28
  Quelle: Federal Reserve, offizieller Kalender (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
  Quellenprüfung: NICHT VERIFIZIERT — vorhandener Reporttext ist kein Prüfergebnis
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
    result, findings = market_first.normalize_event_section(
        report, fomc_requester=lambda *a, **k: FedResponse()
    )
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

    decision = _verify_fomc_official_source(date(2026, 12, 9), requester=requester)
    assert decision.verified
    assert len(requested) == 2
    assert "HTTP 403" in decision.detail
    press = _verify_fomc_official_source(
        date(2026, 12, 9), requester=lambda *a, **k: SuccessResponse(),
        event_kind="PRESS_CONFERENCE",
    )
    assert press.verified
    assert press.event_id == "FOMC_PRESS_CONFERENCE"


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


def test_report_candidate_parser_excludes_explicit_nontradable_archive_rows():
    import Trading_Radar_Alleinauswertung as report_module
    report = """# 5. Technische Kandidaten
- Asset/Ticker: Amazon.com, Inc. (AMZN)
  - Statusgruppe: JETZT TECHNISCH PRÜFEN
- Asset/Ticker: Paramount Global (PARA)
  - Statusgruppe: NICHT HANDELBAR
- Asset/Ticker: Sony Group Corporation (SONY)
  - Statusgruppe: VERWERFEN / INVALIDIERT
# 6. Portfolio & Risiko
"""
    assert report_module._trading_radar_kandidaten_aus_abschnitt5(report) == ["AMZN"]


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



def test_market_first_audit_accepts_numeric_m2_as_liquidity_evidence():
    from trading_radar_neuauswertung import audit_market_first_content

    report = """1 · Trading-Radar
Marktregime: Restriktives, zinsgetriebenes Makroregime mit defensiver Rotation.
Indizes/Marktbreite: S&P 500 7/11 Sektor-ETFs über ihrem 50-Tage-Durchschnitt.
Zinsen/Renditen: US 10Y Treasury bei 5,00%.
Dollar/Liquidität/Volatilität: M2 bei 23.413,9 Mrd. USD (Stand 01.09.2026).
Basisszenario: Seitwärts bis moderat aufwärts bei erhöhtem Selektionsdruck.
2 · Thesen-Update
Keine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.
3 · Ereignisradar
Keine relevanten Ereignisse im geprüften Zeitraum.
4 · Assetklassenvergleich
Relative Stärke: defensive Sektoren stärker, zyklische Segmente schwächer.
5 · Technische Kandidaten
JETZT TECHNISCH PRÜFEN: Keine Kandidaten
AUF TRIGGER WARTEN: Keine Kandidaten
EREIGNIS ABWARTEN: Keine Kandidaten
NICHT HANDELBAR: Keine Kandidaten
6 · Portfolio & Risiko
Portfolio-Risiken geprüft.
7 · MSCI-Vergleich
Benchmark-Vergleich geprüft.
8 · Daten-/Prüfanhang
Datenstand geprüft."""
    errors = audit_market_first_content(report)
    assert not any("Dollar/Liquidität/Volatilität" in error for error in errors)


def test_ohlcv_gaps_with_full_symbol_coverage_are_not_misreported_as_missing_symbols():
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
    assert any("Datenqualitätsstatus 'gaps' trotz Symbolabdeckung 862/862" in item for item in findings)
    assert not any("Abdeckung nicht vollständig: 862/862" in item for item in findings)


def test_ohlcv_gaps_allow_explicit_zero_candidate_report_without_candidate_mapping():
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
    assert validate_ohlcv_audit(audit, explicit_no_candidates=True) == []


def test_noncanonical_numbered_heading_does_not_get_swallowed_into_previous_section():
    import Trading_Radar_Alleinauswertung as production

    required = (
        "1. Trading-Radar",
        "2. Thesen-Update",
        "3. Ereignisradar",
        "4. Assetklassenvergleich",
        "5. Technische Kandidaten",
        "6. Portfolio & Risiko",
        "7. MSCI-Vergleich",
        "8. Daten-/Prüfanhang",
    )
    raw = """### 1. Trading-Radar
Marktüberblick.
### 2. Thesenkontinuität
Alter/abweichender Abschnittstitel.
### 3. Ereignisradar
Ereignisse.
### 4. Assetklassenvergleich
Rotation.
### 5. Technische Kandidaten
Keine Kandidaten.
### 6. Portfolio & Risiko
Risiko.
### 7. MSCI-Vergleich
Benchmark.
### 8. Daten-/Prüfanhang
Daten."""
    sections = production._gemini_radar_abschnitte(raw, required)
    assert "2. Thesen-Update" not in sections
    assert "Thesenkontinuität" not in sections["1. Trading-Radar"]
    retry = "### 2. Thesen-Update\nKeine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung."
    merged, missing = production._gemini_radar_abschnitte_zusammenfuehren(
        raw, retry, required, ["2. Thesen-Update"]
    )
    assert missing == []
    assert merged.count("### 2.") == 1
    assert "### 2. Thesen-Update" in merged
    assert "Thesenkontinuität" not in merged



def test_empty_candidate_groups_are_normalized_before_ohlcv_assessment():
    import Trading_Radar_Alleinauswertung as production
    from trading_radar_neuauswertung import normalize_market_first_report_structure
    from trading_radar_market_first import (
        report_explicitly_declares_no_technical_candidates,
        report_has_explicit_empty_candidate_groups,
        validate_ohlcv_audit,
    )

    report = """### 1. Trading-Radar
Marktregime: Restriktives Marktregime mit defensiver Rotation.
### 2. Thesen-Update
Keine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.
### 3. Ereignisradar
Keine relevanten Ereignisse im geprüften Zeitraum.
### 4. Assetklassenvergleich
Relative Stärke: defensive Werte stärker als zyklische Werte.
### 5. Technische Kandidaten
Keine belastbaren technischen Kandidaten; keine Handelsfreigabe.
### 6. Portfolio & Risiko
Risiko geprüft.
### 7. MSCI-Vergleich
Benchmark geprüft.
### 8. Daten-/Prüfanhang
Daten geprüft."""
    normalized = normalize_market_first_report_structure(report)
    assert report_explicitly_declares_no_technical_candidates(normalized)
    assert report_has_explicit_empty_candidate_groups(normalized)
    assert production._trading_radar_kandidaten_aus_abschnitt5(normalized) == []

    audit = {
        "status": "gaps",
        "universe_count": 862,
        "covered_count": 862,
        "affected_candidates": [],
        "candidate_tickers_checked": [],
        "candidate_mapping_complete": False,
        "unmapped_candidate_tickers": [],
    }
    assert validate_ohlcv_audit(audit, explicit_no_candidates=True) == []


def test_relative_strength_matrix_uses_common_cutoff_and_identical_21_63_day_windows():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
    ]
    index = pd.date_range("2026-05-01", periods=100, freq="B")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame({
        col: [100.0 + i * (0.1 + j * 0.001) for i in range(len(index))]
        for j, col in enumerate(columns)
    }, index=index)
    report = (
        "### 1. Trading-Radar\nMarktregime: neutral.\n"
        "### 2. Thesen-Update\nThesenstatus: keine neue These.\n"
        "### 4. Assetklassenvergleich\nVergleich.\n"
        "### 5. Technische Kandidaten\nKeine Kandidaten.\n"
    )
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert findings == []
    assert "Relative-Stärke-Matrix" in result
    assert "Gemeinsamer Datenstichtag: 2026-" in result
    for group in ("Indizes:", "Regionen:", "Sektoren:", "Anlageklassen:"):
        assert group in result
    assert "Gewinner:" in result and "Verlierer:" in result and "Divergenz:" in result
    matrix = result.split("Relative-Stärke-Matrix", 1)[1].split("### 5.", 1)[0]
    assert "1M " in matrix and "3M " in matrix
    assert "1M NICHT VERFÜGBAR" not in matrix


def test_live_snapshot_labels_distinct_treasury_dates_as_temporal_observations():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
    ]
    index = pd.date_range("2026-05-01", periods=100, freq="B")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame({col: [100.0] * len(index) for col in columns}, index=index)
    data[("^TNX", "Close")] = 4.99
    report = (
        "### 1. Trading-Radar\n10-jährige US-Treasury-Rendite: 5,0000%.\n"
        "### 2. Thesen-Update\nThesen.\n"
        "### 4. Assetklassenvergleich\nVergleich.\n"
        "### 5. Technische Kandidaten\nKeine Kandidaten.\n"
    )
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert findings == []
    assert "GETRENNT DOKUMENTIERTE US-10Y-BEOBACHTUNGEN" in result
    assert "QUELLENKONFLIKT: OFFEN" not in result
    assert "Makro-Wert: 5.0000%" in result
    assert "Live-Wert: 4.9900%" in result



def test_relative_strength_keeps_expected_session_when_one_core_ticker_is_stale():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
    ]
    index = pd.date_range("2026-05-01", periods=100, freq="B")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.1 for i in range(len(index))] for col in columns},
        index=index,
    )
    # One core index is stale. It must be reported as a gap, not move the
    # common expected session backwards for every other index/region/sector.
    data[("^GSPC", "Close")] = pd.Series(
        [100.0 + i * 0.1 for i in range(len(index) - 1)], index=index[:-1]
    )
    report = (
        "### 1. Trading-Radar\nMarktregime: neutral.\n"
        "### 2. Thesen-Update\nKeine neue These.\n"
        "### 4. Assetklassenvergleich\nVergleich.\n"
        "### 5. Technische Kandidaten\nKeine Kandidaten.\n"
    )
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert "Relative-Stärke-Matrix" in result, f"findings={findings!r}; tail={result[-1000:]!r}"
    matrix = result.split("Relative-Stärke-Matrix", 1)[1].split("### 5.", 1)[0]
    assert findings == []
    assert f"Gemeinsamer Datenstichtag: {index[-1].date().isoformat()}T00:00:00+00:00" in matrix
    sp500 = next(line for line in matrix.splitlines() if "S&P 500 (^GSPC)" in line)
    assert "1M NICHT VERFÜGBAR" in sp500
    assert "kein Kurs exakt am gemeinsamen Stichtag" in sp500
    assert "- Index S&P 500 (^GSPC): NICHT VERFÜGBAR." in result
    assert "Nasdaq (^IXIC) 1M +" in matrix
    assert "Russell 2000 (^RUT) 1M +" in matrix


def test_m02_uses_explicit_uup_proxy_when_spot_dxy_missing_on_core_cutoff():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
               "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD"]
    index = pd.date_range("2026-06-01", periods=80, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame({col: [100.0 + i * 0.2 for i in range(len(index))] for col in columns}, index=index)
    # Crypto must not move the core equity cutoff. Spot DXY is missing on that
    # date, so the UUP ETF is an explicitly named, same-date dollar proxy.
    data.loc[index[-1], ("DX-Y.NYB", "Close")] = float("nan")
    report = (
        "### 1. Trading-Radar\nMarktregime: konstruktiv.\n"
        "Dollar/Liquidität/Volatilität: M2-Liquidität wird beobachtet.\n"
        "### 2. Thesen-Update\n"
    )
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert findings == []
    assert "Marktregime-Datenstichtag: 2026-09-18" in result
    assert "Dollar-Proxy UUP (US-Dollar-ETF):" in result
    assert "Quelle: Yahoo Finance (UUP)" in result
    risk = next(line for line in result.splitlines() if line.startswith("Dollar/Liquidität/Volatilität:"))
    assert "Dollarproxy:" in risk
    assert "DXY: NICHT VERFÜGBAR" not in risk


def test_event_date_is_promoted_from_one_explicit_valid_period_date():
    row = normalize_event({
        "name": "ADP Weekly Employment Change",
        "period": "Wöchentlich / In 2 Tagen (2026-11-17)",
        "source": "https://www.bls.gov/schedule/news_release/empsit.htm",
    })
    assert row["date"] == "2026-11-17"
    assert row["status"] == "READY"
    assert row["period"] == "Wöchentlich / In 2 Tagen (2026-11-17)"


def test_event_section_promotes_only_one_explicit_period_date():
    from trading_radar_market_first import normalize_event_section

    report = (
        "3. Ereignisradar\n"
        "- Ereignis-Uhrzeit: NICHT VERFÜGBAR\n"
        "- Kennzahl: ADP Weekly Employment Change\n"
        "- Zeitraum: Wöchentlich / In 2 Tagen (2026-11-17)\n"
        "- Quelle: https://www.bls.gov/schedule/news_release/empsit.htm\n"
        "Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)\n"
        "Quellenprüfung: NICHT VERIFIZIERT (Quelle fehlt, ist nicht vertrauenswürdig oder nicht live verifiziert)\n"
        "\n4. Assetklassenvergleich\nAktien und Anleihen werden verglichen.\n"
    )
    normalized, findings = normalize_event_section(report)
    assert "Datum: 2026-11-17" in normalized
    assert "DATUM AUS ZEITRAUM ÜBERNOMMEN" not in normalized
    assert "Ereignis ohne Datum" not in findings
    assert "Ereignis ohne verifizierte Quelle" in findings

    relative = normalize_event({"name": "CPI", "period": "In 2 Tagen", "source": "https://www.bls.gov/schedule/news_release/cpi.htm"})
    ambiguous = normalize_event({"name": "CPI", "period": "2026-11-17 bis 2026-11-18", "source": "https://www.bls.gov/schedule/news_release/cpi.htm"})
    invalid = normalize_event({"name": "CPI", "period": "2026-02-30", "source": "https://www.bls.gov/schedule/news_release/cpi.htm"})
    assert relative["date"] is None
    assert ambiguous["date"] is None
    assert invalid["date"] is None
    german = normalize_event({"name": "CPI", "period": "17.11.2026", "source": "https://www.bls.gov/schedule/news_release/cpi.htm"})
    assert german["date"] == "2026-11-17"


def test_explicit_event_date_is_not_relabelled_as_inferred_from_period():
    from trading_radar_market_first import normalize_event_section

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: FOMC-Zinsentscheid\n"
        "- Datum: 2026-11-17\n"
        "- Zeitraum: Sitzungstermin 2026-11-17\n"
        "- Quelle: Federal Reserve, offizieller Kalender (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)\n"
        "- Quellenprüfung: NICHT VERIFIZIERT (Quelle fehlt, ist nicht vertrauenswürdig oder nicht live verifiziert)\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(report)

    assert "Datum: 2026-11-17" in normalized
    assert "nicht live verifiziert" in normalized
    assert "DATUM AUS ZEITRAUM ÜBERNOMMEN" not in normalized
    assert "Ereignis ohne Datum" not in findings


def test_fred_observations_api_is_not_a_verified_release_calendar_source():
    from trading_radar_market_first import normalize_event_section

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: Initial Jobless Claims\n"
        "- Datum: 2026-11-19\n"
        "- Zeitraum: Wöchentlich\n"
        "- Quelle: https://api.stlouisfed.org/fred/series/observations\n"
        "- Quellenprüfung: LIVE_VERIFIZIERT\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(report)
    assert "Ereignis ohne Datum" not in findings
    assert "Ereignis ohne verifizierte Quelle" in findings
    assert "NICHT VERIFIZIERT" in normalized
    assert "LIVE_VERIFIZIERT" not in normalized


def test_explicit_event_date_is_confirmed_only_by_real_forexfactory_adapter():
    import json
    from trading_radar_market_first import normalize_event_section

    class FFResponse:
        status_code = 200
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        text = json.dumps([{
            "id": "ff-123", "country": "USD", "date": "2026-11-17T13:15:00-05:00",
            "title": "ADP Employment Report", "time": "08:15am", "timezone": "America/New_York",
        }])
        def json(self):
            return json.loads(self.text)

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: ADP: ADP Employment Report\n"
        "- Datum: 2026-11-17\n"
        "- Zeitraum: November 2026\n"
        "- Quelle: ForexFactory (LIVE)\n"
        "- Quellenprüfung: LIVE_VERIFIZIERT — Textmarker allein\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, forexfactory_requester=lambda *a, **k: FFResponse()
    )
    assert findings == []
    assert "Datum: 2026-11-17" in normalized
    assert "ForexFactory LIVE adapter" in normalized
    assert "HTTP 200" in normalized



def test_unresolved_event_clock_headers_use_metric_for_live_forexfactory_verification():
    import json
    from trading_radar_market_first import normalize_event_section

    class FFResponse:
        status_code = 200
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        text = json.dumps([
            {
                "id": "ff-adp-20261117", "country": "USD",
                "date": "2026-11-17T08:15:00-05:00",
                "title": "ADP Weekly Employment Change",
            },
            {
                "id": "ff-claims-20261119", "country": "USD",
                "date": "2026-11-19T08:30:00-05:00",
                "title": "Unemployment Claims",
            },
        ])

        def json(self):
            return json.loads(self.text)

    report = (
        "3. Ereignisradar\n"
        "- Ereignis-Uhrzeit: Name nicht aufgelöst (HEUTE) (2026-11-17)\n"
        "  - Zeitzone: UTC\n"
        "  - Kennzahl: ADP Weekly Employment Change\n"
        "  - Zeitraum: Aktuelle Berichtsperiode\n"
        "  - Datum: 2026-11-17\n"
        "  - Unter Konsens: Nachlassen des Lohndrucks\n"
        "  - Datum: NICHT VERFÜGBAR (Szenariofeld, kein eigener Termin)\n"
        "  - Quelle: ForexFactory (CACHE) / Makro-Datenpaket\n"
        "  - Quellenprüfung: NICHT VERIFIZIERT (Quelle fehlt)\n"
        "- Ereignis-Uhrzeit: in 2 Tag(en) (2026-11-19)\n"
        "  - Zeitzone: UTC\n"
        "  - Kennzahl: Unemployment Claims (Initial Jobless Claims)\n"
        "  - Zeitraum: Wöchentlich\n"
        "  - Datum: 2026-11-19\n"
        "  - Entsprechend Konsens: Keine Überraschung\n"
        "  - Datum: NICHT VERFÜGBAR (Szenariofeld, kein eigener Termin)\n"
        "  - Quelle: ForexFactory (CACHE) / Makro-Datenpaket\n"
        "  - Quellenprüfung: NICHT VERIFIZIERT (Quelle fehlt)\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, forexfactory_requester=lambda *a, **k: FFResponse()
    )

    assert findings == []
    assert normalized.count("ForexFactory LIVE adapter") == 2
    assert normalized.count("LIVE_VERIFIZIERT") == 2
    assert "Ereignis-Uhrzeit: ADP Weekly Employment Change (2026-11-17)" in normalized
    assert "Ereignis-Uhrzeit: Unemployment Claims (2026-11-19)" in normalized
    assert "Name nicht aufgelöst" not in normalized
    assert "in 2 Tag(en)" not in normalized
    assert normalized.count("Uhrzeit: 08:15") == 1
    assert normalized.count("Uhrzeit: 08:30") == 1
    assert normalized.count("Zeitzone: UTC-05:00") == 2
    assert normalized.count("Abrufzeit=") == 2
    assert "ForexFactory (CACHE)" not in normalized


def test_forexfactory_http_200_wrong_event_or_date_is_not_evidence():
    import json
    from trading_radar_market_first import normalize_event_section

    class FFResponse:
        status_code = 200
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        text = json.dumps([{"id": "x", "country": "USD", "date": "2026-11-18", "title": "CPI"}])
        def json(self):
            return json.loads(self.text)

    report = (
        "3. Ereignisradar\n- Ereignis: ADP Employment Report\n"
        "- Datum: 2026-11-17\n- Quelle: ForexFactory (LIVE)\n"
        "- Quellenprüfung: LIVE_VERIFIZIERT\n\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, forexfactory_requester=lambda *a, **k: FFResponse()
    )
    assert "Ereignis ohne verifizierte Quelle" in findings
    assert "NICHT VERIFIZIERT" in normalized



def test_forexfactory_calendar_url_alone_does_not_verify_event():
    row = normalize_event({
        "name": "ADP Employment Report", "date": "2026-11-17",
        "source": "https://www.forexfactory.com/calendar",
        "source_verification": "LIVE_VERIFIZIERT",
    })
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"



def test_fomc_mixed_decision_and_press_conference_record_is_not_classified():
    from trading_radar_market_first import _fomc_event_kind

    assert _fomc_event_kind("FOMC-Zinsentscheid") == "DECISION"
    assert _fomc_event_kind("FOMC Pressekonferenz") == "PRESS_CONFERENCE"
    assert _fomc_event_kind("FOMC-Zinsentscheid und FOMC Pressekonferenz") is None
    assert _fomc_event_kind("FOMC Minutes und FOMC Pressekonferenz") is None

def test_fomc_diagnostic_without_verifier_is_rejected_even_when_date_is_present():
    from trading_radar_market_first import normalize_event_section

    report = (
        "3. Ereignisradar\n- Ereignis: FOMC-Zinsentscheid\n"
        "- Datum: 2026-12-09\n"
        "- Quelle: Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm\n"
        "- Quellenprüfung: LIVE_VERIFIZIERT — HTTP 200; Datum bestätigt\n\n"
        "4. Assetklassenvergleich\n"
    )
    class BadResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm"
        text = "<html>October 2026; no matching meeting</html>"
    normalized, findings = normalize_event_section(
        report, fomc_requester=lambda *a, **k: BadResponse()
    )
    assert "FOMC-Ereignis nicht live" in " ".join(findings)
    assert "NICHT VERIFIZIERT" in normalized

def test_structured_event_normalizer_rejects_fred_observations_url_as_source():
    row = normalize_event({
        "name": "Initial Jobless Claims",
        "date": "2026-11-19",
        "source": "https://api.stlouisfed.org/fred/series/observations",
    })
    assert row["date"] == "2026-11-19"
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["source"]


def test_structured_event_normalizer_rejects_invalid_explicit_calendar_date():
    row = normalize_event({
        "name": "Initial Jobless Claims",
        "date": "2026-02-30",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
    })
    assert row["date_valid"] is False
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["date"]


def test_generic_untrusted_url_is_not_a_verified_event_source():
    row = normalize_event({
        "name": "CPI",
        "date": "2026-12-10",
        "source": "https://example.invalid/calendar",
    })
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["source"]


def test_event_source_requires_https_calendar_url_not_provider_name():
    from trading_radar_market_first import _is_verified_event_source

    assert _is_verified_event_source("https://www.bls.gov/schedule/news_release/empsit.htm")
    assert _is_verified_event_source("https://www.forexfactory.com/calendar")
    assert not _is_verified_event_source("FRED")
    assert not _is_verified_event_source("Yahoo Finance")
    assert not _is_verified_event_source("Official ADP Calendar")
    assert not _is_verified_event_source("https://evil.example/fred")
    assert not _is_verified_event_source("https://www.bls.gov:invalid/schedule/news_release/empsit.htm")
    assert not _is_verified_event_source("https://user@www.bls.gov/schedule/news_release/empsit.htm")
    assert not _is_verified_event_source("https://api.stlouisfed.org/fred/series/observations")


def test_event_with_explicit_date_but_provider_name_only_remains_incomplete():
    row = normalize_event({
        "name": "Initial Jobless Claims",
        "date": "2026-11-19",
        "source": "FRED",
    })
    assert row["date_valid"] is True
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["source"]


def test_generic_trusted_provider_calendar_source_keeps_existing_policy():
    row = normalize_event({
        "name": "CPI",
        "date": "2026-11-19",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
    })
    assert row["date_valid"] is True
    assert row["source_verified"] is True
    assert row["status"] == "READY"


def test_bare_live_verified_marker_is_not_accepted_as_fetch_evidence():
    row = normalize_event({
        "name": "CPI", "date": "2026-11-19",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "source_verification": "LIVE_VERIFIZIERT",
    })
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"


def test_live_fetch_evidence_must_match_declared_source_and_calendar_path():
    for fetched_url in ("https://evil.example/calendar", "https://www.bls.gov/"):
        row = normalize_event({
            "name": "CPI", "date": "2026-11-19",
            "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
            "source_verification": f"LIVE_VERIFIZIERT — HTTP 200; Abruf: {fetched_url}",
        })
        assert row["source_verified"] is False
        assert row["status"] == "INCOMPLETE"


def test_trusted_calendar_url_without_live_verification_is_not_verified():
    from trading_radar_market_first import normalize_event

    row = normalize_event({
        "name": "ADP Employment Report",
        "date": "2026-11-17",
        "source": "https://www.forexfactory.com/calendar",
    })
    assert row["date_valid"] is True
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["source"]


def test_negative_wrapped_live_verification_marker_is_not_accepted():
    from trading_radar_market_first import _has_live_verification

    assert not _has_live_verification("NICHT LIVE_VERIFIZIERT")
    assert not _has_live_verification("Quelle NICHT VERIFIZIERT (vorher LIVE_VERIFIZIERT)")
    assert not _has_live_verification("Quelle wurde LIVE_VERIFIZIERT erwähnt")
    assert not _has_live_verification(
        "Name nicht aufgelöst (LIVE_VERIFIZIERT) — offizieller Kalender geprüft",
        "ForexFactory (LIVE)", event_name="ADP", event_date="2026-11-17",
    )


def test_event_header_narrative_date_is_not_promoted_to_explicit_event_date():
    from trading_radar_market_first import normalize_event_section

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: Initial Jobless Claims am 2026-11-19\n"
        "- Zeitraum: Wöchentlich\n"
        "- Quelle: https://www.dol.gov/newsroom/releases\n"
        "- Quellenprüfung: LIVE_VERIFIZIERT — HTTP 200; Abruf: https://www.dol.gov/newsroom/releases\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(report)
    assert "Ereignis ohne Datum" in findings
    assert "Datum: NICHT VERFÜGBAR" in normalized
    assert "Initial Jobless Claims am 2026-11-19" in normalized
    assert "Datum: 2026-11-19" not in normalized


@pytest.mark.parametrize(
    "status",
    ["NICHT LIVE_VERIFIZIERT", "Quelle wurde LIVE_VERIFIZIERT erwähnt", "UNVERIFIZIERT"],
)
def test_event_verification_requires_positive_status_token_at_start(status):
    from trading_radar_market_first import normalize_event

    row = normalize_event({
        "name": "ADP Employment Report",
        "date": "2026-11-17",
        "source": "https://www.forexfactory.com/calendar",
        "source_verification": status,
    })
    assert row["source_verified"] is False
    assert row["status"] == "INCOMPLETE"


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker@www.bls.gov/schedule/news_release/empsit.htm",
        "https://www.bls.gov:8443/schedule/news_release/empsit.htm",
        "https://www.bls.gov.evil.example/schedule/news_release/empsit.htm",
    ],
)
def test_event_source_rejects_credential_port_and_lookalike_hosts(url):
    from trading_radar_market_first import _is_verified_event_source

    assert _is_verified_event_source(url) is False


def test_narrative_date_in_event_title_is_not_promoted_to_release_date():
    from trading_radar_market_first import normalize_event

    row = normalize_event({
        "name": "CPI-Veröffentlichung (2026-12-10)",
        "period": "Dezember 2026",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "source_verification": "LIVE_VERIFIZIERT — HTTP 200; Abruf: https://www.bls.gov/schedule/news_release/cpi.htm",
    })
    assert row["date"] is None
    assert row["date_valid"] is False
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["date", "source"]


def test_project_forexfactory_live_stamp_requires_actual_adapter_evidence():
    from trading_radar_market_first import _is_verified_event_source, normalize_event

    assert _is_verified_event_source("ForexFactory (LIVE) / Makro-Datenpaket")
    assert not _is_verified_event_source("ForexFactory")
    assert not _is_verified_event_source("ForexFactory (CACHE)")

    event = normalize_event({
        "name": "ADP Weekly Employment Change",
        "date": "2026-11-17",
        "source": "ForexFactory (LIVE) / Makro-Datenpaket",
        "source_verification": "LIVE_VERIFIZIERT",
    })
    assert event["status"] == "INCOMPLETE"
    assert event["source_verified"] is False



def test_generic_live_evidence_requires_matching_event_date_and_exact_release_url():
    row = normalize_event({
        "name": "CPI-Veröffentlichung", "date": "2026-11-19",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "source_verification": (
            "LIVE_VERIFIZIERT — HTTP 200; Abruf: "
            "https://www.bls.gov/schedule/news_release/cpi.htm; "
            "bestätigt: CPI-Veröffentlichung am 2026-11-19"
        ),
    })
    assert row["source_verified"] is True
    assert row["status"] == "READY"

def test_live_verification_rejects_unrelated_same_host_release_page():
    from trading_radar_market_first import normalize_event

    event = normalize_event({
        "name": "CPI-Veröffentlichung",
        "date": "2026-11-19",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "source_verification": (
            "LIVE_VERIFIZIERT — HTTP 200; Abruf: "
            "https://www.bls.gov/schedule/news_release/empsit.htm"
        ),
    })
    assert event["source_verified"] is False
    assert event["status"] == "INCOMPLETE"
    assert "source" in event["missing_required"]


def test_live_verification_requires_matching_event_and_date_in_fetched_evidence():
    from trading_radar_market_first import normalize_event

    event = normalize_event({
        "name": "CPI-Veröffentlichung",
        "date": "2026-11-19",
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "source_verification": (
            "LIVE_VERIFIZIERT — HTTP 200; Abruf: "
            "https://www.bls.gov/schedule/news_release/cpi.htm; "
            "bestätigt: Employment Situation am 2026-11-06"
        ),
    })
    assert event["source_verified"] is False
    assert event["status"] == "INCOMPLETE"


def test_fomc_month_page_exception_is_bound_to_event_month_and_name():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source, normalize_event

    class FedResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-december.htm?view=calendar"
        text = "<html><title>December 2026</title><tr><td>December 8 - 9</td><td>FOMC Meeting</td><td>2:00 p.m.</td></tr></html>"

    evidence = _verify_fomc_official_source(
        date(2026, 12, 9), requester=lambda *a, **k: FedResponse()
    )
    valid = normalize_event({
        "name": "FOMC-Zinsentscheid", "date": "2026-12-09",
        "source": "Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_verification": "LIVE_VERIFIZIERT — report text is not evidence",
    }, verification_evidence=evidence)
    forged = normalize_event({
        "name": "FOMC-Zinsentscheid", "date": "2026-12-09",
        "source": "Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_verification": "LIVE_VERIFIZIERT — HTTP 200; Abruf: https://www.federalreserve.gov/newsevents/2026-december.htm",
    })
    wrong_event = normalize_event({
        "name": "US-Arbeitsmarktbericht", "date": "2026-12-09",
        "source": "Federal Reserve official calendar https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_verification": "LIVE_VERIFIZIERT",
    }, verification_evidence=evidence)
    assert valid["source_verified"] is True
    assert forged["source_verified"] is False
    assert wrong_event["source_verified"] is False


def test_event_section_downgrades_same_host_but_wrong_release_page():
    from trading_radar_market_first import normalize_event_section

    report = """3. Ereignisradar
- Ereignis: CPI-Veröffentlichung
- Datum: 2026-11-19
- Quelle: https://www.bls.gov/schedule/news_release/cpi.htm
- Quellenprüfung: LIVE_VERIFIZIERT — HTTP 200; Abruf: https://www.bls.gov/schedule/news_release/empsit.htm

4. Assetklassenvergleich
Relative Stärke.
"""
    normalized, findings = normalize_event_section(report)
    event_section = normalized.split("3. Ereignisradar", 1)[1].split("4. Assetklassenvergleich", 1)[0]
    assert "Ereignis ohne verifizierte Quelle" in findings
    assert "Quellenprüfung: NICHT VERIFIZIERT" in event_section
    assert "LIVE_VERIFIZIERT" not in event_section


def test_caller_constructed_evidence_object_is_not_accepted_as_live_proof():
    from trading_radar_market_first import _EventVerificationEvidence, normalize_event

    forged = _EventVerificationEvidence(
        kind="FOMC", event_id="FOMC_DECISION", event_name="FOMC Zinsentscheid",
        event_date="2026-12-09", event_time="14:00", timezone="America/New_York",
        requested_url="https://www.federalreserve.gov/newsevents/2026-december.htm",
        final_url="https://www.federalreserve.gov/newsevents/2026-december.htm",
        http_status=200, retrieved_at="2026-11-16T10:00:00+00:00",
        content_match=True, adapter="federal_reserve_month_calendar_verifier",
        response_sha256="not-a-real-response-hash", verified=True, detail="forged",
    )
    event = normalize_event({
        "name": "FOMC-Zinsentscheid", "date": "2026-12-09",
        "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "source_verification": "LIVE_VERIFIZIERT",
    }, verification_evidence=forged)
    assert event["source_verified"] is False
    assert event["status"] == "INCOMPLETE"


def test_generic_bls_live_verification_still_accepts_matching_real_fetch_evidence():
    """The ForexFactory/FOMC adapter changes must not disable other providers."""
    source = "https://www.bls.gov/schedule/news_release/cpi.htm"
    row = normalize_event({
        "name": "CPI", "date": "2026-11-19", "source": source,
        "source_verification": (
            "LIVE_VERIFIZIERT — HTTP 200; Abruf: " + source
            + "; Event: CPI am 2026-11-19"
        ),
    })
    assert row["source_verified"] is True
    assert row["status"] == "READY"


def test_fomc_event_identity_classifier_does_not_prefer_decision_when_ambiguous():
    from trading_radar_market_first import _fomc_event_kind

    assert _fomc_event_kind("FOMC-Zinsentscheid") == "DECISION"
    assert _fomc_event_kind("FOMC Pressekonferenz") == "PRESS_CONFERENCE"
    assert _fomc_event_kind("FOMC-Zinsentscheid und FOMC Pressekonferenz") is None



def test_fomc_press_conference_remains_same_day_at_1415_new_york_time(tmp_path):
    from trading_radar_market_first import _enrich_fomc_event
    import json

    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28", "2026-12-09"]}), encoding="utf-8")
    report = "Datenstand: 2026-10-28T18:15:00+00:00"  # 14:15 EDT
    body = "- Ereignis: FOMC Pressekonferenz"
    requested = []

    class Response:
        status_code = 200
        text = "<table><tr><td>October 27-28, 2026</td><td>FOMC meeting</td><td>2:00 p.m. Press Conference 2:30 p.m.</td></tr></table>"

        @property
        def url(self):
            return requested[-1]

    def requester(url, **kwargs):
        requested.append(url)
        return Response()

    result = _enrich_fomc_event(body, report, calendar, source_requester=requester)
    assert requested and "2026-october.htm" in requested[0]
    assert "Event-ID: FOMC_PRESS_CONFERENCE" in result
    assert "Datum: 2026-10-28" in result
    assert "Uhrzeit: 14:30" in result


@pytest.mark.parametrize(
    "cutoff_utc",
    ["2026-10-28T18:30:00+00:00", "2026-10-28T18:31:00+00:00"],
)
def test_fomc_press_conference_moves_to_next_meeting_at_or_after_1430_new_york_time(
    tmp_path, cutoff_utc
):
    from trading_radar_market_first import _enrich_fomc_event
    import json

    calendar = tmp_path / "fomc_termine.json"
    calendar.write_text(json.dumps({"termine": ["2026-10-28", "2026-12-09"]}), encoding="utf-8")
    report = f"Datenstand: {cutoff_utc}"  # 14:30 / 14:31 EDT
    body = "- Ereignis: FOMC Pressekonferenz"
    requested = []

    class Response:
        status_code = 200
        text = "<table><tr><td>December 8-9, 2026</td><td>FOMC meeting</td><td>2:00 p.m. Press Conference 2:30 p.m.</td></tr></table>"

        @property
        def url(self):
            return requested[-1]

    def requester(url, **kwargs):
        requested.append(url)
        return Response()

    result = _enrich_fomc_event(body, report, calendar, source_requester=requester)
    assert requested and "2026-december.htm" in requested[0]
    assert "Datum: 2026-12-09" in result


def test_fomc_press_row_day_30_is_not_matched_by_230pm_minute_digits():
    from datetime import date
    from trading_radar_market_first import _verify_fomc_official_source

    class Response:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-october.htm?view=calendar"
        text = """<html><title>October 2026</title><table>
        <tr><td>October 29-30, 2026</td><td>FOMC Meeting</td><td>2:00 p.m.</td></tr>
        <tr><td>2</td><td>FOMC Press Conference</td><td>2:30 p.m.</td></tr>
        </table></html>"""

    result = _verify_fomc_official_source(
        date(2026, 10, 30), requester=lambda *a, **k: Response(),
        event_kind="PRESS_CONFERENCE",
    )
    assert not result.verified


def test_combined_event_time_zone_label_is_a_field_not_a_new_event_record():
    from trading_radar_market_first import normalize_event_section
    report = """3. Ereignisradar
- Ereignis: ADP Weekly Employment Change
  - Datum: 2026-11-17
  - Quelle: ADP official calendar
  - Uhrzeit/Zeitzone: 08:15 America/New_York
  - Kennzahl: ADP Beschäftigungsänderung
  - Berichtszeitraum: November 2026
  - Letzter Wert: 42.000
  - Konsens: 35.000

4. Assetklassenvergleich
"""
    normalized, findings = normalize_event_section(report)
    assert findings == []
    assert normalized.count("Ereignis: ADP Weekly Employment Change") == 1
    assert normalized.count("Uhrzeit/Zeitzone:") == 1
    assert "4. Assetklassenvergleich" in normalized


def test_combined_event_time_zone_unavailable_is_preserved_as_one_field():
    from trading_radar_market_first import normalize_event_section
    report = """3. Ereignisradar
- Ereignis: ADP Weekly Employment Change
  - Datum: 2026-11-17
  - Quelle: ADP official calendar
  - Uhrzeit/Zeitzone: NICHT VERFÜGBAR
  - Kennzahl: ADP Beschäftigungsänderung
  - Berichtszeitraum: November 2026
  - Letzter Wert: NICHT VERFÜGBAR
  - Konsens: NICHT VERFÜGBAR

4. Assetklassenvergleich
"""
    normalized, findings = normalize_event_section(report)
    assert findings == []
    assert normalized.count("Ereignis: ADP Weekly Employment Change") == 1
    assert normalized.count("Uhrzeit/Zeitzone:") == 1


def test_fed_waller_speaker_adapter_verifies_official_calendar_and_utc_time():
    from datetime import date
    from trading_radar_market_first import (
        _is_trusted_event_evidence, _verify_fed_waller_speaker_event,
    )

    url = "https://www.federalreserve.gov/newsevents/2026-November.htm"
    html = """
    <html><body>
      <h3>November 2026</h3>
      <div>8:25 a.m.</div>
      <div>Speech - Governor Christopher J. Waller</div>
      <div>Welcoming Remarks</div>
      <div>At the College Fed Challenge Finals</div>
      <div>20</div>
    </body></html>
    """

    class Response:
        status_code = 200
        def __init__(self, requested_url):
            self.url = requested_url
            self.text = html

    evidence, detail = _verify_fed_waller_speaker_event(
        "FOMC Member Waller Speaks", requester=lambda requested_url, **k: Response(requested_url),
        today=date(2026, 11, 20),
    )
    assert evidence is not None
    assert evidence.event_date == "2026-11-20"
    assert evidence.event_time == "13:25"
    assert evidence.timezone == "UTC"
    assert _is_trusted_event_evidence(
        evidence, kind="FED_SPEAKER",
        event_name="FOMC Member Waller Speaks", event_date="2026-11-20",
    )
    assert "08:25 America/New_York" in detail
    assert "13:25 UTC" in detail


def test_event_normalizer_enriches_undated_waller_only_from_official_fed_calendar():
    from datetime import date
    from trading_radar_market_first import normalize_event_section

    url = "https://www.federalreserve.gov/newsevents/2026-November.htm"
    html = """
    <html><body>
      <div>8:25 a.m.</div>
      <div>Speech - Governor Christopher J. Waller</div>
      <div>Welcoming Remarks</div>
      <div>At the College Fed Challenge Finals</div>
      <div>20</div>
    </body></html>
    """

    class Response:
        status_code = 200
        def __init__(self, requested_url):
            self.url = requested_url
            self.text = html

    report = """3. Ereignisradar
- Ereignis-Uhrzeit: HEUTE | Zeitzone: UTC | Kennzahl: FOMC Member Waller Speaks | Zeitraum: Aktuell | Letzter Wert: NICHT VERFÜGBAR | Konsens: NICHT VERFÜGBAR | Quelle: ForexFactory
- Betroffene These/Assetklasse/konkrete offene Positionen: Zinsmärkte
- Datum: NICHT VERFÜGBAR
- Quellenprüfung: NICHT VERIFIZIERT
4. Assetklassenvergleich
"""
    normalized, findings = normalize_event_section(
        report,
        fed_speaker_requester=lambda requested_url, **kwargs: Response(requested_url),
        today=date(2026, 11, 20),
    )
    assert not findings
    assert "Datum: 2026-11-20" in normalized
    assert "Federal Reserve official calendar" in normalized
    assert "Uhrzeit: 13:25" in normalized
    assert "Zeitzone: UTC" in normalized
    assert "Kennzahl: FOMC Member Waller Speaks" in normalized
    assert "Quelle: ForexFactory" not in normalized
    assert "Quellenprüfung: LIVE_VERIFIZIERT" in normalized


def test_live_snapshot_replaces_only_attached_vix_dxy_values_and_preserves_neighbor_numbers():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot
    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    index = pd.date_range("2026-06-01", periods=80, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    values = {col: [100.0 + i * 0.2 for i in range(len(index))] for col in columns}
    values[("^VIX", "Close")] = [18.0 + i * 0.01 for i in range(len(index))]
    values[("DX-Y.NYB", "Close")] = [100.0 + i * 0.02 for i in range(len(index))]
    data = pd.DataFrame(values, index=index)
    report = ("### 1. Trading-Radar\nMarktregime: konstruktiv.\nIndizes/Marktbreite: S&P stabil.\n"
              "Dollar/Liquidität/Volatilität: USD-Liquidität stabil; VIX NICHT VERFÜGBAR; Liquiditätsindikator 42; DXY 99,90; Treasury-Narrativ 4,5%.\n"
              "\n### 2. Thesen-Update\n")
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert not findings
    risk_line = next(line for line in result.splitlines() if line.startswith("Dollar/Liquidität/Volatilität:"))
    assert risk_line.count("VIX:") == 1 and risk_line.count("DXY:") == 1
    assert "Liquiditätsindikator 42" in risk_line and "Treasury-Narrativ 4,5%" in risk_line
    assert "NICHT VERFÜGBAR" not in risk_line
    assert sum("- Volatilität VIX:" in line for line in result.splitlines()) == 1
    assert sum("- Dollar-Proxy DXY" in line for line in result.splitlines()) == 1
    second_pass, second_findings = append_live_market_snapshot(
        result, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    assert not second_findings
    second_risk_line = next(line for line in second_pass.splitlines() if line.startswith("Dollar/Liquidität/Volatilität:"))
    assert second_risk_line == risk_line


def test_live_snapshot_repetition_preserves_all_scenarios_and_eight_sections():
    import pandas as pd
    import re
    from trading_radar_market_first import append_live_market_snapshot

    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE",
        "XLU", "XLV", "XLY",
    ]
    index = pd.date_range("2026-06-01", periods=80, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    values = {col: [100.0 + i * 0.2 for i in range(len(index))] for col in columns}
    values[("^VIX", "Close")] = [18.0 + i * 0.01 for i in range(len(index))]
    values[("DX-Y.NYB", "Close")] = [100.0 + i * 0.02 for i in range(len(index))]
    data = pd.DataFrame(values, index=index)

    report = (
        "1. Trading-Radar\n"
        "Marktregime: konstruktiv.\n"
        "Indizes/Marktbreite: Marktbreite 8/11.\n"
        "Zinsen/Renditen: US 10Y Treasury bei 4,00%.\n"
        "Dollar/Liquidität/Volatilität: VIX: 18,00; DXY: 100,00.\n"
        "**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**\n"
        "- Altbestand, darf bei der Migration ersetzt werden.\n"
        "Basisszenario: BASISSZENARIO_DAUERHAFT_ERHALTEN.\n"
        "Positives Alternativszenario: POSITIVES_ALTERNATIVSZENARIO_DAUERHAFT_ERHALTEN.\n"
        "Negatives Alternativszenario: NEGATIVES_ALTERNATIVSZENARIO_DAUERHAFT_ERHALTEN.\n\n"
        "2. Thesen-Update\nThesen-ID: T-1 | Version: 1 | Status: BESTÄTIGT.\n\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n\n"
        "4. Assetklassenvergleich\nRelative-Stärke-Matrix folgt.\n\n"
        "5. Technische Kandidaten\nKeine Kandidaten.\n\n"
        "6. Portfolio & Risiko\nKeine offenen Positionen.\n\n"
        "7. MSCI-Vergleich\nBenchmarkdaten.\n\n"
        "8. Daten-/Prüfanhang\nPrüfprotokoll.\n"
    )

    first, first_findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )
    second, second_findings = append_live_market_snapshot(
        first, fetcher=lambda requested: data,
        as_of_time=f"{index[-1].date()}T23:00:00+00:00",
    )

    assert not first_findings and not second_findings
    for marker in (
        "BASISSZENARIO_DAUERHAFT_ERHALTEN",
        "POSITIVES_ALTERNATIVSZENARIO_DAUERHAFT_ERHALTEN",
        "NEGATIVES_ALTERNATIVSZENARIO_DAUERHAFT_ERHALTEN",
    ):
        assert first.count(marker) == 1
        assert second.count(marker) == 1
    assert second.count("TRADING_RADAR_LIVE_MARKET_SNAPSHOT_START") == 1
    assert second.count("TRADING_RADAR_LIVE_MARKET_SNAPSHOT_END") == 1
    assert second.count("**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**") == 1
    for number, title in enumerate(
        (
            "Trading-Radar", "Thesen-Update", "Ereignisradar", "Assetklassenvergleich",
            "Technische Kandidaten", "Portfolio & Risiko", "MSCI-Vergleich", "Daten-/Prüfanhang",
        ),
        start=1,
    ):
        assert re.search(rf"(?m)^{number}\. {re.escape(title)}$", second)


def test_pipe_delimited_forexfactory_event_header_is_normalized_and_live_verified():
    from trading_radar_market_first import normalize_event_section

    class Response:
        status_code = 200
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        text = json.dumps([{
            "id": "adp-weekly-20261124",
            "country": "USD",
            "title": "ADP Weekly Employment Change",
            "date": "2026-11-24T14:15:00+01:00",
        }])

        def json(self):
            return json.loads(self.text)

    report = (
        "3. Ereignisradar\n"
        "* ADP Weekly Employment Change | 2026-11-24 | 14:15 MEZ | "
        "Erwartung: NICHT VERFÜGBAR | Quelle: ForexFactory.\n"
        "- Datum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)\n"
        "- Quellenprüfung: NICHT VERIFIZIERT (Datum nicht belegt; Quelle fehlt)\n"
        "Uhrzeit: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)\n"
        "Zeitzone: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)\n"
        "Kennzahl: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)\n"
        "Berichtszeitraum: NICHT VERFÜGBAR (keine verifizierte Ereignisquelle)\n"
        "* Auswirkung: Volatilität bei Zins-Assets; bestätigt die These bei hoher Arbeitsmarktaktivität.\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report,
        forexfactory_requester=lambda requested_url, **kwargs: Response(),
    )

    assert findings == []
    assert normalized.count("ADP Weekly Employment Change") >= 1
    assert "Datum: 2026-11-24" in normalized
    assert "Uhrzeit: 14:15" in normalized
    assert "Zeitzone: UTC+01:00" in normalized
    assert "ForexFactory LIVE adapter" in normalized
    assert "LIVE_VERIFIZIERT" in normalized
    assert "Auswirkung:" in normalized
    assert "Ereignis ohne Datum" not in findings


def test_live_snapshot_uses_core_market_session_not_weekend_crypto_and_falls_back_to_uup():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "DX=F", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
    ]
    business_dates = pd.date_range(end="2026-11-23", periods=80, freq="B", tz="UTC")
    sunday = pd.DatetimeIndex([pd.Timestamp("2026-11-22", tz="UTC")])
    index = business_dates.union(sunday).sort_values()
    columns = pd.MultiIndex.from_product([symbols, ["Close"]])
    values = {}
    for symbol in symbols:
        if symbol == "BTC-USD":
            series = pd.Series(float("nan"), index=index)
            series.loc[business_dates[-3]] = 90.0
            series.loc[sunday[0]] = 91.0
        else:
            series = pd.Series(float("nan"), index=index)
            series.loc[business_dates] = [100.0 + i * 0.25 for i in range(len(business_dates))]
        if symbol == "^VIX":
            series.loc[business_dates] = [18.0 + i * 0.01 for i in range(len(business_dates))]
        if symbol in {"DX-Y.NYB", "DX=F"}:
            # Both index feeds stop on Friday; UUP is the only exact Monday proxy.
            series.loc[business_dates[-1]] = float("nan")
        if symbol == "UUP":
            series.loc[business_dates] = [25.0 + i * 0.01 for i in range(len(business_dates))]
        values[(symbol, "Close")] = series.to_numpy()
    data = pd.DataFrame(values, index=index, columns=columns)
    report = (
        "1. Trading-Radar\nMarktregime: konstruktiv.\n"
        "Indizes/Marktbreite: Marktbreite 8/11.\n"
        "Zinsen/Renditen: US 10Y Treasury bei 4,00%.\n"
        "Dollar/Liquidität/Volatilität: VIX NICHT VERFÜGBAR; DXY NICHT VERFÜGBAR; Liquiditätslage stabil.\n\n"
        "2. Thesen-Update\n\n3. Ereignisradar\nKeine relevanten Ereignisse.\n"
        "4. Assetklassenvergleich\n\n5. Technische Kandidaten\n"
    )
    result, findings = append_live_market_snapshot(
        report, fetcher=lambda requested: data,
        as_of_time="2026-11-24T05:30:00+00:00",
    )

    assert findings == []
    assert "Gemeinsamer Datenstichtag: 2026-11-23T00:00:00+00:00" in result
    assert "- Volatilität VIX:" in result and "Datenstichtag: 2026-11-23" in result
    assert "- Dollar-Proxy UUP (US-Dollar-ETF):" in result
    assert "Quelle: Yahoo Finance (UUP)" in result
    assert "Dollarproxy:" in next(
        line for line in result.splitlines() if line.startswith("Dollar/Liquidität/Volatilität:")
    )
    assert "Datenlücke: kein Kurs exakt am gemeinsamen Stichtag" in result


def test_pipe_delimited_event_header_does_not_claim_live_verification_without_feed_match():
    import json
    from trading_radar_market_first import normalize_event_section

    class Response:
        status_code = 200
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        text = "[]"

        def json(self):
            return json.loads(self.text)

    report = (
        "3. Ereignisradar\n"
        "* ADP Weekly Employment Change | 2026-11-24 | 14:15 MEZ | Quelle: ForexFactory.\n"
        "- Datum: NICHT VERFÜGBAR\n"
        "- Quellenprüfung: NICHT VERIFIZIERT (Datum nicht belegt; Quelle fehlt)\n"
        "Uhrzeit: NICHT VERFÜGBAR\n"
        "Zeitzone: NICHT VERFÜGBAR\n"
        "Kennzahl: NICHT VERFÜGBAR\n"
        "Berichtszeitraum: NICHT VERFÜGBAR\n"
        "* Auswirkung: Volatilität bei Zins-Assets.\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, forexfactory_requester=lambda requested_url, **kwargs: Response()
    )

    assert "Datum: 2026-11-24" in normalized
    assert "Uhrzeit: 14:15" in normalized
    assert "Zeitzone: Europe/Berlin" in normalized
    assert "LIVE_VERIFIZIERT" not in normalized
    assert any("Ereignis ohne verifizierte Quelle" in item for item in findings)


def test_us_prefixed_forexfactory_events_bind_live_values_and_payload_provenance():
    from trading_radar_market_first import normalize_event_section

    class FFResponse:
        status_code = 200
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

        def __init__(self):
            self.text = json.dumps([
                {
                    "id": "eur-decoy",
                    "country": "EUR",
                    "date": "2026-11-25T08:30:00-05:00",
                    "title": "Prelim GDP q/q",
                    "previous": "9.9%",
                    "forecast": "9.9%",
                },
                {
                    "id": "usd-gdp",
                    "country": "USD",
                    "date": "2026-11-25T08:30:00-05:00",
                    "title": "Prelim GDP q/q",
                    "period": "Q3 2026",
                    "previous": "3.4%",
                    "forecast": "3.5%",
                    "actual": "4.3%",
                },
            ])

        def json(self):
            return json.loads(self.text)

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: US Prelim GDP q/q (Q3 2026)\n"
        "- Datum: 2026-11-25\n"
        "- Ereignis-Uhrzeit: NICHT VERFÜGBAR\n"
        "- Uhrzeit: NICHT VERFÜGBAR\n"
        "- Zeitzone: NICHT VERFÜGBAR\n"
        "- Kennzahl: US Bruttoinlandsprodukt\n"
        "- Berichtszeitraum: HEUTE (2026-11-25)\n"
        "- Letzter Wert: 1.5% (Q2 2026)\n"
        "- Konsens: NICHT VERFÜGBAR\n"
        "- Quelle: ForexFactory LIVE\n"
        "- Istwert: NICHT VERFÜGBAR\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, forexfactory_requester=lambda *_a, **_k: FFResponse()
    )

    assert findings == []
    assert "Uhrzeit: 08:30" in normalized
    assert "Zeitzone: UTC-05:00" in normalized
    assert "Berichtszeitraum: Q3 2026" in normalized
    assert "Letzter Wert: 3.4%" in normalized
    assert "Konsens: 3.5%" in normalized
    assert "Istwert: 4.3%" in normalized
    assert "Wert-Provenienz: ForexFactory JSON" in normalized
    assert "Payload-SHA256=" in normalized
    assert "ForexFactory LIVE adapter" in normalized


def test_ecb_non_monetary_council_meeting_is_verified_without_inventing_a_clock():
    from trading_radar_market_first import normalize_event_section

    class ECBResponse:
        status_code = 200
        url = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
        text = (
            "<html><body><div>25/11/2026</div>"
            "<div>Governing Council of the ECB: non-monetary policy meeting (in Frankfurt)</div>"
            "</body></html>"
        )

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: EZB-Rat Nicht-geldpolitische Sitzung\n"
        "- Datum: 2026-11-25\n"
        "- Ereignis-Uhrzeit: NICHT VERFÜGBAR\n"
        "- Uhrzeit: NICHT VERFÜGBAR\n"
        "- Zeitzone: UTC / MEZ\n"
        "- Kennzahl: Nicht-geldpolitische Sitzung des EZB-Rats\n"
        "- Berichtszeitraum: HEUTE (2026-11-25)\n"
        "- Letzter Wert: NICHT VERFÜGBAR\n"
        "- Konsens: NICHT VERFÜGBAR\n"
        "- Quelle: ECB official calendar\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, ecb_requester=lambda *_a, **_k: ECBResponse()
    )

    assert findings == []
    assert "ECB official calendar (https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html)" in normalized
    assert "Zeitzone: Europe/Berlin" in normalized
    assert "Berichtszeitraum: NICHT VERFÜGBAR (Sitzungstermin; kein statistischer Berichtszeitraum)" in normalized
    assert "Uhrzeit: NICHT VERFÜGBAR" in normalized
    assert "LIVE_VERIFIZIERT" in normalized
    assert "Payload-SHA256=" in normalized


def test_ecb_event_is_not_verified_when_date_and_event_are_not_in_same_calendar_context():
    from trading_radar_market_first import _verify_ecb_governing_council_event

    class ECBResponse:
        status_code = 200
        url = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
        text = (
            "<html><body><div>25/11/2026</div><div>Other meeting</div>"
            "<div>26/11/2026</div>"
            "<div>Governing Council of the ECB: non-monetary policy meeting (in Frankfurt)</div>"
            "</body></html>"
        )

    evidence, detail = _verify_ecb_governing_council_event(
        "EZB-Rat Nicht-geldpolitische Sitzung",
        "2026-11-25",
        requester=lambda *_a, **_k: ECBResponse(),
    )
    assert evidence is None
    assert "0 exakt datums- und ereignisgebundene Treffer" in detail


def test_fomc_calendar_does_not_masquerade_as_a_live_rate_or_futures_consensus_source():
    from trading_radar_market_first import normalize_event_section

    class FedResponse:
        status_code = 200
        url = "https://www.federalreserve.gov/newsevents/2026-december.htm"
        text = (
            "<html><body><table><tr><td>December 8-9, 2026</td>"
            "<td>FOMC Meeting</td><td>2:00 p.m.</td></tr></table></body></html>"
        )

    report = (
        "3. Ereignisradar\n"
        "- Ereignis: FOMC Leitzinsentscheidung (Fed)\n"
        "- Datum: 2026-12-09\n"
        "- Ereignis-Uhrzeit: NICHT VERFÜGBAR\n"
        "- Uhrzeit: NICHT VERFÜGBAR\n"
        "- Zeitzone: NICHT VERFÜGBAR\n"
        "- Kennzahl: Fed Target Range\n"
        "- Zeitraum: 2026-12-09\n"
        "- Letzter Wert: 3.75%-4.00% (EFFR 3.88%)\n"
        "- Konsens: Hold 66%, Hike 34% (Impliziert aus Futures ZQ)\n"
        "- Quelle: Federal Reserve / FOMC Termine\n"
        "\n4. Assetklassenvergleich\n"
    )
    normalized, findings = normalize_event_section(
        report, fomc_requester=lambda *_a, **_k: FedResponse()
    )
    assert findings == []
    assert "Letzter Wert: NICHT VERFÜGBAR (Fed-Kalender bestätigt keinen vorherigen Zielkorridor)" in normalized
    assert "Konsens: NICHT VERFÜGBAR (keine separat live verifizierte Futures-Quelle)" in normalized
    assert "Unverifizierter Vorwert: 3.75%-4.00% (EFFR 3.88%)" in normalized
    assert "Unverifizierte Markterwartung: Hold 66%, Hike 34% (Impliziert aus Futures ZQ)" in normalized
