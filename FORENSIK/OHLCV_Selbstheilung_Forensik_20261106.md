# Forensische Codeprüfung – OHLCV-Selbstheilung

**Stand:** 06.11.2026  
**Repository:** `joergneuber/Sektor-Analyse`  
**Branch:** `main`  
**Änderungsziel:** Allgemeine automatische Reparatur fehlender täglicher OHLCV-Kurse  
**Ausgangsarchiv:** `Sektor-Analyse-main(20261106-053847).zip`  
**Ausgangsarchiv SHA-256:** `0007066b9d53863d622ec84d143de2b5133ab114f4418e99277bdb3c169fd389`

## 1. Byte-/Dateiebene

Alle drei bearbeiteten Originaldateien aus dem ZIP wurden vor der Änderung anhand ihres Git-Blob-SHA-1 mit der aktuellen GitHub-Version auf `main` abgeglichen. Die Hashes stimmen exakt überein. Damit ist die hochgeladene Ausgangsbasis für diese Dateien identisch mit GitHub `main`.

| Datei | Original | Neu | Original SHA-256 | Neuer SHA-256 |
|---|---:|---:|---|---|
| `historical_ohlcv.py` | 40,939 B / 888 Zeilen | 53,064 B / 1174 Zeilen | `9a495f6d52d897a433834cf9071fcc0c5ac585347bd7b8b1e8c66829c4936c2f` | `4807e5120eaa2204b204d24002c1b600d984627b018392cabce13efc2b43e9c0` |
| `.github/workflows/main.yml` | 47,489 B / 954 Zeilen | 47,560 B / 955 Zeilen | `465aed1bbff112d91556615e7e6380277e9f6482f37abf1ed01e61523c1ea835` | `a6daaa89467650b3c1d8a327de1ba11c19324b2688ec0d2e68d792d44404b2dd` |
| `tests/test_historical_ohlcv.py` | 15,073 B / 352 Zeilen | 21,277 B / 520 Zeilen | `16a131146213e969232a8ff9d529c538d6038da05c2c98344959d1d1201bc2bd` | `dc180616e004fcc6b95ead3b39d04aad1bfa4042e625b1328bd88c1eb9ab15a7` |

Alle bearbeiteten Dateien bleiben UTF-8 mit LF-Zeilenenden. Es wurden keine CRLF-Zeilenenden eingeführt.

### GitHub-Basisabgleich

| Datei | GitHub-Blob-SHA-1 der Originalversion | Ergebnis |
|---|---|---|
| `historical_ohlcv.py` | `d65b4a6a551a11e47d6c65807a6cc95876e54201` | **identisch mit GitHub `main`** |
| `.github/workflows/main.yml` | `a2f3b5af0389b57e5bcc0fe2c1021ec3c01164f8` | **identisch mit GitHub `main`** |
| `tests/test_historical_ohlcv.py` | `74896bd276e181af59838b1daaa5b35ab0b878a7` | **identisch mit GitHub `main`** |

## 2. Zeilenweiser Diff

Der vollständige Unified Diff liegt in `FORENSIK/OHLCV_Selbstheilung_20261106.diff`.

| Datei | Hinzugefügt | Entfernt |
|---|---:|---:|
| `historical_ohlcv.py` | 301 | 15 |
| `.github/workflows/main.yml` | 2 | 1 |
| `tests/test_historical_ohlcv.py` | 168 | 0 |

**Gesamt:** 471 hinzugefügte und 16 entfernte Diff-Zeilen. Die wenigen entfernten Zeilen sind Ersetzungen bzw. Anpassungen bestehender Funktions- und Workflowzeilen; keine Datei wurde ersetzt oder verkürzt.

## 3. Strukturelle Prüfung

### `historical_ohlcv.py`

- **Neu:** `_detect_recent_trading_day_gaps()` – ermittelt fehlende erwartete Sessions in einem begrenzten Zeitfenster einschließlich fehlender letzter abgeschlossener Sessions.
- **Neu:** `_insert_missing_frames()` – ergänzt ausschließlich noch nicht vorhandene `(ticker, date)`-Datensätze.
- **Geändert:** `update_database()` – führt nach Bootstrap und bisherigem Stale-Update die Lückenerkennung, Reparatur und anschließende Nachprüfung aus.
- **Geändert:** `_analyse_trading_day_gaps()` – berücksichtigt bekannte CME-Nichtveröffentlichungstage auch im Diagnose-Audit.
- **Geändert:** `_calendar_name_for_ticker()` – ergänzt ETH und weitere USD-Krypto-Paare als 24/7 sowie Kalenderzuordnungen für Korea, Dänemark und Spanien.
- **Geändert:** `main()` – ergänzt CLI-Optionen zur Steuerung der Reparatur.

Keine bestehenden Funktionen oder Klassen wurden entfernt. Das SQLite-Tabellenschema wurde nicht verändert.

### `.github/workflows/main.yml`

- Ergänzt `exchange-calendars` in der Laufzeitinstallation. Das Paket wird von der bestehenden Kalenderlogik und der neuen Lückenreparatur benötigt.
- Setzt `NMM_HISTORICAL_OHLCV_GAP_LOOKBACK_DAYS: "365"` explizit im Job.

### `tests/test_historical_ohlcv.py`

- Fünf neue Regressionstests für Einfügen ohne Überschreiben, Erkennung von Handelstagslücken, automatische Reparatur, unbekannte Kalender/Nichtveröffentlichungstage und zusätzliche Kalenderzuordnungen.
- Ein bestehender Gap-Audit-Test wurde um die CME-Nichtveröffentlichungsregel ergänzt.

## 4. Semantische und logische Auswirkungen

1. Der Standardlauf prüft die letzten **365 Kalendertage**.
2. Erwartete Handelstage werden über `exchange-calendars` bestimmt. Wochenenden und reguläre Börsenfeiertage werden nicht als Lücken behandelt.
3. Die Prüfung umfasst auch fehlende Sessions bis zum letzten abgeschlossenen Handelstag; sie beschränkt sich nicht mehr auf Lücken zwischen erstem und letztem gespeicherten Kurs.
4. Nur Ticker mit erkannten Lücken werden erneut von Yahoo Finance geladen. Die Abfragen erfolgen in vorhandenen Batches.
5. Der neue Reparaturpfad verwendet `INSERT OR IGNORE`: bereits gespeicherte Kurswerte werden durch diesen Pfad **nicht überschrieben**.
6. Nach dem Abruf wird erneut geprüft. Nicht lieferbare Daten bleiben als offene Lücken sichtbar und werden beim nächsten Lauf erneut versucht.
7. Bei nicht eindeutig zugeordneten Börsenkalendern wird nicht geraten; diese Symbole werden als nicht verifiziert ausgewiesen und nicht automatisch repariert.
8. ETH-USD und weitere Yahoo-USD-Kryptopaare werden als 24/7 behandelt.
9. Der bekannte CME-Labor-Day 2026 ohne COMEX-/NYMEX-Settlement wird nicht fälschlich als nachzuladende Kurslücke behandelt.

**Wichtige Abgrenzung:** Das bestehende Stale-Tail-Update bleibt unverändert und darf weiterhin – wie bisher – vorhandene Werte aktualisieren. Die Garantie „nicht überschreiben“ gilt gezielt für den neuen inneren Kurslücken-Reparaturpfad.

### Steuerung

- Standard: `python historical_ohlcv.py update` → Reparatur aktiv, 365 Tage.
- Zeitraum anpassen: `python historical_ohlcv.py update --gap-lookback-days 730`
- Reparatur abschalten: `python historical_ohlcv.py update --no-gap-repair`
- Workflow-Standard kann über `NMM_HISTORICAL_OHLCV_GAP_LOOKBACK_DAYS` geändert werden.

## 5. Vollständigkeit und Cross-Reference

- Neue Funktionen werden aus `update_database()` aufgerufen und in den Regressionstests direkt geprüft.
- Neue Konstante `DEFAULT_GAP_LOOKBACK_DAYS` wird von Funktionsstandard, CLI und Workflow-Konfiguration verwendet.
- `NON_PUBLISHED_DAILY_BAR_SESSIONS` wird sowohl im Reparatur-Detektor als auch im historischen Gap-Audit angewendet.
- Kalenderzuordnungen werden von Stale-Prüfung, Gap-Reparatur und Audit gemeinsam verwendet.
- Ein AST-basierter Cross-Reference-Scan der geänderten Funktionen ergab keine ungelösten Projekt-Symbole. Die angezeigten lokalen Scan-Treffer `exc` (Exception-Variablen) und `exchange_cal` (lokale Imports) sind innerhalb der jeweiligen Funktionen definiert.
- Keine Änderung an `analyse.py`, Trading-Radar-Kandidatenlogik, Handelsstrategie oder Datenbanktabellen.

## 6. Regressionen und Testläufe

| Prüfung | Ergebnis |
|---|---|
| `tests/test_historical_ohlcv.py` | **22 bestanden** |
| OHLCV + Trading-Radar-Markt-First, Prompt, Production Integration und Data Contract | **126 Tests + 19 Subtests bestanden** |
| Ergänzende Workflow-/Market-Cache-/Architekturtests | **54 bestanden** |
| Summe der gezielten Testläufe | **180 Tests + 19 Subtests bestanden** |
| Python-Kompilierung der geänderten Python-Dateien | **OK** |
| YAML-Parsing von `.github/workflows/main.yml` | **OK** |
| CLI `historical_ohlcv.py --help` | **OK** |
| Whitespace-Prüfung | **0 Zeilen mit abschließendem Leerraum** |

Ein kompletter Lauf aller 70 Testmodule wurde zusätzlich gestartet, aber durch die 60-Sekunden-Ausführungsgrenze der Prüfungsumgebung unterbrochen. Er wird daher ausdrücklich **nicht** als vollständig bestanden ausgewiesen.

## 7. Produktions-/Datenbanktest und Grenzen

Das ZIP enthält keine `historical_ohlcv.sqlite`. Ein Lauf gegen die echte 1-GB-Release-Datenbank und ein Live-Abruf von Yahoo Finance waren deshalb in dieser Prüfung nicht möglich. Die Reparatur wurde mit kontrollierten SQLite-Testdaten und simulierten Yahoo-Antworten geprüft.

Vor der produktiven Übernahme sollte der nächste GitHub-Workflow-Lauf anhand der neuen Kennzahlen `gap_found`, `gap_added`, `gap_remaining` und `gap_unverified` kontrolliert werden. Die tatsächliche Lieferbarkeit der 166 fehlenden europäischen Kurse vom 02.11.2026 kann erst dieser Live-Lauf bestätigen.

## 8. Regressionseinschätzung

**Ergebnis der lokalen Prüfung: bestanden für die gezielten Tests und statischen Prüfungen.**  
Die neue Funktion ist in den bestehenden Updateprozess integriert, verwendet das vorhandene SQLite-Schema und begrenzt automatische Reparaturen standardmäßig auf 365 Tage. Ein vollständiger Produktionsnachweis steht noch aus.

**GitHub wurde nicht verändert.** Die drei Quelldateien müssen vom Nutzer selbst in die angegebenen Repository-Pfade hochgeladen werden.
