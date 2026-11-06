# Forensische Codeprüfung – IFEU-Kalenderkorrektur
**Datum:** 2026-11-06  
**Repository-Grundlage:** `joergneuber/Sektor-Analyse`, Branch `main`  
**Verglichener GitHub-Stand:** Commit `a9e0eba16d237cef2e4c6902b07fa825a06505fd`  
**Ziel:** `BZ=F` (ICE Brent Futures) nicht länger mit dem `ICEUS`-Proxy, sondern mit einem ausdrücklich auf ICE Futures Europe (IFEU) bezogenen Kalender prüfen.

## 1. Byte-/Dateiebene

Die beiden Eingangsdateien aus dem ZIP stimmen bytegenau mit dem GitHub-Stand überein. Die Git-Blob-SHAs wurden gegen die aktuellen GitHub-Dateien verifiziert.

| Datei | Bytes | Zeilen | SHA-256 | Git-Blob-SHA-1 | Encoding | Zeilenenden |
|---|---:|---:|---|---|---|---|
| historical_ohlcv.py – Basis | 54,662 | 1,215 | `c764c7589bf3b20cf3817d9f59b8a56ebc95a6a4d963cfdfff1fb263e1cb371a` | `a940348e28b031249b38b9f6bbe38162745bc35e` | UTF-8 | LF |
| historical_ohlcv.py – neu | 57,646 | 1,275 | `a1094642ab5d0d89e95f3231eb70f42a2dafc53f7ba868886f681c488cc8acb9` | `ec77345ff68f9525ce5eedc5d34813308983c8ec` | UTF-8 | LF |
| tests/test_historical_ohlcv.py – Basis | 27,441 | 671 | `999a6230c651bb7e4b8f5d434679eae475d8149a9478675cb24267824674ea2b` | `04f4bc3d54aa14ab00d8de5b5a622919e8bf8224` | UTF-8 | LF |
| tests/test_historical_ohlcv.py – neu | 32,587 | 813 | `6ce83589b5a20f06d126588a4c53c45b35ef3ea239192f58a939229dc34e53bb` | `89bb1371359058869313dfcdd6f8877df7a3e455` | UTF-8 | LF |
Alle vier Fassungen sind UTF-8 mit LF-Zeilenenden. Es wurden keine CRLF-Zeilenenden gefunden.

## 2. Zeilenweise Unterschiede

Die vollständigen Unified-Diffs für beide Dateien stehen in `IFEU_Kalenderkorrektur_Diff_20261106.diff`.

| Datei | Hinzugefügt | Entfernt |
|---|---:|---:|
| `historical_ohlcv.py` | 95 | 35 |
| `tests/test_historical_ohlcv.py` | 143 | 1 |

Wesentliche geänderte Codebereiche in `historical_ohlcv.py`:
- Zeile 76: Zuordnung `BZ=F`: `ICEUS` → `IFEU`.
- Zeilen 90–107: IFEU-Verifizierungszeitraum und explizite vollständige Schließtage.
- Zeilen 163–187: neue Funktion `_calendar_session_dates()`; IFEU wird nicht an `XLON`, `ICEUS` oder einen anderen Proxy delegiert.
- Zeilen 208–223: `_previous_completed_session_date()` verwendet die gemeinsame Kalenderfunktion und liefert außerhalb des verifizierten Zeitraums keinen geratenen Handelstag.
- Zeilen 229–349: `_analyse_trading_day_gaps()` verwendet die IFEU-Sitzungen und markiert Historie außerhalb der verifizierten Grenzen als unvollständig.
- Zeilen 643–710: `update_database()` fällt bei nicht verifizierbarem IFEU-Kalender nicht still auf einen generischen Business-Day-Kalender zurück.
- Zeilen 861–950: `_detect_recent_trading_day_gaps()` verwendet denselben IFEU-Kalender.
- Zeile 1149: `audit_snapshot()` kann bei unvollständiger Kalenderabdeckung keinen Status `complete` melden.

In `tests/test_historical_ohlcv.py` wurde die erwartete BZ=F-Kalenderzuordnung angepasst und sechs neue Regressionstests ergänzt.

## 3. Strukturelle Prüfung

- Neu: `_calendar_session_dates(calendar_name, start_date, end_date)`.
- Geändert: `_previous_completed_session_date()`.
- Geändert: `_analyse_trading_day_gaps()`.
- Geändert: `_detect_recent_trading_day_gaps()`.
- Geändert: `update_database()`.
- Geändert: `audit_snapshot()`.
- Konstanten ergänzt: `IFEU_CALENDAR_VERIFIED_END`, `IFEU_CLOSED_SESSIONS`; IFEU in `CALENDAR_EARLIEST_DATES`.
- Es wurden keine Klassen entfernt oder neu eingeführt.
- Keine neue externe Abhängigkeit.
- `exchange_calendars` bleibt für alle bisherigen Kalender erhalten; nur IFEU erhält einen eigenen, expliziten Kalenderpfad.

## 4. Semantische/logische Prüfung

### IFEU-Regel

`BZ=F` wird auf `IFEU` abgebildet. Der Kalender verwendet werktägliche Sessions ab 2025-01-01 bis einschließlich 2027-12-31, abzüglich der in den offiziellen ICE-Unterlagen bestätigten vollständigen Schließtage:

- 2025-01-01, 2025-04-18, 2025-12-25
- 2026-01-01, 2026-04-03, 2026-12-25
- 2027-01-01, 2027-03-26

Verkürzte Sessions bleiben Handelstage. Insbesondere werden US-Feiertage nicht pauschal ausgeschlossen. Auch UK-Bankfeiertage wie Easter Monday, May/Spring Bank Holiday, Summer Bank Holiday und Boxing Day observed werden nicht pauschal als Brent-Schließtage behandelt.

### Verifizierungsgrenzen

- Vor 2025-01-01 und nach 2027-12-31 wird keine vollständige IFEU-Kalenderabdeckung behauptet.
- Historische OHLCV-Daten bleiben erhalten und sichtbar.
- Die Diagnose kennzeichnet solche Daten als `calendar_coverage_incomplete_tickers`.
- `audit_snapshot()` darf in diesem Fall nicht `complete` ausgeben.
- Die aktuelle Gap-Reparatur verwendet außerhalb des verifizierten IFEU-Zeitraums keinen generischen BDay-Ersatz für Brent.

### Betroffene Abläufe

Die identische Kalenderquelle wird in historischer Gap-Analyse, Recent-Gap-Analyse, letzter abgeschlossener Session, Stale-Prüfung und Datenbank-Audit verwendet. Damit wird verhindert, dass Diagnose und automatische Reparatur unterschiedliche Handelstage annehmen.

## 5. Vollständigkeit

- Die Änderung beschränkt sich auf die OHLCV-Kalenderlogik und die dazugehörigen Regressionstests.
- `analyse.py`, Markt-/Trading-Logik, Tickeruniversum, Datenbank-Schema, Datenquelle und Workflow-YAML wurden nicht verändert.
- Die bestehenden CME-, Xetra-, Korea- und FX-Overrides bleiben inhaltlich unverändert.
- Die bestehende ICEUS-Override-Logik wird nicht global verändert; nur `BZ=F` erhält die IFEU-Zuordnung.

## 6. Regressionen

**Bestanden:**
- Python-Compile-Prüfung für beide geänderten Dateien.
- `compileall -q .` für das gesamte entpackte Repository.
- Gezielte Regression: `pytest -q tests/test_historical_ohlcv.py tests/test_diagnose_historical_ohlcv_release.py`  
  Ergebnis: **37 passed**.
- Neue Tests prüfen IFEU-US-Feiertage/verkürzte Sessions, vollständige Schließtage, 2025-12-26, 2026-12-28, 2027-Easter/Boxing-Day-Verhalten, letzten abgeschlossenen Handelstag, historische und aktuelle Gap-Erkennung, Abdeckungsgrenzen sowie das Verhindern eines generischen BDay-Fallbacks.

**Nicht vollständig ausgeführt:**
- Der komplette Repository-Testlauf `pytest -q tests` wurde durch die Laufzeitumgebung nach rund 60 Sekunden automatisch unterbrochen. Das ist kein festgestellter Testfehler, aber auch kein vollständiger grüner Gesamttest.
- Die echte Release-Datenbank wurde in diesem lokalen Codepaket nicht diagnostiziert. Dafür ist nach dem GitHub-Upload der vom Benutzer gestartete read-only Workflow `OHLCV Release-Datenbank Diagnose` erforderlich.

## 7. Offizielle Quellen zur Kalenderregel

1. ICE Brent Crude Futures – Produktseite; nennt ICE Futures Europe und MIC-Code IFEU:  
   https://www.ice.com/products/219/Brent-Crude-Futures/
2. ICE Futures Europe Trading Schedule – Oil, Utilities and Agricultural, Juli 2026; enthält den Kalender 2026 und 2027 sowie die Status-/Session-Schlüssel A/B/C/D/TD:  
   https://www.ice.com/publicdocs/Trading_Schedule.pdf
3. ICE Futures Europe Trading Schedule 2025 – Fixed Income, Equity and Indices; bestätigt die vollständigen Exchange-Schließtage 2025:  
   https://www.ice.com/publicdocs/futures/Trading_Schedule_Migrated_Liffe_Contracts.pdf
4. ICE Circular 25/034 vom 02.04.2025; bestätigt Exchange closed am 18.04.2025 und open am 21.04.2025:  
   https://www.ice.com/publicdocs/circulars/25034.pdf
5. ICE Futures Europe Christmas/New Year Trading Hours 2025; bestätigt, dass Brent Futures am 26.12.2025 regulär offen waren:  
   https://www.ice.com/publicdocs/futures/IFEu_Christmas_New_Year_Trading_Hours_2025.pdf

## 8. Ergebnis und Freigabegrenze

Die IFEU-Kalenderkorrektur ist lokal implementiert und die gezielten OHLCV-/Diagnose-Regressionen sind grün. Der Code behauptet ausdrücklich keine vollständige Kalenderabdeckung außerhalb 2025–2027.

**Noch nicht bestätigt:** Wie viele der zuvor gemeldeten sieben BZ=F-Lücken nach Ausführung auf der echten Release-Datenbank verbleiben. Das kann erst der nächste Release-Diagnoselauf belegen.

Es wurden keinerlei Schreibaktionen auf GitHub ausgeführt. Der Benutzer lädt die gelieferten Dateien selbst hoch.
