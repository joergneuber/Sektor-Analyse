# OHLCV Pending-Queue – Architektur und Betriebsvertrag

## Zweck

Der manuelle Einzel-Check darf das gemeinsame OHLCV-Universum nicht mehr direkt
verändern und insbesondere kein `historical-ohlcv`-Release schreiben. Er meldet
validierte, erfolgreich geladene Ticker als Pending-Queue an den nächsten
Hauptlauf. Ausschließlich `.github/workflows/main.yml` schreibt das dauerhafte
OHLCV-Bundle.

## Datenfluss

1. `einzel_check.yml` initialisiert pro Workflow-Run
   `historical_ohlcv_pending_queue.json`.
2. `einzel_check.py` registriert einen Ticker erst nach erfolgreichem Laden und
   erfolgreicher technischer Datenprüfung über
   `historical_ohlcv_pending_queue.register_pending_ticker()`.
3. Der Queue-Writer normalisiert und dedupliziert Ticker. Registrierungsfehler
   werden als `queue_errors` mit dem betroffenen Ticker dokumentiert. Lokale
   Aufrufe ausserhalb von GitHub Actions behalten das bisherige Verhalten und
   registrieren direkt im lokalen Universum, ohne eine Queue-Datei anzulegen.
4. Ein Finalisierungsschritt schreibt das Ergebnis des eigentlichen
   Einzel-Check-Schritts in das Queue-Manifest.
5. `actions/upload-artifact@v6` lädt pro Run und Attempt ein unveränderliches
   Artifact hoch. Auch leere Queues werden hochgeladen, damit „keine neuen
   Ticker“ von einem fehlenden Artifact unterscheidbar bleibt.
6. Der Hauptworkflow importiert Queues nach der Wiederherstellung des letzten
   Bundles und vor dem ersten OHLCV-Update, damit Scanner die neuen Ticker sehen.
7. Direkt nach den Scannerläufen erfolgt ein zweiter Queue-Checkpoint. Er fängt
   während des Hauptlaufs fertig gewordene Einzel-Checks ab. Werden dabei neue
   Ticker hinzugefügt, wird der Lauf `PARTIAL`: Die Ticker werden gespeichert,
   aber die bereits gelaufenen Scanner können sie noch nicht berücksichtigt
   haben. Der nächste Hauptlauf scannt sie regulär.
8. Der Importer prüft zusätzlich `queued`- und `in_progress`-Einzel-Check-
   Workflows. Ein aktiver oder auf einen Runner wartender Queue-Producer
   verhindert einen `COMPLETE`-Status.
9. Der Importer prüft Branch, Workflow-Pfad, Commit-SHA, Run-ID, Attempt,
   Scanner-Step, Artifact-Step, Artifact-Namen, Manifest und Ticker.
10. Übernommene Run-/Attempt-Receipts liegen in einer eigenen SQLite-Tabelle
   innerhalb der OHLCV-Datenbank. Damit ist der Import nach erfolgreicher
   Bundle-Persistenz idempotent. Wird das Bundle nicht erfolgreich gespeichert,
   fehlt auch das Receipt im nächsten Runner; die Queue wird erneut importiert.

## Run- und Artifact-Prüfung

- Es werden nur Runs des Workflows `einzel_check.yml` auf dem Branch des
  aktuellen Hauptlaufs berücksichtigt.
- Die Workflow-Datei wird am jeweiligen `head_sha` gelesen. Runs vor Einführung
  der Queue werden dadurch nicht fälschlich als fehlende Queue-Artefakte
  bewertet.
- Run-Pagination erfolgt vollständig. Bei Erreichen des GitHub-Suchlimits von
  1.000 Ergebnissen wird der Datumsbereich rekursiv geteilt.
- Jeder Run-Attempt wird separat geprüft. Ein erfolgreicher früherer Attempt
  geht bei einem späteren fehlgeschlagenen Rerun nicht verloren. Scheitert der
  neueste Attempt, wird sein partiell übernommener Queue-Stand dauerhaft als
  `PARTIAL` nachgewiesen; ein später erfolgreicher Attempt kann frühere
  fehlgeschlagene Attempts ablösen.
- Der Einzel-Check-Job muss den Scanner-Step und den Artifact-Upload-Step
  nachweisen. Ein fehlendes/abgelaufenes/ungültiges Artifact wird als Fehler
  protokolliert und niemals als leere Queue interpretiert.
- Die Queue wird nur aus dem exakt passenden Run-Artifact gelesen. Die
  Authorization-Information wird bei Redirects zu Artifact-Storage nicht
  weitergereicht.

## Idempotenz und Fehlerbehandlung

- Ticker werden über `historical_ohlcv.register_ticker()` in das gemeinsame
  Universum übernommen; bereits vorhandene Ticker sind ein No-op.
- Receipts werden erst nach erfolgreicher Übernahme aller Ticker geschrieben.
- Ein fehlgeschlagener Einzel-Check kann bereits erfolgreich registrierte
  Ticker im Artifact enthalten. Diese Ticker werden übernommen; nur Ticker, die
  erfolgreich geladen wurden, gelangen in die Queue. Der neueste fehlgeschlagene,
  abgebrochene oder übersprungene Scanner-Attempt bleibt trotzdem `PARTIAL` und
  kann nicht durch ein bereits geschriebenes Receipt stillschweigend zu
  `COMPLETE` werden.
- Registrierungsfehler werden über `queue_errors` als nachholbare Ticker
  mitgeführt. Der Hauptlauf versucht deren Registrierung erneut.
- Scheitert der Import, bleibt der Hauptlauf diagnostisch ausführbar, erhält
  aber `OHLCV_QUEUE_IMPORT=PARTIAL/FAILED`. Ein neu importierter Ticker nach
  den Scannerläufen erzwingt ebenfalls `PARTIAL`. Das Trading-Radar prüft
  zusätzlich, dass die Statusdatei zum Step-Output passt, Repository und Branch
  stimmen, `after_scanners` gesetzt ist, keine Fehler/aktiven Producer verbleiben
  und nach den Scannern keine Ticker hinzugefügt wurden. Es setzt bei Abweichungen
  `TRADING_RADAR_OHLCV_QUEUE_GATE=FAIL` und kann nicht `FINAL` werden.
- Die finale OHLCV-Synchronisierung erfasst getrennt Exitcodes für Update,
  Coverage und offene Positionen. Das Update läuft mit `historical_ohlcv.py
  update --strict`: interne Batch-Fehler, fehlende Ticker oder nicht reparierte
  Handelstagslücken liefern Exitcode 1. Der Standardaufruf ohne `--strict`
  behält sein bisheriges Best-Effort-Verhalten. Ein unvollständiges finales
  Ergebnis wird als `partial` oder `failed` ausgegeben und führt zu `NICHT_FINAL`.
- Ein Bundle-Upload-/Digest-/Integritätsfehler bleibt ein eigener
  Persistenzfehler. Die Radar-Diagnose darf weiterlaufen, aber nicht final
  freigegeben werden.

## Single-Writer-Regel

- `einzel_check.yml`: nur `contents: read`; kein Release-Upload.
- `main.yml`: einziger Writer des Releases `historical-ohlcv`; der Job ist auf
  `refs/heads/main` begrenzt, auch bei manuellem Workflow-Dispatch.
- Andere Diagnose-/Backtest-Workflows dürfen lokale OHLCV-Daten lesen oder
  temporär aktualisieren, schreiben aber nicht in das dauerhafte Release.

## Aufbewahrungsgrenze

GitHub Actions-Artefakte und Workflow-Runs haben eine begrenzte Retention.
Der Producer fordert 90 Tage an; die effektive Repository-/Organisationspolicy
kann kürzer sein. Der Hauptlauf ist daher auf zeitnahe, regelmäßig wiederholte
Queue-Übernahme ausgelegt. Fehlende oder abgelaufene Artefakte innerhalb des
Prüffensters werden als `FAILED`/`PARTIAL` erkannt. Eine lückenlose Wiederher-
stellung nach einer Pause, die länger als die tatsächliche Retention dauert,
ist allein mit Actions-Artefakten nicht garantierbar.

## Fachlicher Status

| Queue-Import | Finale Synchronisierung | Ergebnis
|---|---|---|
| COMPLETE | complete | Queue- und Sync-Gates können PASS sein
| PARTIAL / FAILED | beliebig | NICHT_FINAL
| COMPLETE | partial / failed | NICHT_FINAL
| PARTIAL / FAILED | partial / failed | NICHT_FINAL

`NICHT_FINAL` bleibt ein nachvollziehbarer Bericht und darf als Audit-Artifact
und klar markierte E-Mail ausgegeben werden. Eine reguläre Google-Drive-
Veröffentlichung setzt weiterhin einen erfolgreichen Report-Gate-Lauf und
`FINAL` voraus.
