# Forensische Codeprüfung – OHLCV-Qualitätsdetaildiagnose

**Prüfdatum:** 06.11.2026  
**Repository:** `joergneuber/Sektor-Analyse`  
**Branch:** `main`  
**Unveränderter Ausgangscommit:** `28f523413d59ce494fe79a5856276c5802e63622`

## 1. Prüfgegenstand und Eingrenzung

Es wird ausschließlich der vorhandene Workflow **„OHLCV Release-Datenbank Diagnose“** erweitert. Es wird kein neuer Workflow angelegt. Die Release-SQLite-Datei wird weiterhin bytegenau kopiert; die Audit- und Detailabfragen laufen gegen die Arbeitskopie. Die Quelldatenbank wird nach dem Lauf erneut gehasht und auf Unverändertheit geprüft.

Geänderte Dateien:
- `diagnose_historical_ohlcv_release.py`
- `tests/test_diagnose_historical_ohlcv_release.py`
- `.github/workflows/diagnose_historical_ohlcv_release.yml`

Unverändert und nicht Teil des Pakets:
- `historical_ohlcv.py`
- `analyse.py`
- `.github/workflows/main.yml`
- die tägliche OHLCV-Update-Logik und die Release-Datenbank

## 2. Byte-/Dateiebene

Alle drei Ausgangsdateien wurden gegen die aktuelle GitHub-Version auf `main` abgeglichen. Die Basis-Git-Blob-SHAs stimmen mit den gelesenen GitHub-Blobs überein. Größen, Zeilenzahlen und SHA-256 vor/nach der Änderung stehen im `OHLCV_Qualitaetsdetail_Manifest_20261106.json`.

Alle geänderten Dateien sind UTF-8 mit LF-Zeilenenden. Es wurden keine Binärdateien oder Datenbankdateien geändert.

## 3. Zeilenweise Änderungen

- Diagnose-Skript: neue Konstanten für vier zusätzliche Artefakte; neue Detailabfragen und Ausgabefunktionen; Diagnoseversion von 3 auf 4; Einbindung der Detailauswertung nach erfolgreichem `audit_snapshot`.
- Testdatei: fünf neue Tests für Fehlergrund-/90-Tage-Aufschlüsselung, fehlende Ticker, isolierte Providerfehler, Artefakterstellung und den Ablauf über `main()` mit unveränderter Quelldatenbank.
- Workflow: zwei Vorabprüfungen für die neuen Funktionen und vier zusätzliche Artefaktpfade.

Der vollständige Unified Diff liegt in `OHLCV_Qualitaetsdetail_Diff_20261106.diff`.

## 4. Strukturelle Prüfung

Neue Funktionen im Diagnose-Skript:
- `collect_invalid_candle_details()` – lädt jede fehlerhafte Kerze der als INVALID klassifizierten Ticker.
- `probe_yahoo_missing_tickers()` – prüft jeden fehlenden Universums-Ticker separat und fängt Providerfehler je Symbol ab.
- `collect_missing_instrument_details()` – verbindet Universe-Fehlstatus, Instrument-Datensatz, Bar-Anzahl und letzte gespeicherte Bar.
- `write_quality_detail_outputs()` – schreibt CSV-, JSON- und Markdown-Berichte.

Die SQL-Fehlerbedingungen entsprechen den INVALID-Prädikaten aus `historical_ohlcv.audit_snapshot`: fehlende OHLCV-Felder, nichtpositive OHLC-Werte, negatives Volumen sowie unplausible High-/Low-Beziehungen. Zusätzlich wird die Zahl der tatsächlich gefundenen INVALID-Ticker gegen die Tickerliste des bestehenden Audits gegengeprüft.

## 5. Semantische/logische Auswirkungen

Die Diagnose beantwortet künftig:
1. Wie viele einzelne Kerzen sind fehlerhaft – nicht nur wie viele Ticker tragen den Status INVALID?
2. Welche konkrete Bedingung verletzt jede Kerze (z. B. `high_below_low`, `volume_negative`)?
3. Liegt der Fehler innerhalb oder außerhalb des bestehenden 90-Tage-Fensters?
4. Gibt es für jeden fehlenden Universums-Ticker einen Instrument-Datensatz und gespeicherte Bars?
5. Liefert Yahoo für den fehlenden Ticker aktuell Daten der letzten 90 Kalendertage?

Die Yahoo-Abfrage ist ausdrücklich nur eine aktuelle Verfügbarkeitsprobe. Sie beweist nicht rückwirkend, ob ein Symbol beim früheren Datenbanklauf verfügbar war. `no_provider_rows` wird daher nicht als endgültiger Nachweis eines ungültigen Symbols interpretiert.

## 6. Vollständigkeit der Artefakte

Der bestehende Workflow lädt weiterhin die bisherigen Diagnoseberichte hoch und ergänzt:
- `OHLCV_Invalid_Detail.csv` – eine Zeile pro fehlerhafter Kerze mit Datum, OHLCV, Quelle, Fehlergründen und 90-Tage-Kennzeichnung.
- `OHLCV_Invalid_Zusammenfassung.json` – Zähler, Fehlergrundverteilung und Tickerzusammenfassung.
- `OHLCV_Missing_Instruments.csv` – DB-Abdeckung und Yahoo-Probe pro fehlendem Universums-Ticker.
- `OHLCV_Quality_Report.md` – lesbare Gesamtauswertung.

Die fehlenden Ticker werden dynamisch aus `audit_snapshot` übernommen; sie sind nicht auf `ACT0.DE`, `SKYD` und `VMRK` fest verdrahtet.

## 7. Regressionen und Tests

Erfolgreich:
- `python -m pytest -q tests/test_diagnose_historical_ohlcv_release.py tests/test_historical_ohlcv.py` – **36 passed**.
- `python -m compileall -q .` – erfolgreich.
- YAML-Parsing der Workflow-Datei – erfolgreich.
- Integrationstest über `main()` mit SQLite-Fixture: Quelldatenbank-SHA-256 bleibt unverändert; Audit-Kopie wird entfernt; neue Berichte werden geschrieben.

Nicht vollständig abgeschlossen:
- `python -m pytest -q tests` wurde zweimal mit einem Zeitlimit abgebrochen (zunächst 120 s, anschließend 240 s); der letzte Lauf war bei ca. 46 %. Deshalb wird die vollständige Repository-Testsuite ausdrücklich **nicht** als bestanden ausgewiesen.
- `ruff`, `pyflakes` und `flake8` sind in der lokalen Umgebung nicht installiert.
- Ein Lauf gegen die echte 969-MB-Release-Datenbank ist noch nicht erfolgt; dieser reale Integrationstest erfolgt nach Upload der drei Dateien und manuellem Start des vorhandenen Workflows.

## 8. GitHub-Abgleich und Übergabe

GitHub `main` wurde nur lesend abgefragt. Es wurden weder Dateien hochgeladen noch Commits erstellt, Workflows gestartet oder Repository-Einstellungen geändert.

Nach dem manuellen Upload der drei geänderten Dateien kann der bestehende Workflow **„OHLCV Release-Datenbank Diagnose“** über `workflow_dispatch` gestartet werden. Erst sein Artefakt liefert die echten Einzelbefunde für die 112 INVALID-Ticker und die zum Laufzeitpunkt fehlenden Universums-Ticker.
