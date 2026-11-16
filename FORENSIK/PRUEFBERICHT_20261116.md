# Forensischer Prüfbericht – Sektor-Analyse Patch 16.11.2026

## 1. Prüfgrundlage

- Repository: `joergneuber/Sektor-Analyse`
- Branch: `main`
- Baseline-Commit: `baf674f27a418b5109604387aecb71acdfcd8b28`
- Vollständiger Repository-Export: `Sektor-Analyse-main(20261116-143352).zip`
- SHA-256 Repository-Export: `3b48cc777f46aadcb1a3be08e52a51939d9bccf9099efa4955e7137747510554`
- Vorheriger Patch: `SEKTOR_ANALYSE_PATCH_NACHBESSERUNG_20261116_FINAL(2).zip`
- SHA-256 vorheriger Patch: `739b05ef4b5656eb2d0db7d44da9884c59ad39e5d1ad7bd5338713f9da5af1c6`
- Read-only Baseline-Abgleich: **17/17 Ausgangsdateien stimmen mit dem exportierten GitHub-main-Stand überein**. Bei 13 Dateien wurde der im vorherigen Patch-Manifest dokumentierte Git-Blob-SHA bestätigt; bei vier zusätzlich benötigten Dateien wurde der GitHub-Blob-SHA separat read-only abgefragt und bestätigt.

GitHub wurde nicht verändert. Es gab keine Schreibaktionen, Commits oder Uploads.

## 2. Ergebnis zu den sechs Freigabepunkten

### 1) Entwicklungsphase bei fehlender Historie

**Korrektur:** Der Trendfolge-Managementpfad wird jetzt auch bei leerer Tageshistorie aufgerufen. Fehlende Historie wird nicht länger als Grund verwendet, den Managementpfad zu überspringen und danach den normalen Stop ungeschützt auszuführen.

Managementstatus bei fehlender Historie:

`Entwicklung nicht verifizierbar | Historie fehlt | normaler Stop-Ausgang ausgesetzt`

Bei fehlendem/ungültigem Einstiegsdatum wird ebenfalls fail-closed gesperrt. Der normale Stop-Ausgang wird erst wieder zugelassen, wenn die drei gültigen abgeschlossenen Handelstage nachgewiesen sind bzw. ein bereits erreichter Entwicklungsstatus vorliegt.

**Regression:** Long und Short mit aktuellem Kurs jenseits des alten Stops und leerer Historie bleiben `Offen`; der Status enthält den ausdrücklichen Sperrgrund.

### 2) Fehlender ATR14

**Korrektur:** ATR14-Ausfall wird als eigener Managementzustand ausgewiesen, z. B.:

- `ATR14 nicht verfügbar`
- `ATR14 nicht verfügbar | TP1 erreicht`

Ohne ATR14 wird kein adaptiver oder ATR-basierter Swing-Trailing-Stop vorgetäuscht. Der bestehende Stop bleibt unverändert und wird weiterhin normal überwacht. TP1 bleibt ein informativer Hinweis.

### 3) Keine 50-%-Teilverkaufsregel

**Klarstellung und Codeprüfung:** Es gibt keine automatische 50-%-Teilverkaufsregel. Es wird weder eine Teilmenge gebucht noch eine Restposition erzeugt. TP1/TP2 lösen keine automatische Verkaufs- oder Mengenbuchung aus.

- Im geänderten `positionen_tracker.py` findet sich kein `50%`-Regelcode.
- Die Tracker-Spaltenstruktur wird nicht um Mengen-/Teilverkaufsspalten erweitert.
- Der Test prüft, dass keine Spalte ergänzt, kein Status geschlossen und kein Ausstiegskurs gesetzt wird.

### 4) `Uhrzeit/Zeitzone` als Feld

**Korrektur:** Das kombinierte Label wird in der Event-Normalisierung und im Report-Gate als Feld erkannt. Es startet keinen neuen Ereignisdatensatz. Für Legacy-Berichte ohne explizite `Ereignis:`-Zeile werden Datums-Bullets als Datensatzgrenzen erkannt; nachfolgende Uhrzeit-/Zeitzonenfelder bleiben dem jeweiligen Ereignis zugeordnet.

Explizit fehlende Uhrzeit/Zeitzone wird nicht erfunden, sondern als `NICHT VERFÜGBAR` behandelt.

### 5) M-03 und M-08 mit Generator und Gate

Es wurden sowohl synthetische Regressionen als auch eine Replay-Prüfung mit dem tatsächlichen Run-1314-Report durchgeführt.

**M-03 – tatsächlicher Run 1314:**
- Vor der Generator-Normalisierung: Gate erkennt die falsche Aussage `OHLCV vollständig, keine Datenlücken` bei `VALID=873, INVALID=1`.
- Nach `_trading_radar_ohlcv_abschnitt8_aktualisieren`: die widersprüchliche Aussage wird durch einen neutralen Verweis auf den Daten-/Prüfanhang ersetzt.
- Anschließendes Report-Gate: **keine M-03-Fehler**.
- Das Gate scannt die vollständige Reportdatei und nicht nur Abschnitt 8. Ein unzutreffender Claim in anderen Reportabschnitten wird ebenfalls erkannt.
- Ein zusätzlicher Regressionstest stellt sicher, dass eine allgemeine Aussage über Makrodatenlücken nicht fälschlich als OHLCV-Behauptung gewertet wird.

**M-08 – tatsächlicher Run 1314:**
- `Uhrzeit/Zeitzone` wird als Feld behandelt.
- Datumsbasierte Legacy-Ereignisgrenzen werden getrennt.
- Anschließendes Report-Gate: **keine M-08-Fehler**.

**Weiterhin korrekt blockiert:** Der tatsächliche Report enthält ForexFactory-Quellen mit `CACHE`. Der Event-Normalizer meldet deshalb weiterhin `Ereignis ohne verifizierte Quelle`. Diese Quelle wird nicht fälschlich als LIVE-verifiziert freigegeben.

### 6) Long-/Short- und Regressionstests

Gezielte Trendfolge-Tests prüfen:
- Long- und Short-Entwicklungsphase mit fehlender Historie und bereits erreichtem normalen Stop-Level;
- ATR14-Ausfall vor und nach TP1;
- unveränderter Stop bei fehlendem ATR14;
- keine automatische Verkaufs-/Mengenbuchung bei TP1;
- bestätigte Swing-Pivots ohne Look-ahead;
- dynamisches TP2 ohne TP2-Eintrittszwang;
- bestehende Trendfolge-Abgrenzung gegenüber Trendwende/Hebeltrader.

## 3. Testergebnisse

Zwei relevante Testbatches:

| Testbatch | Ergebnis |
|---|---:|
| Trendfolge, Market-First, Report-Gate und Produktionsintegration | **196 bestanden**, 11 Subtests |
| Trading-Radar-Datenvertrag, ältere Gate-Regressionen, Positionen-Tracker, Recovery und Datumslogik | **264 bestanden**, 19 Subtests |
| **Gesamt** | **460 bestanden**, **30 Subtests bestanden** |

Zusätzlich:
- Python `compileall` über den vollständigen Repository-Export: Exitcode 0, keine Syntaxfehler.
- AST-Parsing aller 17 geänderten Python-Dateien: bestanden.
- Top-Level-Funktionen/Klassen und Imports der vier unmittelbar betroffenen Produktionsmodule blieben gegenüber ihrer jeweiligen Ausgangsbasis unverändert.
- Es wurden genau 17 Repository-Dateien verändert; keine weiteren Dateien.

Die komplette Repository-Suite aus allen 101 Testmodulen wurde nicht vollständig ausgeführt. Die 460 Tests stammen aus den für diese Änderung relevanten Regression- und Integrationsmodulen.

## 4. Verbleibende, unabhängige Report-Blocker

Der Replay des tatsächlichen Reports bleibt **NICHT_FINAL**. Nach den hier vorgenommenen Korrekturen verbleiben:

1. **M-07:** abweichende 10Y-Treasury-Beobachtung benötigt weiterhin eine separat belegte Quelle, Abrufzeit, Datenstichtag, Einheit und Verzögerung.
2. **Ereignisquelle:** ForexFactory-Cache ist keine LIVE-Verifizierung; der Normalizer blockiert dies weiterhin korrekt.

Diese Punkte sind nicht Bestandteil der sechs angeforderten Korrekturen und wurden nicht durch Lockerung der Gates kaschiert. Der Report darf deshalb weiterhin nicht als FINAL veröffentlicht werden.

Hinweis zur Ausführung: Der externe Gemini-Erzeugungsschritt wurde nicht erneut gestartet. Der vollständige lokale Produktions-Nachbearbeitungspfad (OHLCV-Generator, Event-Normalisierung und Report-Gate) wurde mit dem tatsächlichen gespeicherten Run-1314-Report erneut durchlaufen.

## 5. Umfang und Upload-Hinweis

- 17 geänderte Repository-Dateien
- 0 neue Repository-Dateien
- 0 gelöschte Repository-Dateien
- `GITHUB_MAIN_VS_PATCH.diff` enthält den vollständigen zeilenweisen Diff gegen die Baseline.
- `PATCH_MANIFEST.json` enthält pro Datei Git-Blob-SHA, SHA-256, Byte- und Zeilenstände.

**GitHub blieb unverändert.** Der Patch ist technisch getestet, aber der tatsächliche Trading-Radar-Report bleibt wegen M-07 und der nicht live verifizierten ForexFactory-Quelle NICHT_FINAL. Die Dateien aus dem ZIP sind für die sechs angeforderten Korrekturen vorgesehen; sie sind keine Freigabe des Run-1314-Reports.
