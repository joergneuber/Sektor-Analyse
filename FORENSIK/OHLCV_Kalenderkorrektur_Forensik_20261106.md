# Forensische Codepruefung – OHLCV-Kalenderkorrektur

Stand: 06.11.2026
Repository: `joergneuber/Sektor-Analyse`, Branch `main`
Basis-Commit: `01ea9d0606b70be4abf9acc18f26e4025968b537`

## Ergebnis

Die Diagnose vom 06.11.2026 meldete 55 fehlende Handelssitzungen bei 13 Tickern. Der Abgleich mit den offiziellen Kalendern und Settlement-Mitteilungen bestaetigt **40 dieser 55 Eintraege als Fehlalarme**:

- 36 COMEX-/NYMEX-Tage, an denen CME keine Settlementpreise veroeffentlicht (6 Tage x 6 Kontrakte)
- 2 vollstaendige Schliessungstage der Korea Exchange
- 2 FX-Feiertage (Weihnachten und Neujahr)

Damit verbleiben nach Anwendung der Korrektur **15 erwartete Handelstage mit fehlenden Bars**:

- BZ=F: 7
- ^STOXX und ^STOXX50E: je 3
- DSV.CO: 1
- PPFB.DE: 1

Die 15 verbleibenden Eintraege werden bewusst nicht ausgeblendet. Die Xetra-Tage 14.05. und 25.05.2026 waren laut Deutsche Boerse regulaere Handelstage. ICE Futures Europe weist fuer Brent an den US-Feiertagen verkleinerte Handelszeiten und Settlementfenster aus. DSV.CO und PPFB.DE lagen ebenfalls auf normalen Handelstagen.

## 1. Byte-/Dateiebene

Exakte Dateigroessen, Zeilenzahlen, SHA-256 und Git-Blob-SHA1 fuer Basis- und Neufassung stehen in `MANIFEST_SHA256.json`.

Beide Dateien sind UTF-8 mit LF-Zeilenenden. Keine Datei wurde umbenannt. Keine zusaetzliche Produktionsdatei wurde geaendert.

## 2. Zeilenweise Unterschiede

### `historical_ohlcv.py`

- 53 hinzugefuegte Diff-Zeilen, 13 entfernte Diff-Zeilen.
- Die bisherige Einzel-Ausnahme fuer den CME-Labor-Day wurde auf die durch CME belegten Settlement-Nichtpublikationstage 27.11.2025, 19.01.2026, 16.02.2026, 25.05.2026, 19.06.2026, 03.07.2026 und 07.09.2026 erweitert.
- Neue `NON_TRADING_SESSIONS_BY_CALENDAR`-Ausnahmen fuer KRX (03.06. und 17.07.2026) sowie 24/5-FX (25.12.2025 und 01.01.2026).
- Neue gemeinsame Hilfsfunktion `_expected_daily_bar_dates()`.
- Beide Erkennungspfade (`_analyse_trading_day_gaps()` und `_detect_recent_trading_day_gaps()`) verwenden nun dieselbe Filterfunktion.

### `tests/test_historical_ohlcv.py`

- 107 Zeilen hinzugefuegt, keine vorhandenen Zeilen entfernt.
- Vier Regressionstests ergaenzt: genaue Override-Liste, Audit-Kalenderverhalten, Schutz echter Handelstage und Anwendung im aktuellen Gap-Detektor.

Der vollstaendige Unified Diff steht in `OHLCV_Kalenderkorrektur_Diff.diff`.

## 3. Strukturell / AST

- `historical_ohlcv.py`: eine neue Top-Level-Funktion `_expected_daily_bar_dates`; keine Funktion oder Klasse entfernt; Imports unveraendert.
- `tests/test_historical_ohlcv.py`: vier neue Testfunktionen; bestehende Tests unveraendert; Imports ausserhalb der Testfunktionen unveraendert.
- Keine Aenderung an CLI, Datenbankschema, SQL-Upsert, Datenbank-Schreibpfaden oder Workflow-Dateien.

## 4. Semantisch / logisch

Vorher wurden generische Kalender-Sessions auch dann als erwartete Tagesbalken behandelt, wenn CME fuer COMEX/NYMEX ausdruecklich keine Settlementpreise publizierte. Ebenso fehlten Sonder-Schliessungstage im KRX- und 24/5-Kalender.

Nachher werden zwei Faelle getrennt behandelt:

1. Vollstaendige Marktschliessung (`NON_TRADING_SESSIONS_BY_CALENDAR`).
2. Markt-/Globex-Sitzung ohne taeglichen Settlement-Balken (`NON_PUBLISHED_DAILY_BAR_SESSIONS`).

Die Korrektur verhindert, dass diese Daten als reparierbare OHLCV-Luecken gemeldet oder erneut bei Yahoo nachgeladen werden. Bestehende OHLCV-Datensaetze werden weder geloescht noch ueberschrieben.

Die statische Neuauswertung der 55 Zeilen aus `OHLCV_Gap_Detail.csv` ergibt 40 entfernte Fehlalarme und 15 verbleibende erwartete Handelstage.

## 5. Vollstaendigkeit

Geaendert wurden ausschliesslich:
- `historical_ohlcv.py`
- `tests/test_historical_ohlcv.py`

`analyse.py`, `diagnose_historical_ohlcv_release.py`, `.github/workflows/main.yml`, Datenbankdateien, Secrets und Releases sind nicht Bestandteil der Aenderung.

## 6. Regressionen / Tests

Erfolgreich:
- `python -m py_compile historical_ohlcv.py tests/test_historical_ohlcv.py`
- `python -m compileall -q historical_ohlcv.py tests/test_historical_ohlcv.py`
- `PYTHONPATH=. pytest -q tests/test_historical_ohlcv.py -k 'not market_cache'`: **26 passed, 1 deselected**.

Vollstaendiger Testdatei-Lauf:
- `PYTHONPATH=. pytest -q`: **26 passed, 1 failed**.
- Der eine Fehler ist ein Umgebungs-/Paketvollstaendigkeitsproblem: `market_cache.py` fehlt im bereitgestellten lokalen Paket (`ModuleNotFoundError`). Der Test selbst betrifft `market_cache` und nicht die geaenderten Kalenderfunktionen.

Nicht durchgefuehrt:
- Kein produktiver Update-Lauf gegen die 969-MB-Release-Datenbank.
- Kein Live-Yahoo-Download.
- Kein GitHub-Workflow gestartet.

Die naechste produktive Diagnose nach dem manuellen Upload muss bestaetigen, dass die Gap-Zahl von 55 auf 15 sinkt und die 15 verbleibenden Eintraege weiterhin sichtbar sind.

## 7. Offener Restpunkt: BZ=F

Yahoo `BZ=F` steht fuer Brent Crude an ICE Futures Europe (IFEU). Die aktuelle Zuordnung in `historical_ohlcv.py` verwendet noch den `ICEUS`-Kalender als Proxy. Die sieben aktuellen BZ=F-Daten bleiben absichtlich als Luecken sichtbar: Der offizielle IFEU-Handelsplan bestaetigt an diesen Tagen Brent-Handel und Settlementfenster. Eine kuenftige eigene IFEU-Kalenderimplementierung ist sinnvoll; eine einfache Ersetzung durch `XLON` waere fachlich nicht sicher, weil ICE Brent an einzelnen Londoner Bankfeiertagen weiterhandelt.

## 8. GitHub-Abgleich und Schreibschutz

Die Basisdateien stimmen mit den GitHub-Blobs des Basis-Commits ueberein:
- `historical_ohlcv.py`: `fed843ca932587ef9daeb012388024da7561e307`
- `tests/test_historical_ohlcv.py`: `e5a20832b74fd9dc5f47a6f6ba676487a248b708`

Es wurden ausschliesslich lesende GitHub-Abfragen verwendet. Es gab keine GitHub-Schreiboperation, keinen Commit, keinen Push und keinen Workflow-Start durch den Assistenten.

## 9. Fachliche Quellen

- CME Thanksgiving 2025 Settlement Times: https://www.cmegroup.com/tools-information/holiday-calendar/files/2025/thanksgiving-holiday-settlement-times-2025.pdf
- CME MLK 2026 Settlement Times: https://www.cmegroup.com/tools-information/holiday-calendar/files/2026/mlk-day-holiday-settlement-times-2026.pdf
- CME Presidents' Day 2026 Settlement Times: https://www.cmegroup.com/tools-information/holiday-calendar/files/2026/presidents-day-holiday-settlement-times-2026.pdf
- CME Memorial Day 2026 Settlement Times: https://www.cmegroup.com/tools-information/holiday-calendar/files/2026/memorial-day-holiday-settlement-times-2026.pdf
- CME Juneteenth 2026 Settlement Times: https://www.cmegroup.com/tools-information/holiday-calendar/files/2026/juneteenth-day-settlement-times-2026.pdf
- CME Independence Day 2026 Settlement Times: https://www.cmegroup.com/tools-information/holiday-calendar/files/2026/us-independence-day-settlement-times-2026.pdf
- Deutsche Boerse Xetra trading calendar: https://www.cashmarket.deutsche-boerse.com/cash-en/trading/trading-calendar-and-trading-hours
- ICE Futures Europe 2026 Trading Schedule: https://www.ice.com/publicdocs/Trading_Schedule.pdf
- Korea Exchange holiday announcement via Yonhap: https://en.yna.co.kr/view/AEN20260520003800320
- Nasdaq Nordic trading schedule: https://www.nasdaq.com/sv/european-market-activity/trading-hours
