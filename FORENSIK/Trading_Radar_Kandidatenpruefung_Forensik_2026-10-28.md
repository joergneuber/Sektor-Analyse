# Forensischer Änderungsbericht – Kandidaten-Gate
Datum: 2026-10-28

## Basis und Umfang
Basis war ausschließlich die beigefügte `Trading_Radar_Kandidatenpruefung_Patch_2026-10-28(2).zip`.
Geändert wurden `trading_radar_neuauswertung.py` und `tests/test_trading_radar_market_first.py`.
`Trading_Radar_Alleinauswertung.py` wurde nicht geändert, da die ausführbare Gate-Logik in der Basisdatei `trading_radar_neuauswertung.py` liegt; die Anweisung wurde dort synchronisiert.

## Umgesetzte Änderungen
1. Markdown-Überschriften mit `#` bis `######` werden erkannt, sowohl mit als auch ohne Doppelpunkt.
2. Die Regex verwendet horizontale Leerzeichen an Überschriften-Grenzen, damit sie nicht versehentlich in Nachbarzeilen greift.
3. Doppelte Pflicht- oder optionale Gruppenüberschriften erzeugen einen Gate-Fehler; die erste Fundstelle wird nur deterministisch weiterverarbeitet, nicht stillschweigend als gültig bewertet.
4. Die vier Pflichtgruppen müssen genau einmal vorhanden sein.
5. `VERWERFEN / INVALIDIERT` bleibt optional. Wenn vorhanden, wird die Gruppe genauso auf eindeutige Überschrift und Inhalt/Leer-Markierung geprüft wie die Pflichtgruppen.
6. Kandidaten in der optionalen Gruppe müssen `Statusgruppe: VERWERFEN / INVALIDIERT` angeben; wie bei Pflichtgruppen wird die Statusgruppe mit dem umgebenden Abschnitt abgeglichen.
7. Prompt-Anweisung aktualisiert, sodass sie die Markdown- und optionale Gruppenlogik widerspiegelt.
8. Drei Regressionstests ergänzt: Markdown-Leergruppen, doppelte Gruppenüberschrift und optionale Gruppe.

## Prüfergebnisse
- Python-Syntaxkompilierung: bestanden für beide geänderten Python-Dateien.
- Direkte Gate-Falltests:
  - Vier Markdown-Überschriften mit explizitem Nullbestand: bestanden (keine Gate-Fehler).
  - Doppelte Pflichtgruppenüberschrift: korrekt abgewiesen.
  - Optionale Gruppe mit explizitem Nullbestand: akzeptiert.
  - Kandidat in optionaler Gruppe mit passender Statusgruppe: akzeptiert.
  - Optionale Gruppe ohne Kandidat und ohne Leer-Markierung: korrekt abgewiesen.
- Vollständige Pytest-Suite: **nicht ausführbar aus dieser Patch-ZIP allein**. Die Testdatei importiert `trading_radar_market_first`, das in der ZIP nicht enthalten ist. Die Collection scheitert daher vor den Tests mit `ModuleNotFoundError`. Dies ist eine bekannte Lücke der gelieferten Patch-Basis; kein vollständiger Repository-Test wird behauptet.
- Reale Produktionsbericht-Prüfung: nicht durchgeführt, da in dieser Anfrage kein neuer Bericht nach Anwendung des Patches bereitgestellt wurde.

## Metadaten / SHA-256
| Datei | Basis SHA-256 | Neue SHA-256 | Basis Bytes | Neue Bytes | Basis Zeilen | Neue Zeilen |
|---|---|---|---:|---:|---:|---:|
| `trading_radar_neuauswertung.py` | `ecd83f2d595e2f782f3fea8abdfae6ee71a2d53909962af089fbd09d1d33aef1` | `7b7182798bde914959e19ed2a7f195d6757fb2017c21e5f53502eeae13880adf` | 33033 | 34223 | 632 | 650 |\n| `tests/test_trading_radar_market_first.py` | `e689b9b89c2393130a50b15af6491a430eecacdb1ca031165e5a16741c0d1b38` | `5652681ec04970b903c3b3b0c7435fe5372711ef92d08ec8d2e01ecf342ffdac` | 17819 | 21256 | 413 | 517 |\n\n## Diff-Zusammenfassung
Der ausführbare Gate-Code und die Regressionstests wurden geändert. Es wurden keine vollständigen Projektdateien oder Datenbestände in die Patch-ZIP kopiert. Der Produktionsstatus bleibt bis zum Lauf der vollständigen Testsuite und Prüfung eines frischen Realberichts offen.
