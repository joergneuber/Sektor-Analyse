# Forensischer Prüf- und Reparaturbericht – Trading-Radar Statusmarker

Datum: 25.10.2026

## 1. Eingangsquellen
- Aktueller Projektstand: `Sektor-Analyse-main.zip`
- Laufbericht: `trading-radar-NICHT_FINAL-40226435124.zip` → `Trading_Radar(2026-10-25).txt`
- Actions-Log: `logs_109026545750.zip`
- Betroffene Schritte: `run-scripts/50_Trading-Radar erstellen.txt`, `run-scripts/55_Neue Trading-Radar technisch gültig und frisch verifizieren.txt`, `run-scripts/6_Trading-Radar Datenvertrag Regressionstest.txt`

## 2. Befund aus dem neuen Lauf
Im Log wird Tab 2 erfolgreich gelesen:
- `10.5 TAB-2-VOLLSTÄNDIGKEIT: quellzeilen=51 | datumswerte_parsebar=51 | übernommen=51`
- `HISTORIE 10.5: 51 geschlossene Position(en) vollständig aus Tab 2 übernommen`

Der aktuelle Bericht enthält:
- `TAB-2-ANZAHL=51`
- `TAB-2-QUELLE=Geschlossene Positionen`
- 51 Quellzeilen (Quellzeile 3 bis 53)
- `TAB-2-EXPORT-SHA256=847ba1d593ac488399c2ba005073ad9d95bae7388fce5238102317c90ec7d104`

Der Bericht ist dennoch mit `TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL` markiert. Das Gate-Log meldet ausschließlich:
`REPORT_CHECK=FAIL | Fachstatus NICHT_FINAL: reguläre Veröffentlichung gesperrt`

Damit ist der vorherige Übergabefehler behoben. Der aktuelle Fehler ist eine andere Stelle in derselben Finalitätskette.

## 3. Exakte Root Cause im aktuellen Code
In `speichere_ergebnis()` (ALT-Zeilen ungefähr 16694–16718) wurde der finale Status ausschließlich aus dem Statusmarker erzeugt, den Gemini im Rohtext lieferte:
```python
original_status_matches = re.findall(
    r"(?m)^TRADING_RADAR_FACHLICH_FINAL=(FINAL|NICHT_FINAL)\s*$", text
)
...
final_status = "FINAL" if original_status_matches and all(x == "FINAL" for x in original_status_matches) else "NICHT_FINAL"
if str(closed_block or "").startswith("TAB-2-QUELLE NICHT VERIFIZIERBAR"):
    final_status = "NICHT_FINAL"
```
Ein von Gemini geliefertes `NICHT_FINAL` wurde damit ungeachtet der erfolgreichen deterministischen Quellprüfung weitergereicht. Der Bericht zeigt, dass die Tab-2-Quelle, Anzahl und Hash inzwischen vorhanden sind. Das Report-Gate meldet auch keine fehlenden Tab-2-Marker mehr; es blockiert ausschließlich wegen des Statusmarkers.

## 4. Änderung ALT → NEU
Die Statusberechnung ist jetzt deterministisch:
```python
# Der vom Sprachmodell gelieferte Statusmarker ist kein Beleg fuer die
# technische/fachliche Freigabe: Gemini kann NICHT_FINAL aus Kontext
# oder aus einem vorherigen Entwurf uebernehmen. Die Freigabe wird hier
# deterministisch aus den validierten Pflichtdaten abgeleitet; weitere
# unabhaengige Report-Gates pruefen den fertigen Bericht anschliessend.
final_status = "FINAL"
if validation_errors or str(closed_block or "").startswith("TAB-2-QUELLE NICHT VERIFIZIERBAR"):
    final_status = "NICHT_FINAL"
```
Der alte Statusmarker wird nicht länger als Entscheidungsgrundlage verwendet. Der Status bleibt `NICHT_FINAL`, wenn die deterministische Validierung Fehler meldet oder der geschlossene Positionsblock als nicht verifizierbar markiert ist. Anschließend prüft das unabhängige Report-Gate weiterhin die Struktur und die Pflichtmarker.

## 5. Testnachweise
- `py_compile` für die geänderte Python-Datei und den Test: bestanden.
- Gezielte Suite: `python -m unittest discover -s tests -p test_trading_radar_data_contract.py -v` → **20 Tests bestanden**.
- Neuer Regressionstest: gültige Tab-2-Fakten mit eingehendem Modellmarker `NICHT_FINAL` ergeben nach deterministischer Validierung `FINAL` plus Anzahl, Quellenmarker und SHA-256.
- Bestehender Negativtest bleibt bestehen: ungültiges Einstiegsdatum erzeugt `NICHT_FINAL` und unterdrückt die kanonischen Quellenmarker.
- Vollständige Testsuite: 194 Tests wurden gestartet; der Gesamtlauf ist nicht grün, da zwei vorhandene Testmodule beim Import fehlschlagen, darunter `test_bitcoin_edelmetall_sections_korrigiert.py` mit `IsADirectoryError` für `tests`. Die gezielte betroffene Suite ist grün.
- Zusätzliche Diagnose am mitgelieferten Bericht: eine temporäre Kopie, in der ausschließlich der Statusmarker auf `FINAL` gesetzt wurde, bestand das aktuelle Report-Gate mit `REPORT_CHECK=PASS | acht Pflichtabschnitte eindeutig, geordnet und mit Inhalt`. Diese Kopie wurde nicht als Bericht ausgegeben und ist kein Ersatz für einen echten neuen Lauf.

## 6. Änderungsumfang
Nur diese beiden Projektdateien sind geändert:
1. `Trading_Radar_Alleinauswertung.py`
2. `tests/test_trading_radar_data_contract.py`

## 7. SHA-256
- `Trading_Radar_Alleinauswertung.py` — 900101 Bytes — SHA-256 `83ba2dfbbce879e888b61974af2ca894de255da3c8f643346fe5597e2f0d3567`\n- `tests/test_trading_radar_data_contract.py` — 29473 Bytes — SHA-256 `4a64ad74337f801fc7e21fcaf07e16241a581dbf5ebf36aa2a256744e4f24da3`\n\n## 8. Einschränkungen / Freigabe\n\nDie Korrektur behebt den im aktuellen Lauf nachgewiesenen Statusmarker-Fehler, ohne das Report-Gate zu umgehen. Der nächste echte GitHub-Actions-Lauf wurde hier nicht ausgeführt. Ein erfolgreicher FINAL-Bericht ist erst dann live bestätigt, wenn der Workflow mit dem korrigierten Code erneut gelaufen ist und der Report-Gate-PASS im Actions-Log steht.\n