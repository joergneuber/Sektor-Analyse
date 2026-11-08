# US Split-Adjustment Diagnostic – Alpaca SIP vs Yahoo

## Zweck

Isolierter, manuell gestarteter Diagnose-Lauf. Er vergleicht Alpaca SIP mit `adjustment=SPLIT` gegen den bereits im Projekt verwendeten Yahoo-Pfad (`yfinance`, `auto_adjust=False`). Damit werden die beiden OHLCV-Reihen auf eine vergleichbare Aktiensplit-Basis gebracht. Der Lauf verändert keine produktiven Dateien und keine SQLite-Daten.

## Dateien

- `diagnose_us_split_adjustment.py` – Diagnose-Skript
- `tests/test_diagnose_us_split_adjustment.py` – isolierte Unit-Tests
- `.github/workflows/us_split_adjustment_diagnostic.yml` – manueller Workflow (separat geliefert)
- `README_US_SPLIT_ADJUSTMENT.md` – diese Anleitung

Das Skript benötigt die bereits im Repository vorhandene `provider_coverage_us.py` und `historical_ohlcv.py`. Es nutzt das vorhandene US-Universum aus dem read-only Audit der Release-SQLite.

## Sicherheit und Kosten

- ausschließlich `workflow_dispatch`; kein Zeitplan
- `permissions: contents: read, actions: read`
- keine Orders, keine Trading-Aufrufe und keine Schreibzugriffe auf GitHub
- SQLite wird nur in ein temporäres Verzeichnis kopiert und dort auditiert; SHA-256 vor/nach dem Lauf muss identisch sein
- bestehende GitHub Secrets `ALPACA_KEY` und `ALPACA_SECRET`; keine Ausgabe der Schlüssel
- keine kostenpflichtigen Dienste oder Tarifänderungen

## Installation

1. `diagnose_us_split_adjustment.py` ins Repository-Root legen.
2. `tests/test_diagnose_us_split_adjustment.py` nach `tests/` legen.
3. Die separat gelieferte YAML-Datei nach `.github/workflows/us_split_adjustment_diagnostic.yml` legen.
4. `README_US_SPLIT_ADJUSTMENT.md` optional ins Repository-Root legen.
5. GitHub Actions → **US Split-Adjustment Diagnostic – Alpaca SIP vs Yahoo** → Run workflow.

Standard: 365 Kalendertage. Zulässig sind 30 bis 730 Tage.

## Ergebnisse

- `US_Split_Adjustment_Diagnostic.json` – vollständige Daten je Ticker
- `US_Split_Adjustment_Diagnostic.csv` – Tabellenformat
- `US_Split_Adjustment_Diagnostic.md` – Kurzbericht und größte Abweichungen

## Interpretation

Kleine Restdifferenzen können durch unterschiedliche Feeds, Tagesaggregation, Volumenbehandlung oder Yahoo-Korrekturen entstehen. Dieser Test prüft zunächst die Split-Basis. Er beweist allein weder eine konkrete Corporate Action noch, welcher Anbieter in jedem Einzelfall korrekt ist. Produktive Daten oder Berechnungen werden nicht umgestellt.
