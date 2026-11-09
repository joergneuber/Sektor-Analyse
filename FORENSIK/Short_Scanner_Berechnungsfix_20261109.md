# Forensische Codeprüfung – Short-Scanner-Berechnungslogik
Stand: 09.11.2026

## 1. Prüfgrundlage und Umfang

- Grundlage: vom Nutzer bereitgestelltes Archiv `Sektor-Analyse-main(20261109-083114).zip`
- SHA-256 des Ausgangsarchivs: `4f0cf9ec2de5aa28ec7071d48789b075ffe667c5136dfa81b669e9675de9181d`
- Alle Dateien des Archivs wurden mit dem entpackten Arbeitsstand verglichen.
- Ergebnis: genau **3 Projektdateien** geändert/neu; keine ursprüngliche Archivdatei fehlt.
- `analyse.py`, `trendline_utils.py`, `ohlcv_quality_gate.py` und alle übrigen Projektdateien blieben byteidentisch.
- Es wurden keine Änderungen an GitHub vorgenommen.

## 2. Byte-/Dateiebene

| Datei | Ausgangsgröße | Neue Größe | Ausgangszeilen | Neue Zeilen |
|---|---:|---:|---:|---:|
| `short_scanner.py` | 46.271 Byte | 50.838 Byte | 937 | 1.067 |
| `.github/workflows/short_check.yml` | 2.259 Byte | 2.376 Byte | 55 | 57 |
| `tests/test_short_scanner_calculations.py` | – | 6.407 Byte | – | 179 |

- Alle drei Dateien sind UTF-8 ohne BOM und verwenden LF-Zeilenenden.
- `short_scanner.py` SHA-256 vorher: `f37334d6bbdd077b84ba55d25144d1bdba8bcf24b3269eb62b735b70256348e0`
- `short_scanner.py` SHA-256 neu: `5c961ae2b4a4a4e70f5bce2666b77a1d40e1dcfcad2d6b1e76cbc653dbd190c1`
- Workflow SHA-256 vorher: `400efe0331b14dab07c4b8840f103529489a73d5eb4c3d5f5997bba01f124d17`
- Workflow SHA-256 neu: `34a1e4e2f2ea9bfe39335b91a8fd62e95eef1687ce8d649c2085ab6ccade04df`
- Neuer Test SHA-256: `b34a8f25d090161367efbf52c6271f10d603205ee8e4a71fb08bd3624274d9a5`
- Unified Diff im Paket: `FORENSIK/Short_Scanner_Berechnungsfix_20261109.diff`
- Diff-Umfang einschließlich neuem Test: 336 hinzugefügte und 25 entfernte Zeilen.

## 3. Strukturelle Prüfung (AST)

Neue Funktionen:
- `berechne_atr14`
- `get_swing_highs_above`
- `berechne_strukturellen_stop_short`
- `waehle_short_ziele`
- `berechne_short_crv`
- `short_crv_erfuellt`

Geänderte Funktionen:
- `_pruefe_short_setup`
- `check_kumo_breakdown`
- `check_pullback_zone_short`
- `get_swing_lows_below`
- `sammle_abwaerts_ziele`
- `main` (Funnel-Reihenfolge und Briefingtext)

Entfernte Funktionen: keine.
Imports: AST-Vergleich bestätigt, dass die Importliste unverändert ist. Es werden keine neuen externen Abhängigkeiten eingeführt.

## 4. Semantische/logische Änderungen

### 4.1 Stop

Vorher:
- höchstes Hoch der letzten zehn Kerzen × 1,02;
- kein ATR-Bezug und keine bestätigte Pivot-Struktur;
- kein Fail-closed-Verhalten, wenn eine sinnvolle Struktur fehlt.

Neu:
- nächstes bestätigtes Swing-Hoch oberhalb des Einstiegs;
- Stop = Swing-Hoch + 0,5 × ATR14;
- Pivot-Bestätigung mit `order=5`, also fünf Kerzen links und rechts;
- ohne bestätigtes Hoch oder gültige ATR wird das Setup mit `kein_struktureller_stop` abgelehnt.

Es wurde **kein zusätzlicher pauschaler Maximalrisiko-Prozentsatz** eingeführt. Die bestehenden CRV-Anforderungen bleiben der Qualitätsfilter.

### 4.2 TP1 und TP2

- Keine 8-%-/12,6-%-Fallbacks mehr.
- TP1 und TP2 müssen aus der bestehenden Sammlung charttechnischer Abwärtslevel stammen.
- Ohne zwei reale Zielkandidaten wird das Setup mit `zu_wenig_abwaertsziele` abgelehnt.
- Nichtpositive, nichtendliche und oberhalb des Einstiegs liegende Zielwerte werden ausgeschlossen.
- Die bestehende Reihenfolge bleibt: nächstes Ziel = TP1, nächstes ausreichend getrenntes Ziel = TP2.

### 4.3 CRV

- Berechnung in einer eigenen Funktion mit Prüfung der Kursgeometrie.
- Entscheidung erfolgt anhand der ungerundeten CRV-Werte.
- Beide Bedingungen sind strikt: `CRV1 > 1.0` und `CRV2 > 1.0`.
- Rundung auf zwei Stellen erfolgt nur für die Ausgabe.
- Ungültige Kurs-/Stop-/Zielgeometrie wird mit `ungueltige_crv_basis` abgelehnt.

### 4.4 Pivot-Bestätigung / Randpunkte

Bei `scipy.argrelextrema()` kann der Standardmodus `clip` Extrempunkte nahe dem Datenrand liefern, obwohl nicht genügend rechte Bestätigungskerzen vorliegen.

Deshalb werden Randindizes nun explizit ausgeschlossen:
- Swing-Hochs für den Stop;
- Swing-Tiefs für die Abwärtsziele;
- Swing-Hochs in der Lower-High-Prüfung des Pullbacks.

Damit werden unbestätigte aktuelle Randpunkte nicht als bestätigte Struktur verwendet.

### 4.5 Kumo-Breakdown

Der vorherige Schlusskurs muss jetzt oberhalb der **oberen** Wolkengrenze liegen. Ein vorheriger Kurs innerhalb der Wolke reicht nicht mehr als Beleg für einen frischen Breakdown von oben.

### 4.6 Funnel und Briefing

Neue Ablehnungsgründe:
- `kein_struktureller_stop`
- `zu_wenig_abwaertsziele`
- `ungueltige_crv_basis`

Der Workflow führt zusätzlich die neuen Berechnungs-Regressionstests aus. Die bestehenden Funnel- und CSV-Spalten bleiben ansonsten erhalten.

## 5. Regressionstests und technische Validierung

- Neue Berechnungstests: **9 bestanden**
- Bestehende Short-Sektor-Auswahltests: **5 bestanden**
- Vollständiger vorhandener Projekt-Testbestand: **315 Tests bestanden**
- Python-Syntaxprüfung: **139 Python-Dateien ohne Syntaxfehler**
- YAML-Parsing von `short_check.yml`: erfolgreich
- AST-Vergleich: Importliste unverändert; keine entfernten Funktionen
- Vollständiger Archivabgleich: genau zwei bestehende Dateien geändert, eine Testdatei neu; keine fehlenden Originaldateien

Die vollständige Testsuite wurde mit `unittest discover` ausgeführt und endete mit `OK` (315 Tests).

## 6. Inhalt der neun neuen Berechnungstests

1. Stop wird aus dem nächstgelegenen bestätigten Swing-Hoch plus 0,5 ATR berechnet.
2. Ohne bestätigten Widerstand entsteht kein Stop.
3. Ein noch nicht rechts bestätigtes aktuelles Hoch wird nicht als Stop verwendet.
4. Ein noch nicht rechts bestätigtes aktuelles Tief wird nicht als Ziel verwendet.
5. ATR14 ist positiv und benötigt ausreichende Historie.
6. Kumo-Breakdown aus dem Wolkeninneren wird abgelehnt; Durchbruch von oberhalb wird akzeptiert.
7. CRV wird ungerundet bewertet; Werte knapp unter 1,0 bestehen nicht.
8. TP-Auswahl verlangt zwei reale Zielkandidaten.
9. Zielsammlung gibt keine nichtpositiven Kurswerte zurück.

## 7. Grenzen der Prüfung

- Es wurde kein Live-Short-Scan mit GitHub-Secrets/API-Zugang ausgeführt. Der erste produktive Lauf nach dem Upload muss deshalb zusätzlich kontrolliert werden.
- Der vollständige Testbestand lief lokal erfolgreich; ein dedizierter externer Linter (`ruff`/`pyflakes`) war in der Ausführungsumgebung nicht installiert.
- Die Änderungen betreffen nur die Short-Scanner-Berechnung. Eine Aussage über die Profitabilität der Strategie ist daraus nicht ableitbar; dafür ist ein historischer Backtest mit den neuen Stop-/Zielregeln erforderlich.

## 8. Übergabe

Zu GitHub hochzuladen sind ausschließlich:
1. `short_scanner.py`
2. `.github/workflows/short_check.yml`
3. `tests/test_short_scanner_calculations.py`

Die beiden Forensikdateien dienen der Dokumentation und können optional zusätzlich im Projektordner `FORENSIK/` abgelegt werden. Der Nutzer führt den GitHub-Upload selbst aus.
