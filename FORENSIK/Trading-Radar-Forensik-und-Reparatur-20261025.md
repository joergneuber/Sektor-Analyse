# Forensischer Reparaturbericht – Trading-Radar

**Basis:** `Sektor-Analyse-main(20261025-141709).zip`  
**Umfang:** vier geänderte Dateien; keine Änderungen an Scanner-, Entry-/Exit-, Positionsmanagement-, Workflow- oder E-Mail-Logik.  
**Ziel:** die vier benannten Report-Gate-Punkte dauerhaft und prüfbar absichern.

## 1. Befund und Korrektur

### Punkt 1 – Tab-2-Quellen-/Exportzählung und Fingerabdruck

**Befund:** Der Gate-Regex erfasste den Abschnitt 10.5 bis zum Statusmarker bzw. Dateiende. Weil Kapitel 11 hinter 10.5 steht, konnten Methodik-/Datenanhang-Zeilen in den SHA-256-Prüfbereich geraten. Der Generator berechnete den Fingerabdruck dagegen aus dem deterministischen Tab-2-Block allein. Das war ein inkonsistenter Prüfvertrag.

**Korrektur:**
- `trading_radar_report_gate.py`: Abschnitt 10.5 endet nun an einer eigenständigen Kapitel-11-Überschrift (oder ersatzweise am Statusmarker/Dateiende).
- Der Gate-Check verlangt ausdrücklich `TAB-2-QUELLE=...` und prüft die ausgewiesene Anzahl gegen konkrete Ticker-Zeilen.
- `Trading_Radar_Alleinauswertung.py`: vollständig leere Sheet-Zeilen werden nicht als Positionsdatensätze gezählt. Jede befüllte Zeile ohne Ticker löst einen Fehler aus, statt eine irreführende Anzahl bzw. einen scheinbar gültigen Export zu erzeugen.
- SHA-256 bleibt über die tatsächlich gespeicherten Exportzeilen berechnet; doppelte Positionszeilen und abweichende Fingerabdrücke bleiben Gate-Fehler.

**Regression:** `test_m05_fingerprint_excludes_following_chapter_11` beweist, dass Kapitel 11 nicht in den Export-Fingerabdruck einfließt. Der bestehende Fingerabdruck-Mismatch-Test bleibt bestehen.

### Punkt 2 – M-06 Performanceangaben

**Befund:** Der frühere Writer prüfte hauptsächlich, ob Kennzahlbezeichnungen vorkamen. Vorhandene Platzhalter konnten dadurch den Ergänzungspfad umgehen. Das Gate akzeptierte außerdem eine explizite Nichtverfügbarkeitsformulierung an anderer Stelle, obwohl das beschriftete Feld selbst einen Platzhalter enthalten konnte.

**Korrektur:**
- Der finale Report-Writer normalisiert Abschnitt 7 unmittelbar vor dem Speichern.
- Portfolio-Gesamtperformance und durchschnittliche Positionsperformance werden getrennt geprüft.
- Ein vollständiger numerischer Wert bleibt erhalten. Ein fehlender oder ungültiger Wert wird durch `NICHT VERFÜGBAR (keine belegte Berechnungsgrundlage)` ersetzt; es wird nichts geschätzt.
- Das Gate prüft jedes beschriftete Vorkommen. `TBD`, `Platzhalter` oder ein sonstiger ungültiger Wert kann nicht durch einen anderen Satz im Abschnitt „gerettet“ werden.

**Regression:** `test_m06_rejects_placeholder_even_if_unavailable_prose_also_exists` sowie `test_report_field_normalizer_explicitly_marks_missing_m06_m08_values`.

### Punkt 3 – M-08 Ereignisdetails

**Befund:** Der Prompt verlangte die Felder, aber die gespeicherte Ausgabe wurde nicht deterministisch auf alle Pflichtfelder vervollständigt. Fehlende Felder blieben damit vom Gemini-Output abhängig.

**Korrektur:**
- Der finale Report-Writer ergänzt in Abschnitt 3 fehlende Uhrzeit, Zeitzone, Kennzahl, Berichtszeitraum, letzten Wert und Konsens als ausdrücklich `NICHT VERFÜGBAR`, wenn die Quelle keinen belegten Wert liefert.
- Vorhandene ungültige Platzhalter werden ersetzt. Bereits vorhandene belegte Angaben werden nicht durch erfundene Werte überschrieben.
- Das Gate akzeptiert explizite Nichtverfügbarkeit für jedes einzelne Feld, prüft Zeitzonen aber weiterhin gegen zulässige Zeitzonenformate, sofern ein echter Wert angegeben wird.
- Ein expliziter Satz „Keine relevanten Ereignisse im geprüften Zeitraum“ bleibt als alternative gültige Ausgabe zulässig.

**Regression:** `test_m08_accepts_explicit_unavailable_status_for_each_event_field`; vorhandene Tests für fehlende/ungültige Ereignisdetails und „keine relevanten Ereignisse“ bleiben bestehen.

### Punkt 4 – Gate und Regressionen

Die Reparatur wird an drei Ebenen abgesichert: Quellzeilenvalidierung beim Lesen von Tab 2, deterministische Normalisierung des gespeicherten Berichts und unabhängige Gate-Prüfung. Die Regressionstests prüfen gezielt den zuvor übersehenen Fehlerzustand sowie die Schutzwirkung gegen ungültige Werte.

## 2. Geänderte Dateien und Fingerabdrücke

| Datei | Alte Zeilen | Neue Zeilen | Alte SHA-256 | Neue SHA-256 |
|---|---:|---:|---|---|
| `Trading_Radar_Alleinauswertung.py` | 17051 | 17179 | `ca2126250d074f72cca3e39c5a2e00aab8c2c2a862b6dbe062845e508f0f1d7b` | `75af8bdfd6d70bf2f9273bd92c5875133150f9a5b1e80833748133a8afa37036` |
| `trading_radar_report_gate.py` | 341 | 391 | `f8fda686b3271403640a281c21d9d2ee2eed1fd9a41159d0c04dc8c72e80d2f1` | `44123f6fd49c9317a9bdcb88f110c7ad1cf610830a1c21fffcc89fad577f81e7` |
| `tests/test_trading_radar_report_gate.py` | 173 | 202 | `6ffd2350c961a02c2d3bd463af0984c3113efc3d634839d111d3e19aab5ed501` | `19c3586a609907b366f61baf23c794175937abec0e18aa296938bb4ccdcea2f7` |
| `tests/test_trading_radar_data_contract.py` | 246 | 262 | `b79fb845e5fad43730934bc4b7a2c0ce009d056246ccd3fc47e1be3d1cab6710` | `6dc0beb5eee5c8828806d9949be0e13cd2c9ab09aee97994bd42d747b6231d1b` |

## 3. Gezielte Prüfung und Teststatus

- `py_compile` für beide geänderten Produktionsmodule: **PASS**.
- AST-Parsing aller **91 Python-Dateien** im Projekt: **PASS**, keine Syntax-/AST-Fehler.
- Gezielt Report-Gate, Datenvertrag und Mailversand: **42 Tests bestanden**.
- Vollständige Testentdeckung: zwei bereits in der Anlage vorhandene Testmodule haben unabhängige Pfad-/Importfehler beim Auflösen ihrer Quell-Datei. Diese Tests sind nicht durch die Änderungen verursacht und wurden nicht stillschweigend repariert.
- Übrige Suite nach Ausschluss dieser fehlerhaften Testmodule: **174 Tests bestanden, 0 Failures, 0 Errors**.
- Es erfolgte kein echter Google-Drive-, Gemini-, SMTP- oder GitHub-Actions-Produktionslauf. Der Nachweis belegt lokale Code-/Regressionstests, nicht die erfolgreiche Live-Zustellung oder einen `FINAL`-Report mit den aktuellen Live-Daten.

## 4. Cross-Reference und Scope

- Neue Funktion `_trading_radar_berichtsfelder_normalisieren`: genau eine Produktionsdefinition und ein Aufruf in `speichere_ergebnis`; zusätzlich direkte Testreferenz.
- Lokale Hilfsfunktionen `performance_field_ok` und `event_field_ok` sind innerhalb der Gate-Validierung definiert und nur dort aufgerufen.
- Keine Imports oder öffentlichen Schnittstellen wurden hinzugefügt.
- Geänderte Symbole/Verhalten wurden durch die gezielten Tests abgedeckt; alle übrigen Projektdateien sind im Patch unverändert.
- Kein Scanner-Scoring, keine Entry-/Exit-Regeln, kein Portfolio-/Positionsstatus, kein Workflow und keine Versandfreigabe wurden verändert.

## 5. Restbedingungen / fachliche Grenze

Die Software kann fehlende Quellenfelder sichtbar als `NICHT VERFÜGBAR` markieren. Das bedeutet nicht, dass die Daten inhaltlich vorhanden sind. Ein solcher Bericht kann weiterhin `NICHT_FINAL` bleiben und muss dann vom Gate für reguläre Veröffentlichung blockiert werden. Ein `FINAL`-Status setzt weiterhin die vollständigen fachlichen Anforderungen und ein erfolgreiches Gate voraus.
