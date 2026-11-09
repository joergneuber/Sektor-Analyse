# Forensische Codeprüfung – Short-Scanner CRV-Diagnose

Stand: 09.11.2026

## 1. Prüfgrundlage

- GitHub-Basis-Commit: `07a505ca3083d198490809cba1829d23c1250c6d`
- Basisarchiv des zuletzt ausgelieferten Berechnungsfixes: `SHORT_SCANNER_BERECHNUNGSFIX_20261109.zip`
- SHA-256 Basisarchiv: `053bbb44b5873f8929a7db32181b90efa3ca1e5238818c3407afcc945ecc16d6`
- Die darin enthaltene `short_scanner.py` stimmt bytegenau mit dem vorherigen Patchstand überein (SHA-256 `5c961ae2b4a4a4e70f5bce2666b77a1d40e1dcfcad2d6b1e76cbc653dbd190c1`).
- Workflowdatei bleibt unverändert; der bestehende Workflow führt `test_short_scanner_calculations.py` bereits aus.
- GitHub wurde nicht verändert.

## 2. Byte-/Dateiebene

| Datei | Basis | Neu |
|---|---:|---:|
| `short_scanner.py` | 50,838 Byte / 1067 Zeilen | 53,500 Byte / 1123 Zeilen |
| `tests/test_short_scanner_calculations.py` | 6,407 Byte / 179 Zeilen | 7,497 Byte / 203 Zeilen |

Alle Dateien UTF-8 ohne BOM, LF-Zeilenenden.

### SHA-256
- `short_scanner.py` vorher: `5c961ae2b4a4a4e70f5bce2666b77a1d40e1dcfcad2d6b1e76cbc653dbd190c1`
- `short_scanner.py` neu: `1cd2892e6ffaf311cb165463e931fabf908a68e9d9edd2d369ca560325d14367`
- `tests/test_short_scanner_calculations.py` vorher: `b34a8f25d090161367efbf52c6271f10d603205ee8e4a71fb08bd3624274d9a5`
- `tests/test_short_scanner_calculations.py` neu: `ddf69e7a7b2cb05e98fcc703b2499a0ae2239bf29da9b523530e77be165c52f5`
- Workflow unverändert: JA (SHA-256 `34a1e4e2f2ea9bfe39335b91a8fd62e95eef1687ce8d649c2085ab6ccade04df`)

## 3. Zeilen- und Strukturprüfung

Unified Diff: `Short_Scanner_CRV_Diagnose_20261109.diff`.

- Hinzugefügte/entfernte Diff-Zeilen (ohne Diff-Header): 120 / 40.
- Neue Funktion: `sammle_abwaerts_ziele_mit_quellen`.
- Geänderte Funktionen: `_pruefe_short_setup, main, sammle_abwaerts_ziele`.
- Entfernte Funktionen/Klassen: `keine`.
- Imports byte-/AST-seitig unverändert: JA.
- Neue Abhängigkeiten: keine.

## 4. Semantische/logische Prüfung

### Diagnoseumfang
Für jeden CRV-abgelehnten Ticker wird die Beinahe-Kandidaten-Ausgabe erweitert um:
- Einstiegskurs;
- verwendetes bestätigtes Swing-Hoch als Stop-Struktur;
- ATR14 und den 0,5-ATR-Puffer;
- tatsächlichen Stop und prozentualen Stop-Abstand;
- TP1 und TP2 mit prozentualem Abstand zum Einstieg;
- Quellen-Zuordnung der tatsächlich ausgewählten Ziele (Fib, 52-Wochen-Tief, Pivot-Swing-Tief, Ichimoku, EMA/WMA, runde Kursmarke);
- ungerundete CRV1-/CRV2-Werte und gerundete Anzeige-Werte.

### Handelslogik unverändert
- Es wurden keine CRV-Schwellen verändert: beide CRVs müssen weiterhin strikt > 1,0 sein.
- Stop-Formel bleibt bestätigtes Swing-Hoch + 0,5 × ATR14.
- TP1/TP2-Auswahl, Mindestabstand und Deduplizierung behalten ihre bisherige numerische Auswahl.
- Kein neuer Kandidatenstatus, keine Lockerung, keine neue CSV-Spalte.
- Zusatzberechnung läuft ausschließlich für Kandidaten, die bereits am CRV-Filter scheitern.
- Deduplizierung der Beinahe-Kandidaten nach Ticker bleibt bestehen.
- Reihenfolge-Sortierung bleibt anhand der bisherigen gerundeten CRV-Werte erhalten.

### Zielquellen-Zuordnung
Die bisherige Zielfunktion wurde in eine quellenbewusste Hilfsfunktion refaktoriert. Die bisherige öffentliche Funktion liefert weiterhin nur dieselbe numerische Zielliste.

Ein gezielter Paritätstest mit 100 deterministisch erzeugten OHLCV-/Indikator-Datensätzen ergab: alte und neue TP-Auswahllisten waren in allen 100 Fällen exakt identisch.

## 5. Tests und technische Validierung

- Berechnungs-Regressionstests: 10 bestanden.
- Sektorauswahl-Regressionstests: 5 bestanden.
- Vollständige Projekt-Testsuite: 316 bestanden, `OK`.
- Python-Syntaxprüfung: 139 Dateien, 0 Syntaxfehler.
- YAML-Parsing von `.github/workflows/short_check.yml`: erfolgreich.
- `py_compile` der geänderten Python-Dateien: erfolgreich.
- 100 synthetische TP-Auswahl-Paritätsfälle: bestanden.
- `ruff`/`pyflakes`: in der Ausführungsumgebung nicht installiert; daher kein externer Linter-Lauf behauptet.

## 6. Vollständigkeit und Regressionen

- Geänderte Projektdateien: genau 2.
- `short_check.yml` ist nicht geändert, da er den Berechnungstest bereits ausführt.
- Keine bestehenden Funktionen oder Imports entfernt.
- CSV-Schema unverändert.
- Keine Änderung an `analyse.py`, `trendline_utils.py` oder anderen Projektdateien.
- Die Ausgabe ergänzt ausschließlich den vorhandenen Briefing-Abschnitt für CRV-abgelehnte Beinahe-Kandidaten.

## 7. Grenzen

- Es wurde kein produktiver GitHub-Live-Scan mit Secrets/API-Zugang gestartet.
- Die Diagnosewerte für die 37 Fälle des letzten Laufs können nicht rückwirkend aus dem damaligen Log rekonstruiert werden, weil dort Swing-Hoch, ATR14 und Zielquellen nicht protokolliert wurden. Sie erscheinen im Briefing des nächsten Laufs.
- Die Diagnose entscheidet keine Strategiefrage und verändert keine Handelsfreigabe.

## 8. Uploadumfang

1. `short_scanner.py` – ersetzen.
2. `tests/test_short_scanner_calculations.py` – ersetzen.

Die Dateien aus `FORENSIK/` sind Dokumentation und optional mit hochzuladen. `.github/workflows/short_check.yml` muss nicht ersetzt werden.
