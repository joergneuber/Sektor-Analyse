# Positionswiederherstellung – manifestgesteuert

## Zweck und aktueller Datenstand

Dieses Paket ergänzt einen separaten, manuell gestarteten GitHub-Actions-Workflow.
Er rekonstruiert die fehlende offene Positionsquelle und ergänzt bzw. erstellt den
Positions-Master mit der geschützten geschlossenen Historie.

Der aktuelle Recovery-Datensatz enthält:
- **16 offene Positionen**
- **51 geschlossene Positionen**

Diese Zahlen sind im `recovery/recovery_manifest.json` als Eigenschaften dieses
Datensatzes hinterlegt. Sie sind keine festen Laufzeitkonstanten des Recovery-Codes.

## Abgleich des vorhandenen Master-Snapshots

Der geprüfte Export `Offene Positionen+Check(3).xlsx` vom 12.11.2026 enthält
beide Tabs (`Offene Positionen+Check` und `Geschlossene Positionen`) mit
16 offenen und 51 geschlossenen Datensätzen. Die 51 geschlossenen Datensätze
stimmen vollständig mit der privaten Initialhistorie überein. Die 16 offenen
Datensätze stimmen mit der ursprünglichen offenen Excel-Quelle und der
Recovery-CSV überein.

Der Excel-Export meldet im Tab `Geschlossene Positionen` zwar `max_row=1025`,
enthält aber nur 51 befüllte Positionszeilen; die übrigen Zeilen sind leere
Formatierungs-/Tabellenreste. Der Live-Drive-Zustand wird weiterhin erst durch
den read-only Preflight bestätigt. Ein fehlender oder mehrfach vorhandener
Tab bleibt ein Sicherheitsabbruch und wird nicht automatisch erzeugt.

## Warum 16 nicht fest im Programm stehen darf

Bei einer späteren Wiederherstellung können z. B. 19 oder 23 offene Positionen
vorhanden sein. Der Code liest deshalb die erwartete Anzahl aus dem Manifest.
Eine spätere Wiederherstellung benötigt eine neue, geprüfte Quelldatei und eine
angepasste `expected_count`-Angabe samt SHA-256-Hash im Manifest.

Für die geschlossene Historie gilt dasselbe Prinzip: erwartete Anzahl und Hash
kommen aus dem Manifest und werden auch von `offene_positionen_check.py` für
spätere reguläre Läufe verwendet. Der projektweite Mindestschutz von **51
geschlossenen Positionen** bleibt jedoch bestehen. Eine kleinere Historie wird
nicht automatisch akzeptiert.

Eine spätere Datenänderung ist nur gültig, wenn CSV/Secret, Anzahl, Hash und
Positionsschlüssel zusammenpassen. Ein bloßes Ändern der Zahl im Manifest reicht
nicht aus.

## Enthaltene Dateien und Uploadpfade

- `.github/workflows/positions-recovery.yml` – manueller Zwei-Stufen-Workflow.
- `scripts/restore_positions.py` – Recovery-Orchestrierung und read-only Preflight.
- `scripts/__init__.py` – Python-Paketmarker.
- `tests/test_positions_recovery.py` – Recovery- und Sicherheitsregressionen.
- `recovery/Offene_Positionen_Quelle_16.csv` – aktuelle offene Ausgangsquelle.
- `recovery/recovery_manifest.json` – erwartete Datensatzanzahlen, Quellpfade und SHA-256-Werte.
- `offene_positionen_check.py` – geänderte Bestandsdatei; Quellkonflikte brechen ab und die private Historie wird manifestgesteuert geprüft.
- `tests/test_positions_lifecycle.py` – geänderte Bestandsdatei; Regressionstests einschließlich Manifest-/Secret-Verhalten.
- `POSITIONS_RECOVERY_README.md`, `FORENSIK_POSITIONS_RECOVERY.md`,
  `PATCH_MANIFEST_POSITIONS_RECOVERY.json`, `SHA256SUMS_POSITIONS_RECOVERY.txt`
  und `TESTS_POSITIONS_RECOVERY.log` – Dokumentation und Prüfnachweise.

## Sicherheitsprüfungen

1. Die offene Quelle muss exakt dem Manifest-Hash und der dort angegebenen Anzahl entsprechen.
2. Die private Historie kommt ausschließlich aus `CLOSED_POSITIONS_INITIAL_HISTORY_B64`; Hash, Schema, Status, Anzahl und eindeutige Positionsschlüssel werden geprüft.
3. Das Manifest darf weder die Eindeutigkeitsprüfung noch die exakte Hashprüfung oder append-only-Regel deaktivieren.
4. Der Mindestschutz von 51 geschlossenen Datensätzen ist fest im bestehenden Projekt verankert.
5. Mehrere mögliche Drive-Quellen oder Master führen zum Abbruch. Google-Tabelle plus CSV wird ausdrücklich als Mehrdeutigkeit behandelt.
6. Bestehende Historienzeilen werden nicht ersetzt oder gelöscht. Jeder bereits vorhandene Datensatz mit einem Initialschlüssel wird zusätzlich über alle 26 Historienfelder mit der privaten, gehashten Quelle verglichen; Abweichungen brechen ab. Fehlende verifizierte Datensätze werden ausschließlich angehängt.
7. Vor einer Historienergänzung wird der bestehende Master gesichert.
8. Ein fehlender Tab `Geschlossene Positionen` wird nicht automatisch erzeugt.
9. Die Wiederherstellung startet nicht `positionen_tracker.py`.

## Ablauf in GitHub Actions – zwei getrennte Starts

### Stufe 1: `PREFLIGHT_ONLY` (Standard)

- Workflow manuell starten und die voreingestellte Auswahl `PREFLIGHT_ONLY` beibehalten.
- Der Workflow führt zuerst die Regressionstests aus.
- Danach prüft er Manifest, Quelldatei, SHA-256, privates Historien-Secret,
  `GDRIVE_TOKEN`, OAuth-Scopes und lesenden Zugriff auf den Drive-Ordner.
- Wenn ein Master oder ein anderes Google Spreadsheet existiert, wird auch der
  Sheets-Lesezugriff und die Struktur geprüft.
- Es werden keine Drive-/Sheets-Schreiboperationen ausgeführt.

### Stufe 2: `RESTORE_VERIFIED_POSITIONS`

Erst nach erfolgreichem Stufe-1-Lauf den Workflow erneut manuell starten,
`RESTORE_VERIFIED_POSITIONS` auswählen und die Run-ID des erfolgreichen
`PREFLIGHT_ONLY`-Laufs in `preflight_run_id` eintragen. Der Workflow weist
maschinenlesbar nach, dass der frühere Lauf auf exakt demselben `main`-Commit
erfolgreich war, der Read-only-Preflight erfolgreich war und der Restore-Schritt
übersprungen wurde. Ohne diesen Nachweis bricht der Workflow vor jeder
Google-Schreiboperation ab. Danach wiederholt er Regressionstests und Preflight,
erstellt lokal die technische Check-Ausgabe und beginnt erst anschließend mit
den gesicherten Drive-/Sheets-Schreiboperationen.

Der Preflight kann Schreibberechtigungen anhand der OAuth-Scopes prüfen, aber
keinen tatsächlichen Schreibzugriff auf Drive/Sheets beweisen, ohne selbst eine
Schreiboperation auszuführen. Die endgültige Live-Verifikation erfolgt deshalb
erst im ausdrücklich freigegebenen Recovery-Lauf.

## Secrets

Pflicht:
- `GDRIVE_TOKEN`
- `CLOSED_POSITIONS_INITIAL_HISTORY_B64`
- `TWELVEDATA_API_KEY`, solange die offene Recovery-Quelle `XAU/USD` enthält
  (das ist im aktuellen 16er-Bestand der Fall).

Der Preflight prüft `TWELVEDATA_API_KEY` dynamisch anhand der enthaltenen Ticker
auf Vorhandensein. Die tatsächliche Gültigkeit des Datenanbieter-Schlüssels wird
im freigegebenen Recovery-Lauf durch den Twelve-Data-Kursabruf geprüft. Dieser
Abruf findet vor dem ersten Drive-/Sheets-Schreibzugriff statt.

Der Preflight verlangt den vollständigen OAuth-Scope
`https://www.googleapis.com/auth/drive` sowie
`https://www.googleapis.com/auth/spreadsheets`. `drive.file` allein reicht nicht,
weil damit die vollständige Inventarisierung vorhandener gleichnamiger Dateien
im Drive-Ordner nicht garantiert ist. Zusätzlich wird der Drive-Ordner lesend
inventarisiert.
Das private Historien-Secret wird Base64-dekodiert und anhand des im Manifest
hinterlegten SHA-256-Hashes sowie des Schemas geprüft. Secret-Werte werden nie
ausgegeben.

Markt-Daten-Secrets (z. B. Twelve Data oder Alpaca) werden im eigentlichen
Recovery-Lauf benötigt, soweit die offenen Positionen den jeweiligen Datenpfad
verwenden. Die lokale technische Analyse läuft vollständig vor dem ersten
Google-Schreibzugriff.

## Testgrenzen

Die Recovery- und Lifecycle-Tests wurden nach den Änderungen lokal ausgeführt.
Der unveränderte Baseline-Test `tests/test_offene_positionen_check_history_source.py`
ist Teil des GitHub-Workflows, liegt aber nicht im gelieferten Patch-ZIP, weil er
gegenüber GitHub unverändert ist. Ein Google-Drive-/Sheets-Live-Test wurde nicht
ausgeführt; dafür sind die privaten Repository-Secrets und ein manuell
freigegebener Workflow-Lauf erforderlich. `actionlint` ist in der lokalen
Prüfumgebung nicht installiert.
