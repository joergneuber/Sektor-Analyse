# US Provider Coverage – Alpaca vs Yahoo

## Zweck

Ein isolierter, manuell gestarteter GitHub-Actions-Lauf vergleicht die aktuelle
Datenabdeckung von Alpaca und Yahoo für das vollständige US-Universum (Kalender
XNYS/XNAS) aus `analyse.py` plus `historical_ohlcv_universe.json`.

Verglichen werden:

- der letzte abgeschlossene Handelstag;
- erwartete Handelssitzungen und fehlende Sitzungen im konfigurierten Fenster
  (Standard: 365 Kalendertage);
- Anzahl und Zeitraum gelieferter Bars;
- einfache OHLCV-Strukturfehler;
- sämtliche im historischen OHLCV-Audit erkannten offenen US-Handelstagslücken,
  bei Bedarf mit gezielten Einzelabfragen beider Anbieter.

Der zuletzt geprüfte Datenbestand (07.11.2026) enthielt 663 US-Kalendersymbole:
658 Aktien/ETFs und fünf Index-Benchmarks. Die 153 länger offenen historischen
Lücken lagen damals nicht in XNYS-/XNAS-Symbolen. Zusätzlich fehlte bei 661
US-Symbolen der 06.11.2026-Bar in der SQLite; die aktuelle Yahoo-Probe konnte
diesen Tag für alle 661 Symbole zurückliefern. Diese Werte sind eine Baseline,
keine Vorwegnahme des neuen Alpaca-Vergleichs. Siehe
`BASELINE_AUSGANGSLAGE_2026-11-07.md`.

Alpaca wird ohne `feed`-Argument abgefragt, damit der Test den derzeitigen
Produktionsaufruf in `market_data.py`/`analyse.py` nachbildet. `adjustment=RAW`
ist explizit gesetzt (API-Standard). Yahoo wird mit `auto_adjust=False` über den
bestehenden `_download_batch`-Pfad abgefragt.

## Sicherheitsgrenzen

- Nur `workflow_dispatch`; kein Zeitplan und keine Änderung am produktiven
  Hauptworkflow.
- Keine Orders, keine Trading-API-Aufrufe, keine Änderungen an Datenbankzeilen.
- Die Release-SQLite wird vorab read-only geprüft und bytegleich in ein
  temporäres Verzeichnis kopiert. Das bestehende Audit läuft ausschließlich
  auf dieser Kopie.
- Vor und nach dem Lauf wird SHA-256 der Original-SQLite verglichen.
- Die Release-Dateien werden nur heruntergeladen; kein Release-Upload.
- Secrets `ALPACA_KEY` und `ALPACA_SECRET` werden ausschließlich an den
  Diagnose-Step übergeben und nicht ausgegeben.
- Ergebnisdateien sind JSON, CSV und Markdown als GitHub Actions Artifact.

## Installation durch den Repository-Eigentümer

Die Dateien aus diesem Paket manuell in das Repository übernehmen:

1. `provider_coverage_us.py` in das Repo-Root.
2. `tests/test_provider_coverage_us.py` in `tests/`.
3. `.github/workflows/us_provider_coverage.yml` in `.github/workflows/`.
4. `README_PROVIDER_COVERAGE.md` optional ins Repo-Root.

Anschließend die neue Action **US Provider Coverage – Alpaca vs Yahoo** manuell
über GitHub Actions starten. Es werden die bestehenden GitHub Secrets
`ALPACA_KEY` und `ALPACA_SECRET` verwendet. Standardfenster 365 Tage.

## Erwartete Ausgaben

- `US_Provider_Coverage.json`: vollständiger Maschinenreport und alle Ticker.
- `US_Provider_Coverage.csv`: eine Zeile je US-Ticker.
- `US_Provider_Coverage.md`: kompakte Zusammenfassung.

`no_rows` ist nicht gleichbedeutend mit "Symbol grundsätzlich nicht
unterstützt". Request-Fehler, leere Datenantworten und echte fehlende
Handelssitzungen werden getrennt ausgewiesen.
