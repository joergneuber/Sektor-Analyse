# -*- coding: utf-8 -*-
"""Regression tests for the three confirmed GitHub Run #1275 defects."""
import pandas as pd

from trading_radar_market_first import append_live_market_snapshot
from trading_radar_neuauswertung import audit_market_first_content
from trading_radar_report_gate import (
    _treasury_observation_has_complete_provenance,
    _us10y_conflict_observation_records,
)


def _market_report(zinsfeld):
    return f"""1 · Trading-Radar
Marktregime: risk-on.
Indizes/Marktbreite: S&P 500 breadth improves.
Zinsen/Renditen: {zinsfeld}
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
Relative Stärke: Energy outperforms growth; winners and losers diverge.
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


def test_market_first_numeric_treasury_survives_missing_provenance():
    report = _market_report(
        "US 10Y Treasury yield 4.9800% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)"
    )
    errors = audit_market_first_content(report)
    assert not any("Zinsen/Renditen" in error for error in errors)


def test_thesis_trigger_450_is_not_market_observation_and_live_provenance_is_complete():
    dates = pd.to_datetime(["2026-11-03", "2026-11-04"], utc=True)
    data = pd.DataFrame({("^TNX", "Close"): [49.40, 49.52]}, index=dates)
    data.columns = pd.MultiIndex.from_tuples(data.columns)
    report = (
        "1 · Trading-Radar\n"
        "Zinsen/Renditen: US 10Y Treasury bei 4.9800% "
        "(Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR)\n"
        "2 · Thesen-Update\n"
        "Invalidierungstrigger: Rückgang des 10Y Treasury unter 4,50% "
        "und signifikante Zinssenkungssignale\n"
    )
    result, _warnings = append_live_market_snapshot(report, fetcher=lambda _symbols: data)
    conflicts = [line for line in result.splitlines() if "QUELLENKONFLIKT: OFFEN" in line]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert "Makro-Wert: 4.9800%" in conflict
    assert "Makro-Wert: 4.5000%" not in conflict
    for field in ("Quelle:", "Abrufzeit:", "Datenstichtag:", "Einheit:", "Verzögerung:"):
        assert conflict.count(field) >= 2
    assert "Yahoo Finance (^TNX)" in conflict
    assert "Tages-Schlusskurs" in conflict


def test_m07_does_not_borrow_live_provenance_for_macro_observation():
    line = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.9800% (Makro-Provenienz: "
        "Quelle: NICHT VERFÜGBAR; Abrufzeit: NICHT VERFÜGBAR; "
        "Datenstichtag: NICHT VERFÜGBAR; Einheit: NICHT VERFÜGBAR; "
        "Verzögerung: NICHT VERFÜGBAR) | "
        "Live-Wert: 4.9520% (Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-04T08:16:10+00:00; "
        "Datenstichtag: 2026-11-04T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst."
    )
    records = _us10y_conflict_observation_records(line)
    assert [role for role, _value, _record in records] == ["Makro", "Live"]
    by_role = {role: record for role, _value, record in records}
    assert not _treasury_observation_has_complete_provenance(by_role["Makro"])
    assert _treasury_observation_has_complete_provenance(by_role["Live"])


def test_m07_accepts_complete_independent_macro_and_live_provenance():
    line = (
        "- QUELLENKONFLIKT: OFFEN | Instrument: US-10Y Treasury | "
        "Makro-Wert: 4.9800% (Makro-Provenienz: "
        "Quelle: FRED DGS10; Abrufzeit: 2026-11-04T08:00:00+00:00; "
        "Datenstichtag: 2026-11-03; Einheit: %; Verzögerung: 1 Kalendertage) | "
        "Live-Wert: 4.9520% (Quelle: Yahoo Finance (^TNX); "
        "Abrufzeit: 2026-11-04T08:16:10+00:00; "
        "Datenstichtag: 2026-11-04T00:00:00+00:00; Einheit: %; "
        "Verzögerung: Tages-Schlusskurs) | "
        "Abgleich: nicht aufgelöst."
    )
    records = _us10y_conflict_observation_records(line)
    assert len(records) == 2
    assert all(_treasury_observation_has_complete_provenance(record)
               for _role, _value, record in records)
