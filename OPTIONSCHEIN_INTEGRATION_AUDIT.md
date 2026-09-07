# Optionsschein-Integration – Abschlussaudit

Stand: 07.09.2026

## 1. Ausgangsbasis

Die vom Nutzer bereitgestellten Vorgänger-ZIPs vom 06.09. und 07.09. sind byte-identisch (gleicher SHA-256). Dieser gemeinsame Vorgängerstand ist die Vergleichsbasis.

## 2. Änderungsumfang

Gegenüber dem gemeinsamen Vorgängerstand sind fachlich genau sieben Dateien betroffen:

### Geändert
- `.github/workflows/main.yml`
- `positionen_tracker.py`
- `offene_positionen_check.py`
- `Offene_Positionen.csv`

### Neu
- `os_kurse.py`
- `tests/optionsschein_selftest.py`
- `OPTIONSCHEIN_INTEGRATION_AUDIT.md`

`Geschlossene Positionen_Initialbestand.csv` bleibt byte-identisch zum Vorgänger und ist ausdrücklich nicht Teil der Änderung.

Alle übrigen Ausgangsdateien sind byte-identisch.

## 3. Kurslogik

Kurslogik je offener Optionsscheinposition:

1. Abruf der WKN über die Börse Stuttgart.
2. Wenn ein Geldkurs vorhanden ist, wird der Geldkurs als aktueller Optionsschein-Kurs verwendet.
3. Wenn kein Geldkurs vorhanden ist, wird der letzte echte Preis verwendet.
4. Wenn weder Geldkurs noch letzter Preis vorhanden sind, wird **kein Kurs** eingetragen.
5. In diesem Fall bleiben `OS_Aktueller_Kurs` und `OS_Performance%` leer und `OS_Quelle` wird auf `nicht_verfügbar` gesetzt.
6. Es gibt keine manuelle OS-Kursquelle und keine Schätzung über `Hebel × Aktienperformance`.

Ein automatischer Abruf beginnt mit dem Löschen der automatisch gepflegten Kursfelder der betreffenden Zeile. Dadurch kann ein alter automatischer Kurs nicht als aktueller Kurs stehen bleiben.

## 4. Optionsschein-Felder

Zusätzlich werden automatisch geführt:

- `OS_Aktueller_Kurs`
- `OS_Geld`
- `OS_Brief`
- `OS_Spread`
- `OS_Kurszeit` – der von der Quelle gelieferte Kurszeitstempel des verwendeten Kurses, insbesondere des Geldkurses.
- `OS_Kursquelle`

Erhalten bleiben:

- `OS_Einstiegskurs`
- `OS_Performance%`
- `OS_Quelle`
- `OS_WKN`
- `Produkt_Typ`
- `Emittent`
- `Hebel`

`OS_Manueller_Kurs` wurde aus dem aktiven Schema entfernt.

## 5. Übergabe an „Offene Positionen+Check“

`offene_positionen_check.py` übernimmt die Optionsschein-Felder in die Ausgabe und in die historische Ausgabedatei.

Die lokale CSV-Ausgabe bleibt semikolon-getrennt, UTF-8 mit BOM und verwendet das deutsche Dezimal-Komma. Numerische Optionsschein-Felder werden in Google Sheets als Zahlen mit zwei Nachkommastellen formatiert.

Der historische Initialbestand wird nicht überschrieben oder umgebaut; beim Einlesen werden die benötigten historischen Felder in das aktuelle Ausgabeschema übernommen.

## 6. Regressionstest

`tests/optionsschein_selftest.py` enthält zehn deterministische Teststufen:

1. Deutscher Zahlenparser
2. Geldkurs-Priorisierung + Kurszeit
3. Last-Price-Fallback
4. Kein echter Kurs → kein Ersatzkurs
5. Automatischer Kurs ist alleinige Quelle
6. Automatischer Kurs liefert echte Performance
7. Kein echter Kurs → keine Schätzung / keine Performance
8. Kein Stale-Data-/Schätzungs-Fallback auf Ebene der Berechnungsfunktion
9. Deutscher CSV-Roundtrip
10. Tatsächlicher lokaler Übergabepfad `Offene_Positionen.csv` → `Offene Positionen+Check.csv` mit sieben Optionsscheinpositionen

Ergebnis: `OPTIONS-SCHEIN SELFTEST: PASS` (10/10).

Zusätzlich wurden die betroffenen Python-Dateien kompiliert und der gesamte Projektbaum mit `python -m compileall -q .` geprüft.

## 7. Testabdeckung der sieben Optionsscheine

Der Übergabetest deckt diese sieben WKNs ab:

- `UN68EW`
- `PM0216`
- `PM026C`
- `PK9UHB`
- `GW5GT1`
- `UN4N9T`
- `HM0ZHD`

Die Testdaten verwenden ausschließlich automatische Optionsschein-Kurse.

## 8. Zeitstempel

Wenn die Quelle einen `Kurszeit`-Wert liefert, wird dieser in `OS_Kurszeit` übernommen. Der Selbsttest prüft dies ausdrücklich anhand eines repräsentativen Stuttgart-HTML-Snippets.

## 9. Kein-Kurs-Regel

Wenn weder Geldkurs noch letzter Preis verfügbar sind, wird kein erfundener oder geschätzter Kurs geschrieben. Auch ein alter automatisch gepflegter Kurs wird nicht weiterverwendet. Die Felder für aktuellen Kurs, Geld, Brief, Spread, Kurszeit, Kursquelle und Performance werden in diesem Fall geleert; `OS_Quelle` wird auf `nicht_verfügbar` gesetzt.

Wenn ein echter Kurs vorhanden ist, aber `OS_Einstiegskurs` fehlt oder ungültig ist, bleibt der echte aktuelle Kurs erhalten; nur die Performance bleibt leer.

## 10. Workflow

Der Regressionstest läuft im GitHub-Workflow unmittelbar vor `python positionen_tracker.py`. Bei einem Testfehler endet der Job vor dem produktiven Positionslauf.

## 11. Historischer Initialbestand

`Geschlossene Positionen_Initialbestand.csv` bleibt byte-identisch zum gemeinsamen Vorgängerstand. Die Optionsschein-Integration verändert diese historische Faktenbasis nicht.

## 12. Nicht geändert

Keine Änderung an den übrigen Analyse-, Scanner-, Makro-, Benchmark- oder Upload-Dateien.
