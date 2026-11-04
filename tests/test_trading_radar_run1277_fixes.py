from decimal import Decimal

import Trading_Radar_Alleinauswertung as production
from trading_radar_report_gate import extract_us10y_observations


def test_m02_recognizes_production_us_treasury_zins_wording():
    line = "Zinsen/Renditen: 10-jähriger US-Treasury-Zins bei 4,98%."
    assert extract_us10y_observations(line) == [Decimal("4.98")]


def test_m07_fills_existing_unavailable_macro_provenance_on_conflict_row():
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-29 | STATUS=REAL_CACHED | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-04T08:00:00+00:00 | DELAY_DAYS=6 | UNIT=%"
    )
    report = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.9800% (Makro-Provenienz: "
        "Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR) | "
        "Live-Wert: 4.9520% (Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-04T11:40:06+00:00; "
        "Datenstichtag: 2026-11-03T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst; keine der Beobachtungen wird überschrieben."
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(
        report, macro, {"Makro_Briefing(2026-11-04).txt": "/tmp/Makro_Briefing(2026-11-04).txt"}
    )
    assert "Makro-Provenienz: Quelle=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "Abrufzeit=2026-11-04T08:00:00+00:00" in result
    assert "Datenstichtag=2026-10-29" in result
    assert "Einheit=%" in result
    assert "Verzögerung=6 Kalendertage" in result
    # The separate live observation is retained byte-for-byte; no provenance
    # crosses from FRED to Yahoo or vice versa.
    assert "Live-Wert: 4.9520% (Quelle: Yahoo Finance (^TNX); " in result
    assert "Abrufzeit: 2026-11-04T11:40:06+00:00" in result
    assert "Verzögerung: Tages-Schlusskurs" in result
    assert "Makro-Provenienz: Quelle: NICHT VERFÜGBAR" not in result


def test_m07_does_not_overwrite_explicit_macro_conflict_fields():
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-29 | STATUS=REAL_CACHED | "
        "SOURCE=FRED DGS10 | RETRIEVED_AT=2026-11-04T08:00:00+00:00 | "
        "DELAY_DAYS=6 | UNIT=%"
    )
    report = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.9800% (Makro-Provenienz: Quelle: ANDERE QUELLE; "
        "Abrufzeit: 2026-11-04T07:00:00+00:00; Datenstichtag: 2026-10-29; "
        "Einheit: %; Verzögerung: 6 Kalendertage) | "
        "Live-Wert: 4.9520% (Quelle: Yahoo Finance ^TNX)"
    )
    result = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    assert "Quelle=ANDERE QUELLE" in result
    assert "Abrufzeit=2026-11-04T07:00:00+00:00" in result
    assert "Quelle=FRED DGS10" not in result


def test_m02_and_m07_pass_together_for_run1277_observation_shapes():
    import importlib.util
    from pathlib import Path
    from trading_radar_report_gate import validate_report

    spec = importlib.util.spec_from_file_location(
        "run1277_report_gate_fixture", Path(__file__).with_name("test_trading_radar_report_gate.py")
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    report = fixture.report_fixture().replace(
        "Zinsen/Renditen: US 10Y Treasury bei 5,00% | Quelle: FRED DGS10 | "
        "Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | "
        "Einheit: % | Verzögerung: 1 Börsentag.",
        "Zinsen/Renditen: 10-jähriger US-Treasury-Zins bei 4,98%.",
    )
    conflict = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.9800% (Makro-Provenienz: Quelle: NICHT VERFÜGBAR; "
        "Abrufzeit: NICHT VERFÜGBAR; Datenstichtag: NICHT VERFÜGBAR; "
        "Einheit: NICHT VERFÜGBAR; Verzögerung: NICHT VERFÜGBAR) | "
        "Live-Wert: 4.9520% (Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-04T11:40:06+00:00; "
        "Datenstichtag: 2026-11-03T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst; keine der Beobachtungen wird überschrieben."
    )
    report = report.replace("Dollar/Liquidität/Volatilität:", conflict + "\nDollar/Liquidität/Volatilität:", 1)
    macro = (
        "US 10Y Treasury: 4.9800 | Datenstand=2026-10-29 | STATUS=REAL_CACHED | "
        "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations) | "
        "RETRIEVED_AT=2026-11-04T08:00:00+00:00 | DELAY_DAYS=6 | UNIT=%"
    )
    report = production._trading_radar_treasury_provenienz_ergänzen(report, macro, {})
    errors = validate_report(report)
    assert not any("M-02: US-10Y-Rendite" in item for item in errors), errors
    assert not any("M-07: Jede abweichende 10Y-Treasury-Beobachtung" in item for item in errors), errors


def test_fred_snapshot_identifies_dgs10_when_cache_source_is_only_api_url(monkeypatch):
    import datetime as dt
    import sys
    import types
    import pandas as pd

    sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame()))
    import makro_szenario as macro_module
    saved_at = dt.datetime(2026, 11, 4, 8, 0, tzinfo=dt.timezone.utc).timestamp()
    monkeypatch.setattr(
        macro_module, "fred_series",
        lambda series_id: pd.DataFrame({"DATE": [pd.Timestamp("2026-10-29")], series_id: [4.98]}),
    )
    monkeypatch.setattr(
        macro_module, "_cache_load",
        lambda: {"fred": {"DGS10": {
            "saved_at": saved_at,
            "source": "https://api.stlouisfed.org/fred/series/observations",
            "status": "REAL",
        }}},
    )
    result = macro_module.fred_snapshot("US 10Y Treasury", "DGS10")
    assert "SOURCE=FRED DGS10 (https://api.stlouisfed.org/fred/series/observations)" in result
    assert "RETRIEVED_AT=2026-11-04T08:00:00+00:00" in result
    assert "Datenstand=2026-10-29" in result
    assert "UNIT=%" in result


def test_treasury_gov_fallback_is_not_mislabelled_as_fred(monkeypatch):
    import datetime as dt
    import sys
    import types
    import pandas as pd

    sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame()))
    import makro_szenario as macro_module
    saved_at = dt.datetime(2026, 11, 4, 8, 0, tzinfo=dt.timezone.utc).timestamp()
    source = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
    monkeypatch.setattr(
        macro_module, "fred_series",
        lambda series_id: pd.DataFrame({"DATE": [pd.Timestamp("2026-10-29")], series_id: [4.98]}),
    )
    monkeypatch.setattr(
        macro_module, "_cache_load",
        lambda: {"fred": {"DGS10": {"saved_at": saved_at, "source": source, "status": "REAL"}}},
    )
    result = macro_module.fred_snapshot("US 10Y Treasury", "DGS10")
    assert "SOURCE=U.S. Treasury 10Y yield (" + source + ")" in result
    assert "SOURCE=FRED DGS10" not in result


def test_federal_reserve_h15_fallback_keeps_its_own_provider_identity(monkeypatch):
    import datetime as dt
    import sys
    import types
    import pandas as pd
    from trading_radar_report_gate import _treasury_observation_has_complete_provenance

    sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame()))
    import makro_szenario as macro_module
    saved_at = dt.datetime(2026, 11, 4, 8, 0, tzinfo=dt.timezone.utc).timestamp()
    source = "https://www.federalreserve.gov/releases/h15/current/"
    monkeypatch.setattr(
        macro_module, "fred_series",
        lambda series_id: pd.DataFrame({"DATE": [pd.Timestamp("2026-10-29")], series_id: [4.98]}),
    )
    monkeypatch.setattr(
        macro_module, "_cache_load",
        lambda: {"fred": {"DGS10": {"saved_at": saved_at, "source": source, "status": "REAL"}}},
    )
    result = macro_module.fred_snapshot("US 10Y Treasury", "DGS10")
    assert "SOURCE=Federal Reserve Board H.15 10Y Treasury yield (" + source + ")" in result
    assert "SOURCE=FRED DGS10" not in result
    line = (
        "US 10Y Treasury 4.98% (Quelle: Federal Reserve Board H.15 10Y Treasury yield; "
        "Abrufzeit: 2026-11-04T08:00:00+00:00; Datenstichtag: 2026-10-29; "
        "Einheit: %; Verzögerung: 6 Kalendertage)"
    )
    assert _treasury_observation_has_complete_provenance(line)


def test_dgs10_cache_without_saved_retrieval_metadata_is_refreshed(monkeypatch, tmp_path):
    import json
    import sys
    import types
    import pandas as pd

    sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame()))
    import makro_szenario as macro_module
    cache_dir = tmp_path / ".macro_cache"
    cache_file = cache_dir / "macro_cache.json"
    cache_dir.mkdir()
    stale_payload = pd.DataFrame({
        "DATE": [pd.Timestamp("2026-10-29")], "DGS10": [4.98]
    }).to_json(orient="split", date_format="iso")
    cache_file.write_text(json.dumps({
        "version": macro_module.CACHE_VERSION,
        "fred": {"DGS10": {"payload": stale_payload, "status": "REAL"}},
    }), encoding="utf-8")
    monkeypatch.setattr(macro_module, "MACRO_CACHE_DIR", cache_dir)
    monkeypatch.setattr(macro_module, "MACRO_CACHE_FILE", cache_file)
    monkeypatch.setattr(macro_module, "_cache_valid", lambda *a, **k: True)
    monkeypatch.setattr(macro_module, "_fred_cache_requires_release_refresh", lambda *a, **k: False)
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"observations": [
                {"date": "2026-11-03", "value": "4.97"},
                {"date": "2026-11-04", "value": "4.96"},
            ]}

    calls = []
    monkeypatch.setattr(
        macro_module.requests, "get",
        lambda *a, **k: (calls.append((a, k)) or FakeResponse()),
    )
    result = macro_module.fred_series("DGS10")
    assert len(calls) == 1
    assert result.iloc[-1]["DGS10"] == 4.96
    refreshed = json.loads(cache_file.read_text(encoding="utf-8"))["fred"]["DGS10"]
    assert isinstance(refreshed.get("saved_at"), (int, float))
    assert refreshed.get("source") == "https://api.stlouisfed.org/fred/series/observations"
