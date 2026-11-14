# Forensischer Korrekturbericht – Positionswiederherstellung

**Paketbasis:** `POSITIONS_WIEDERHERSTELLUNG_FIX_20261114_MANIFESTGESTEUERT_NACHBESSERUNG(2).zip`  
**Repository:** `joergneuber/Sektor-Analyse`  
**Branch / verifizierter HEAD:** `main` / `aa60b9f134a0b3f683e79ed104a09884de4c0961`  
**Prüfdatum:** 14.11.2026  
**Status:** Korrekturpaket lokal geprüft; keine GitHub- oder Google-Schreiboperationen.

## 1. Ausgangsbasis und Integrität

Das vom Nutzer bereitgestellte ZIP wurde mit CRC-Prüfung geöffnet. Das ZIP enthält 13 Dateien; die ursprünglichen Dateien wurden als Ausgangsbasis verwendet. Die zwei bereits im Paket geänderten Bestandsdateien (`offene_positionen_check.py` und `tests/test_positions_lifecycle.py`) wurden in dieser Korrekturrunde nicht weiter verändert.

Der GitHub-HEAD wurde erneut direkt über die GitHub-API gelesen. Er stimmt mit dem im Patchmanifest angegebenen Basis-Commit überein. Die GitHub-Blob-SHAs der beiden geänderten Bestandsdateien stimmen weiterhin mit den Basis-SHAs im ursprünglichen Patchmanifest überein. Ein Git-Clone war in der Laufzeitumgebung wegen fehlender DNS-/Netzwerkauflösung nicht möglich; der Repository-Snapshot aus der vorherigen vollständigen Übergabe wurde mit dem erneut abgefragten HEAD und den einzelnen GitHub-Datei-Blob-SHAs abgeglichen.

## 2. Korrekturen

### 2.1 Historienvergleich: Schlüssel plus vollständige Feldintegrität

**Datei:** `scripts/restore_positions.py`

Vorher wurde eine vorhandene Historienzeile im Wesentlichen über den Positionsschlüssel identifiziert. Gleicher Schlüssel bei verändertem Ausstiegskurs, Ausstiegsdatum, Status oder anderen Feldern wurde nicht erkannt.

Jetzt wird jeder bereits vorhandene Datensatz, dessen Positionsschlüssel zum privaten Initialbestand gehört, gegen sämtliche 26 Felder aus `HISTORY_HEADERS` verglichen.

- Zahlen werden numerisch normalisiert, damit `1,00` und `1.00` als gleicher Wert gelten.
- Einstiegs- und Ausstiegsdaten werden auf ISO-Datum normalisiert.
- Textfelder werden nach Trim inhaltlich exakt verglichen.
- Jede Abweichung löst einen Sicherheitsabbruch aus.
- Spätere zusätzliche Trades außerhalb des privaten Initialbestands bleiben erhalten und werden nicht mit dem Initialbestand überschrieben.
- Unterschiedliche Datumsdarstellungen dürfen weder künstliche Schlüsselunterschiede erzeugen noch doppelte Trades verbergen; nach Normalisierung erkannte Duplikate brechen ab.
- Die Prüfung wird nach dem Append erneut gegen den final gelesenen Tab ausgeführt.

**Semantische Wirkung:** Der private SHA-256-geschützte Bestand ist jetzt nicht nur eine Schlüsselreferenz, sondern eine Feld-für-Feld-Integritätsreferenz für alle enthaltenen Initialdatensätze. Bestehende Werte werden weiterhin niemals automatisch überschrieben.

### 2.2 OAuth-Drive-Scope

**Datei:** `scripts/restore_positions.py`

Vorher genügte entweder `drive` oder `drive.file`. Jetzt ist der vollständige Scope `https://www.googleapis.com/auth/drive` zwingend erforderlich. `drive.file` allein wird explizit zurückgewiesen, weil damit die vollständige Inventarisierung bereits vorhandener Drive-Dateien nicht garantiert ist. Der Sheets-Scope bleibt zusätzlich erforderlich.

**Semantische Wirkung:** Der Preflight akzeptiert keinen Scope mehr, der eine unvollständige Sicht auf die vorhandenen Quelldateien ermöglichen könnte. Das reduziert das Risiko, dass ein unsichtbarer vorhandener Master irrtümlich als nicht existent behandelt wird.

### 2.3 Zweistufiger Workflow wird technisch erzwungen

**Datei:** `.github/workflows/positions-recovery.yml`

Der Restore-Start benötigt jetzt zusätzlich `preflight_run_id`. Vor Installation und Ausführung des Recovery-Codes prüft der Workflow mit einem read-only GitHub-API-Aufruf:

- Run gehört exakt zu `.github/workflows/positions-recovery.yml`.
- Event war `workflow_dispatch`.
- Branch war `main`.
- `head_sha` entspricht exakt dem aktuellen `GITHUB_SHA`.
- Run ist abgeschlossen und erfolgreich.
- Der Read-only-Preflight-Schritt war erfolgreich.
- Der eigentliche Restore-Schritt wurde im vorherigen Lauf übersprungen.

Dazu wurde `actions: read` ergänzt; `contents: read` bleibt bestehen. Es gibt keine GitHub-Schreibberechtigung. Die Wiederherstellung selbst wird weiterhin ausschließlich durch den manuellen `RESTORE_VERIFIED_POSITIONS`-Eingabewert ausgelöst.

**Semantische Wirkung:** Ein erfolgreicher Preflight-Lauf auf einem anderen Commit, ein fehlgeschlagener Lauf oder ein früherer Restore-Lauf kann nicht als Freigabenachweis verwendet werden.

### 2.4 Finale Prüfung der offenen Positionsstammdaten

**Datei:** `scripts/restore_positions.py`

`_verify_master_open_tab()` vergleicht nicht mehr nur Ticker und Status. Es vergleicht jetzt für jede erwartete offene Position die stabilen Quellstammdaten:

- Ticker
- Name
- Markt
- Währung
- Einstiegsdatum
- Einstieg
- Status
- OS-WKN
- OS-Einstiegskurs
- OS-Quelle
- OS-Kurszeit

Aktueller Kurs und technische Analysefelder bleiben bewusst ausgenommen, weil sie während des Recovery-Laufs neu berechnet werden.

**Semantische Wirkung:** Ein Master kann nicht mehr allein aufgrund korrekter Ticker und Status als erfolgreich verifiziert gelten, wenn wesentliche Positionsstammdaten abweichen.

### 2.5 Regressionstests und Dokumentation

**Dateien:** `tests/test_positions_recovery.py`, `POSITIONS_RECOVERY_README.md`

Neue Tests decken insbesondere ab:
- gleiche Positionsschlüssel bei verändertem Ausstiegskurs,
- abweichendes Ausstiegsdatum,
- abweichenden Status,
- abweichenden Sektor und OS-WKN,
- äquivalente numerische und Datumsdarstellungen,
- zusätzliche spätere Trades,
- Duplikate nach Datumsnormalisierung,
- `drive.file` ohne vollständigen Drive-Scope,
- abweichende offene Positionsstammdaten,
- Workflow-Nachweis eines separaten Preflight-Laufs.

Die README beschreibt nun die erforderliche Run-ID und den vollständigen Drive-Scope.

## 3. Diffumfang gegenüber dem ursprünglichen Korrektur-ZIP

| Datei | Ursprünglich | Korrigiert | Änderung |
|---|---:|---:|---|
| `scripts/restore_positions.py` | 557 Zeilen | 668 Zeilen | Vollständiger Feldvergleich, Scope, Open-Tab-Prüfung, finale Prüfung |
| `.github/workflows/positions-recovery.yml` | 82 Zeilen | 119 Zeilen | Preflight-Run-ID, API-Nachweis, actions:read |
| `tests/test_positions_recovery.py` | 406 Zeilen | 581 Zeilen | Neue Regressionen für die zuvor offenen Risiken |
| `POSITIONS_RECOVERY_README.md` | 130 Zeilen | 139 Zeilen | Aktualisierter sicherer Ablauf |

Die Zeilenzahlen sind die gezählten Quellzeilen der Dateien im ursprünglichen und korrigierten Paket; die vollständigen Dateien sind im ZIP enthalten.

Unverändert gegenüber dem ursprünglichen Korrektur-ZIP:
- `offene_positionen_check.py`
- `tests/test_positions_lifecycle.py`
- `scripts/__init__.py`
- `recovery/Offene_Positionen_Quelle_16.csv`
- `recovery/recovery_manifest.json`

Die Dokumentations- und Prüfnachweise (`FORENSIK_POSITIONS_RECOVERY.md`, `TESTS_POSITIONS_RECOVERY.log`, Patchmanifest und SHA256-Liste) wurden aktualisiert.

## 4. Testresultate

### Gezielte Positions-/Recovery-Suite

Befehl:

```bash
python -m pytest -q -W error \
  tests/test_positions_recovery.py \
  tests/test_positions_lifecycle.py \
  tests/test_offene_positionen_check_history_source.py
```

Ergebnis:

```text
109 passed
Exit 0
```

### Erweiterte Regression

Befehl:

```bash
python -m pytest -q \
  tests/test_positions_recovery.py \
  tests/test_positions_lifecycle.py \
  tests/test_offene_positionen_check_history_source.py \
  tests/test_trading_radar_run_evidence.py \
  tests/test_trading_radar_repair_contracts.py \
  tests/test_workflow_dynamic_universe.py
```

Ergebnis:

```text
180 passed
Exit 0
```

Ein zusätzlicher Lauf dieser erweiterten Suite mit `-W error` meldete ebenfalls 180 bestandene Tests, endete aber mit Exit 1 aufgrund bereits bestehender `sqlite3.Connection`-ResourceWarnings/`PytestUnraisableExceptionWarning` in den OHLCV-/SQLite-Tests. Die gezielte Positionssuite mit `-W error` besteht dagegen mit Exit 0.

### Statische Validierung

- Python-Kompilierung der geänderten Python-Dateien: bestanden.
- YAML-Parsing des Recovery-Workflows: bestanden.
- Manifest-JSON-Parsing: bestanden.
- ZIP-CRC: bestanden.
- SHA-256- und Git-Blob-SHA-Neuberechnung: Bestandteil der Paketfinalisierung.
- `actionlint`: nicht installiert; daher kein actionlint-Nachweis.
- Vollständige Repository-Testsuite: nicht abgeschlossen.
- Live-Google-Drive-/Sheets-Test: nicht ausgeführt.

## 5. Verbleibende Grenzen

1. Die tatsächlichen OAuth-Scopes und die Sichtbarkeit des privaten Drive-Ordners können ohne Repository-Secrets nicht live bestätigt werden.
2. Die neue Run-ID-Sperre wurde lokal statisch und per YAML-Regression geprüft, aber nicht in GitHub Actions ausgeführt.
3. Es wurde kein Google-Sheets-Schreibtest durchgeführt.
4. Die tatsächliche Gültigkeit von `TWELVEDATA_API_KEY` wurde nicht geprüft.
5. `actionlint` steht nicht zur Verfügung.

## 6. GitHub- und Datenintegrität

- Keine Commits, Branches, Dateiänderungen oder Workflow-Starts durch ChatGPT.
- Keine Google-Drive-/Sheets-Schreiboperationen.
- Der Nutzer behält den Upload ausschließlich selbst in der Hand.
- Das korrigierte Paket enthält alle geänderten und neuen Dateien einschließlich aktualisierter Prüfsummen und Manifest.


## 7. Aktueller GitHub-Actions-Status (erneut read-only abgefragt)

Der Branch `main` steht weiterhin auf `aa60b9f134a0b3f683e79ed104a09884de4c0961`.

Zuletzt sichtbare Läufe auf diesem HEAD:
- `43546513116` – Positions-Alert Check: success
- `43523157712` – Benchmarks-Zwischenstand: success
- `43512540091` – tägliche Analyse Sektoren: failure
- `43511870616` – Gemini Regressionstests: success

Der bekannte Fehler im täglichen Sektorenlauf betrifft den OHLCV-Coverage-/Datenqualitäts-Test (`test_historical_ohlcv.py`, Coverage mit `MISSING`/`HIST-OHLCV-STRICT=PARTIAL`) und ist unabhängig vom hier korrigierten Positions-Recovery-Paket. Es wurde kein Workflow gestartet oder erneut ausgeführt.
