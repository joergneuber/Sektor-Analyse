# Forensische Codeprüfung – OHLCV Paket 4
Datum: 2026-11-06

## 1. Ausgangsbasis / GitHub-Abgleich

Repository: `joergneuber/Sektor-Analyse`  
Branch: `main`  
Ausgangscommit: `51d82213f18abdea5b87553e5da99d3dfcf90297`

Die drei Ausgangsdateien aus der Nutzer-ZIP wurden anhand ihrer Git-Blob-SHA-1-Werte mit dem GitHub-Stand verglichen. Alle drei stimmen exakt überein. Die Nutzer-ZIP selbst ist intakt (181 Einträge, ZIP-Test OK; SHA-256 `62b3912c63c012757545b3c49256bd66fafd123e8fb35de22a62f6cbb5d3ed7e`).

## 2. Befund aus Run #11

Run `42223790135`, Job `127912037163`, erfolgreich abgeschlossen. Die tatsächlichen Einzelresultate im Artefakt zeigen:

- Yahoo repair: 112 Tickeranfragen mit Request-Fehlern.
- Davon 3.781 Zeilen mit `ModuleNotFoundError: No module named 'scipy'` und 2.172 Zeilen mit `ModuleNotFoundError: No module named 'sklearn'`. Das sind CSV-Zeilenfehler, keine Tickerzahlen.
- Stooq: 52 Ticker mit `no_data`, 52 Ticker mit `ConnectTimeout`, 8 nicht unterstützte Mappings.
- Die Stooq-Timeouts beginnen nach einer Reihe erfolgreicher Antworten mit `no_data` und treten anschließend für die übrigen abgefragten Symbole auf.
- Klassifikation blieb bei 4.274 `ABWEICHUNG_UNGEKLÄRT` und 1.679 `SETTLEMENT_SONDERFALL`; null `BESTÄTIGT_FEHLERHAFT`.

Interpretation: Der fehlende Yahoo-Reparatur-Unterbau ist ein klarer Installationsfehler im Workflow. Die Stooq-Timeouts zeigen einen Transport-/Erreichbarkeitsfehler im Runner-Lauf, nicht automatisch fehlende Kursdaten.

## 3. Änderungen

### A. `.github/workflows/diagnose_historical_ohlcv_release.yml`

- `scipy` und `scikit-learn` zur Installationszeile ergänzt.
- Stooq-Timeout auf 10 Sekunden gesetzt.
- Circuit-Breaker-Schwelle auf zwei aufeinanderfolgende Transportfehler gesetzt.
- Keine Änderung an Trigger, Berechtigungen, Datenbankdownload, Diagnose- oder Upload-Schritten.

### B. `ohlcv_invalid_crosscheck.py`

- Stooq-Timeout erhält eine eigene Umgebungsvariable `NMM_OHLCV_STOOQ_TIMEOUT`; bisherige `NMM_OHLCV_SOURCE_TIMEOUT` bleibt als Rückfall kompatibel.
- Neue Umgebungsvariable `NMM_OHLCV_STOOQ_CIRCUIT_BREAKER_FAILURES`, Standardwert 2.
- Nach der Schwelle von aufeinanderfolgenden `requests`-Timeout-/Connection-Fehlern werden weitere Stooq-Aufrufe für diesen Lauf übersprungen.
- Nicht abgefragte Symbole erhalten den Status `provider_unavailable_after_consecutive_errors`.
- Diese Fälle bleiben `ABWEICHUNG_UNGEKLÄRT`; sie werden ausdrücklich nicht als korrekte oder fehlerhafte Kerzen bewertet.
- Die Zusammenfassung protokolliert Timeout, Schwelle, Circuit-Breaker-Status und Transportfehlerzahl.
- Diagnoseversion auf 2 erhöht.
- Keine SQLite-Import-/Verbindungs-/Schreibfunktion hinzugefügt. Das Skript liest weiterhin nur die exportierte CSV.

### C. `tests/test_ohlcv_invalid_crosscheck.py`

- Neuer Regressionstest simuliert zwei aufeinanderfolgende `ConnectTimeout`-Fehler.
- Prüft, dass nach der Schwelle keine weiteren Stooq-Aufrufe erfolgen.
- Prüft die Statusverteilung und dass die nicht abgefragten Ticker ungeklärt bleiben.

## 4. Forensische Dateiebene

Alle Dateien: UTF-8, LF-Zeilenenden. Exakte Bytes, Zeilenzahlen, SHA-256 und Git-Blob-SHAs stehen im Manifest.

| Datei | Vorher | Nachher | Diff |
|---|---:|---:|---:|
| Workflow | 3.549 Bytes / 99 Zeilen | 3.623 Bytes / 100 Zeilen | +3 / -2 |
| Crosscheck-Modul | 16.780 Bytes / 340 Zeilen | 18.252 Bytes / 370 Zeilen | +35 / -5 |
| Tests | 5.081 Bytes / 125 Zeilen | 6.676 Bytes / 173 Zeilen | +48 / -0 |
| **Gesamt** | **25.410 Bytes / 564 Zeilen** | **28.551 Bytes / 643 Zeilen** | **+86 / -7** |

Der vollständige zeilenweise Unified Diff liegt in `Paket4_Forensik_Diff_20261106.diff`.

## 5. Strukturelle Prüfung

- Keine Funktion, Klasse oder Import im Produktionsmodul entfernt oder umbenannt.
- Produktionsmodul: vorhandene Imports und Top-Level-Funktionen unverändert; neue Konfigurationskonstanten und Circuit-Breaker-Zustand innerhalb `run_crosscheck`.
- Testdatei: `requests`-Import und ein zusätzlicher Test.
- Workflow: nur Installationsabhängigkeiten und zwei Umgebungsvariablen angepasst.

## 6. Semantik / Regression

- Fehlende optionale Yahoo-Reparaturabhängigkeiten werden installiert.
- Stooq kann bei wiederholten Transportfehlern nicht mehr unnötig den restlichen Lauf mit jeweils 10-Sekunden-Timeouts verlängern.
- Erfolgreiche Stooq-Antworten oder `no_data` setzen den aufeinanderfolgenden Transportfehlerzähler zurück.
- Nicht verfügbare Daten bleiben fachlich ungeklärt; es erfolgt keine automatische Reparatur und keine Änderung an OHLCV-Werten.
- Die bestehende CME-Settlement-Sonderfalllogik bleibt unverändert.

## 7. Teststatus

- Gezielte Paket-2-/Paket-3-Regressionstests plus Paket-4-Test: **21 passed**.
- `py_compile`: bestanden.
- YAML-Parsing: bestanden.
- Mock-Lauf auf den 5.953 INVALID-Zeilen aus Run #11: alle 5.953 Zeilen und 112 Ticker verarbeitet. Bei simuliertem Stooq-Ausfall nach 52 `no_data`-Antworten wurden nur zwei Timeout-Aufrufe ausgeführt; 50 weitere gemappte Ticker erhielten den Status `provider_unavailable_after_consecutive_errors`; 8 nicht unterstützte Mappings blieben erhalten.
- Vollständige Projekt-Test-Suite: nicht ausgeführt.
- Echter Yahoo-/Stooq-Live-Retest: nicht ausgeführt. Dafür ist nach dem Upload ein neuer GitHub-Workflow-Lauf erforderlich.

## 8. Sicherheits- und Vollständigkeitsgate

- Produktionsdatenbank: nicht geöffnet und nicht verändert.
- GitHub: durch den Assistenten nicht verändert; kein Commit, Upload, Workflow-Start oder Rerun.
- Geänderte Laufzeitdateien: genau drei.
- Paket enthält die drei Dateien an den korrekten Repository-Pfaden sowie Forensik-Diff, Manifest und Uploadhinweise.
